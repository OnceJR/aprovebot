import os
import asyncio
import logging
import time
import random
import hmac
import hashlib
import json
from urllib.parse import unquote, parse_qsl
from aiogram.exceptions import TelegramRetryAfter
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey  # Necesario en aiogram 3 para cambiar estado a otros

from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    BotCommand, BotCommandScopeDefault, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, WebAppInfo
)
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient

# --- CONFIGURACIÓN PRINCIPAL ---
MAIN_BOT_TOKEN = "8857487034:AAGXIoZDTT_G4iOZGbt1fgpEmo3A9AucgP4"
MONGO_URI = "mongodb+srv://carlosjrpelegrina_db_user:1DNyN9AFa9bh1tCr@cluster0.haf2f1l.mongodb.net"

FORCE_SUB_CHANNEL_ID = -1004228343268 
FORCE_SUB_CHANNEL_LINK = "https://t.me/+3JwmD95p661hOTYx"
VIP_GROUP_ID = -1003774403748 

ADMIN_IDS = [8983189714, 7452819858]
SUPER_ADMIN_IDS = ADMIN_IDS  

bot = Bot(token=MAIN_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# Variables globales que se inicializarán dentro de main() para evitar RuntimeError
db_client = None
db = None
backup_queue = None

active_chats = {}
waiting_list = []
pending_trades = {}
processed_albums = set()
active_viewers = {}  # Diccionario para guardar {user_id: timestamp_del_ultimo_ping}
pending_notifications = {}
LOG_GROUP_ID = -1004402977057
chat_threads = {}                    

class BotStates(StatesGroup):
    idle = State()
    searching = State()
    chatting = State()
    waiting_trade_type = State()
    waiting_trade_amount = State()
    waiting_for_id = State()

# --- FUNCIONES AUXILIARES ---
async def set_other_user_state(bot: Bot, storage, chat_id: int, state: State):
    """Permite modificar el estado de FSM de otro usuario en aiogram 3"""
    key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
    fsm_ctx = FSMContext(storage=storage, key=key)
    await fsm_ctx.set_state(state)

async def get_user(user_id):
    user = await db.users.find_one({"_id": user_id})
    if not user:
        user = {"_id": user_id, "lang": "es", "referrals": 0, "reputation": 0, "mode": "anon", "in_vip": False, "notified_vip": False, "last_bonus": 0, "last_offer": 0}
        await db.users.insert_one(user)
    return user

async def save_user(user_id, data):
    await db.users.update_one({"_id": user_id}, {"$set": data}, upsert=True)

async def check_force_sub(user_id):
    if user_id in ADMIN_IDS: return True
    try:
        member = await bot.get_chat_member(FORCE_SUB_CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e: 
        logging.error(f"Error Force Sub: {e}")
        return False

async def get_backup_receivers():
    doc = await db.settings.find_one({"_id": "config"})
    extra_ids = doc.get("extra_receivers", []) if doc else []
    return list(set([ADMIN_IDS[0]] + extra_ids))

async def get_auth_user(request):
    init_data = request.headers.get("Authorization", "")
    if init_data:
        try:
            parsed = dict(parse_qsl(init_data))
            user_obj = json.loads(parsed.get('user', '{}'))
            if 'id' in user_obj:
                return int(user_obj['id'])
        except Exception as e:
            logging.warning(f"No se pudo extraer usuario del initData: {e}")
            
    try:
        query_id = int(request.query.get("id", 0))
        if query_id: 
            return query_id
    except: 
        pass
        
    return None

# --- APIS WEBAPP ---
async def api_live_ping(request):
    user_id = await get_auth_user(request)
    if not user_id: return web.json_response({"error": "Unauthorized"}, status=401)
    
    now = time.time()
    # 1. Registrar al usuario como espectador activo
    active_viewers[user_id] = now
    
    # 2. Limpiar usuarios inactivos
    for uid in list(active_viewers.keys()):
        if now - active_viewers[uid] > 45:
            del active_viewers[uid]
            
    viewers_count = len(active_viewers)
    
    # 3. Sumar tiempo y revisar si gana el premio
    user = await get_user(user_id)
    current_time = user.get("watch_time", 0) + 30 
    await db.users.update_one({"_id": user_id}, {"$set": {"watch_time": current_time}})
    
    won = False
    # Si alcanzó los 600 segundos (10 minutos) y NO tiene VIP
    if current_time >= 600 and not user.get("in_vip"):
        won = True
        await save_user(user_id, {"notified_vip": True, "in_vip": True})
        try:
            invite = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
            msg = "🎉 **¡Misión Cumplida!**\nGracias por quedarte en la transmisión. Como recompensa, aquí tienes tu acceso VIP exclusivo:"
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌟 Entrar al VIP", url=invite.invite_link)]])
            
            # Usamos bot.send_message en lugar de safe_send_message
            await bot.send_message(user_id, msg, reply_markup=markup, parse_mode="Markdown")
            
        except Exception as e:
            logging.error(f"Error enviando VIP por stream: {e}")

    return web.json_response({"success": True, "viewers": viewers_count, "watch_time": current_time, "won": won})

async def handle_live_webapp(request):
    bot_username = request.query.get("bot", "")
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Evento En Vivo</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.6.0/dist/confetti.browser.min.js"></script>
        <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            :root { --bg: #0d1117; --card-bg: #161b22; --accent: #f85149; --text: #ffffff; }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Poppins', sans-serif; }
            body { background: var(--bg); color: var(--text); padding: 16px; }
            .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
            .live-badge { background: var(--accent); padding: 4px 10px; border-radius: 6px; font-weight: 700; font-size: 14px; animation: pulse 2s infinite; }
            .viewers { background: rgba(255,255,255,0.1); padding: 4px 10px; border-radius: 6px; font-size: 14px; display: flex; gap: 6px; align-items: center; }
            @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
            
            .video-wrapper { background: #000; border-radius: 12px; overflow: hidden; margin-bottom: 20px; border: 1px solid #30363d; box-shadow: 0 4px 15px rgba(248, 81, 73, 0.2); }
            video { width: 100%; aspect-ratio: 16/9; display: block; }
            
            .mission-card { background: var(--card-bg); padding: 15px; border-radius: 12px; border: 1px solid #30363d; }
            .mission-title { font-weight: 700; font-size: 15px; margin-bottom: 10px; text-align: center; }
            .progress-bg { background: rgba(255,255,255,0.05); height: 20px; border-radius: 10px; position: relative; overflow: hidden; }
            .progress-fill { background: linear-gradient(90deg, #f85149, #ff7b72); height: 100%; width: 0%; transition: width 1s linear; }
            .time-text { position: absolute; width: 100%; text-align: center; top: 0; left: 0; font-size: 12px; line-height: 20px; font-weight: 700; text-shadow: 1px 1px 2px #000; }
            .success-msg { color: #3fb950; font-weight: 700; text-align: center; display: none; margin-top: 10px; }
        </style>
    </head>
    <body>
        <div class="header">
            <div class="live-badge"><i class="fa-solid fa-tower-broadcast"></i> EN VIVO</div>
            <div class="viewers"><i class="fa-solid fa-eye"></i> <span id="viewer-count">...</span></div>
        </div>

        <div class="video-wrapper">
            <!-- REPRODUCTOR HLS -->
            <video id="video-player" controls playsinline autoplay></video>
        </div>

        <div class="mission-card" id="mission-card">
            <div class="mission-title">🎁 Recompensa: Acceso VIP</div>
            <p style="font-size: 12px; color: #8b949e; text-align: center; margin-bottom: 10px;">Mira la transmisión por 10 minutos para reclamarlo.</p>
            <div class="progress-bg">
                <div class="progress-fill" id="progress-bar"></div>
                <div class="time-text" id="time-text">0 / 10 Minutos</div>
            </div>
            <div class="success-msg" id="success-msg">¡Misión completada! Revisa el bot. 🎉</div>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            let userId = tg.initDataUnsafe?.user?.id || 0;
            let reqHeaders = { "Content-Type": "application/json", "Authorization": tg.initData || "" };
            
            // 1. INICIAR VIDEO STREAM
            let video = document.getElementById('video-player');
            let videoSrc = 'https://stream.mux.com/re2DCw5QBp3Rta2SmZ00ToB63OBqWB007MH01a015nihuRQ.m3u8'; // <-- PON TU LINK .m3u8 AQUÍ
            
            if (Hls.isSupported()) {
                let hls = new Hls();
                hls.loadSource(videoSrc);
                hls.attachMedia(video);
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                video.src = videoSrc;
            }

            // 2. SISTEMA DE "LATIDOS" (PING)
            const TARGET_TIME = 600; // 600 segundos = 10 minutos
            
            async function sendPing() {
                if(!userId) return;
                try {
                    let res = await fetch(`/api/live_ping?id=${userId}`, { method: "POST", headers: reqHeaders, body: "{}" });
                    let data = await res.json();
                    
                    if(data.success) {
                        // Actualizar contadores visuales
                        document.getElementById('viewer-count').innerText = data.viewers;
                        
                        let progress = Math.min(100, (data.watch_time / TARGET_TIME) * 100);
                        document.getElementById('progress-bar').style.width = progress + "%";
                        
                        let mins = Math.floor(data.watch_time / 60);
                        document.getElementById('time-text').innerText = `${mins} / 10 Minutos`;

                        // Si ganó
                        if(data.won || data.watch_time >= TARGET_TIME) {
                            document.getElementById('success-msg').style.display = 'block';
                            if(data.won) {
                                tg.HapticFeedback.notificationOccurred('success');
                                confetti({ particleCount: 150, spread: 90, origin: { y: 0.6 } });
                            }
                        }
                    }
                } catch(e) { console.error("Error pingeando servidor"); }
            }

            // Enviar primer latido de inmediato, luego cada 30 segundos
            sendPing();
            setInterval(sendPing, 30000);
        </script>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type="text/html")

async def api_get_data(request):
    user_id = await get_auth_user(request)
    if not user_id: return web.json_response({"error": "Unauthorized"}, status=401)
    
    user = await get_user(user_id)
    fotos = await db.inventory.count_documents({"user_id": user_id, "type": "photo"})
    videos = await db.inventory.count_documents({"user_id": user_id, "type": "video"})
    
    now = time.time()
    await db.offers.delete_many({"time": {"$lt": now - 86400}})
    
    top_users = []
    async for u in db.users.find().sort("reputation", -1).limit(10):
        if u.get("reputation", 0) > 0:
            top_users.append({"id": u["_id"], "rep": u.get("reputation", 0)})
        
    offers = []
    async for o in db.offers.find().sort("time", -1).limit(20):
        offers.append({"user_id": o["user_id"], "name": o["name"], "text": o["text"], "time": o.get("time", now)})
    
    last_bonus = user.get("last_bonus", 0)
    time_left_bonus = max(0, (last_bonus + (6 * 3600)) - now)
    
    last_offer = user.get("last_offer", 0)
    time_left_offer = max(0, (last_offer + 3600) - now)
    
    return web.json_response({
        "fotos": fotos, "videos": videos,
        "reputation": user.get("reputation", 0),
        "referrals": user.get("referrals", 0),
        "time_left": time_left_bonus,
        "offer_cooldown": time_left_offer,
        "leaderboard": top_users,
        "offers": offers
    })

async def api_claim_bonus(request):
    user_id = await get_auth_user(request)
    if not user_id: return web.json_response({"error": "Unauthorized"}, status=401)
    
    user = await get_user(user_id)
    now = time.time()
    last_bonus = user.get("last_bonus", 0)
    cooldown = 6 * 3600
    
    if now < last_bonus + cooldown:
        return web.json_response({"success": False, "error": "Cooldown active"})
        
    puntos_ganados = random.randint(1, 5)
    nueva_rep = user.get("reputation", 0) + puntos_ganados
    
    await db.users.update_one({"_id": user_id}, {"$set": {"last_bonus": now, "reputation": nueva_rep}})
    await check_vip_status(user_id)
    return web.json_response({"success": True, "bonus": puntos_ganados, "new_rep": nueva_rep, "time_left": cooldown})

async def api_post_offer(request):
    user_id = await get_auth_user(request)
    if not user_id: return web.json_response({"error": "Unauthorized"}, status=401)
    
    user = await get_user(user_id)
    now = time.time()
    last_offer = user.get("last_offer", 0)
    
    if now < last_offer + 3600:
        return web.json_response({"success": False, "error": "Debes esperar 1 hora entre ofertas."})
    
    data = await request.json()
    text = data.get("text", "").strip()[:120] 
    name = data.get("name", "Anónimo")
    
    if len(text) >= 10:
        await db.offers.insert_one({"user_id": user_id, "name": name, "text": text, "time": now})
        await db.users.update_one({"_id": user_id}, {"$set": {"last_offer": now}})
        return web.json_response({"success": True})
        
    return web.json_response({"success": False, "error": "La oferta es muy corta."})

async def api_clear_inv(request):
    user_id = await get_auth_user(request)
    if not user_id: return web.json_response({"error": "Unauthorized"}, status=401)
    await db.inventory.delete_many({"user_id": user_id})
    return web.json_response({"success": True})

async def handle_webapp(request):
    bot_username = request.query.get("bot", "")
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Exchange Panel</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.6.0/dist/confetti.browser.min.js"></script>
        <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            :root {
                --bg: #0d1117; --card-bg: #161b22; --card-border: #30363d;
                --text: #c9d1d9; --text-strong: #ffffff; --hint: #8b949e;
                --accent: #58a6ff; --danger: #f85149; --success: #2ea043; --gold: #e3b341;
                --gradient-gold: linear-gradient(135deg, #f9d423 0%, #ff4e50 100%);
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Poppins', sans-serif; }
            body { background: var(--bg); color: var(--text); padding: 16px; padding-bottom: 24px; }
            .header { text-align: center; margin-bottom: 20px; margin-top: 10px; display: flex; justify-content: center; align-items: center; gap: 10px; }
            .header h1 { font-size: 24px; font-weight: 800; color: var(--text-strong); text-transform: uppercase; }
            .header-icon { font-size: 28px; color: var(--accent); }
            .tabs { display: flex; background: var(--card-bg); border-radius: 14px; padding: 6px; margin-bottom: 24px; overflow-x: auto; border: 1px solid var(--card-border); scrollbar-width: none; }
            .tab { flex: none; width: 32%; text-align: center; padding: 12px 6px; font-size: 14px; font-weight: 600; color: var(--hint); cursor: pointer; display: flex; flex-direction: column; gap: 4px; }
            .tab.active { background: var(--accent); color: #fff; box-shadow: 0 4px 12px rgba(88, 166, 255, 0.3); border-radius: 10px; }
            .section { display: none; flex-direction: column; gap: 16px; }
            .section.active { display: flex; }
            .card { background: var(--card-bg); border-radius: 16px; padding: 20px; border: 1px solid var(--card-border); }
            .card-title { font-size: 16px; font-weight: 700; color: var(--text-strong); margin-bottom: 12px; }
            .btn-main { background: var(--accent); color: #fff; border: none; border-radius: 12px; padding: 14px; width: 100%; font-size: 15px; font-weight: 700; cursor: pointer; }
            .btn-outline { background: transparent; border: 2px solid var(--accent); color: var(--accent); }
            .btn-danger { background: rgba(248, 81, 73, 0.1); color: var(--danger); border: 1px solid var(--danger); }
            .copy-btn { background: rgba(88, 166, 255, 0.1); border: 1px solid rgba(88, 166, 255, 0.3); color: var(--accent); padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px; margin-top: 10px; }
            .copy-btn:active { background: var(--accent); color: #fff; }
            .input-group { margin-bottom: 12px; }
            input[type="text"] { width: 100%; padding: 14px; border-radius: 12px; border: 1px solid var(--card-border); background: rgba(0,0,0,0.2); color: #fff; font-family: 'Poppins'; outline: none;}
            input[type="text"]:focus { border-color: var(--accent); }
            .progress-bg { background: rgba(255,255,255,0.05); border-radius: 10px; height: 14px; width: 100%; }
            .progress-fill { background: var(--gradient-gold); height: 100%; width: 0%; border-radius: 10px; transition: width 0.8s ease-in-out; }
            .chests-container { display: flex; justify-content: center; gap: 15px; margin: 20px 0; }
            .chest-wrapper { width: 90px; height: 90px; cursor: pointer; position: relative; transition: transform 0.2s;}
            .chest-wrapper:active { transform: scale(0.95); }
            .chest-wrapper.disabled { opacity: 0.5; filter: grayscale(100%); pointer-events: none; }
            .chest-img { width: 100%; height: 100%; object-fit: contain; }
            .list-item { background: rgba(255,255,255,0.03); padding: 16px; border-radius: 12px; margin-bottom: 12px; border: 1px solid var(--card-border); }
        </style>
    </head>
    <body>
        <div class="header"><i class="fa-solid fa-bolt header-icon"></i><h1>Exchange Hub</h1></div>
        <div class="tabs">
            <div class="tab active" onclick="switchTab('stats', this)">VIP</div>
            <div class="tab" onclick="switchTab('cofres', this)">Bonus</div>
            <div class="tab" onclick="switchTab('mercado', this)">Market</div>
            <div class="tab" onclick="switchTab('rank', this)">Top</div>
            <div class="tab" onclick="switchTab('inventory', this)">Cofre</div>
        </div>

        <div id="stats" class="section active">
            <div class="card">
                <div class="card-title">Progreso VIP</div>
                <div style="display:flex; justify-content:space-between;"><span>Reputación</span><strong id="vip-text">--/20</strong></div>
                <div class="progress-bg" style="margin-top:10px;"><div class="progress-fill" id="vip-fill"></div></div>
            </div>
            <div class="card">
                <div class="card-title">Referidos (<span id="ref-count">0</span>/3)</div>
                <button class="btn-main btn-outline" onclick="copyRefLink()"><i class="fa-solid fa-link"></i> Copiar Link Invitación</button>
            </div>
        </div>

        <div id="cofres" class="section">
            <div class="card" style="text-align: center;">
                <div class="card-title" style="color:var(--gold);">Recompensa Diaria</div>
                <div class="chests-container" id="chests-container"></div>
                <div id="bonus-status" style="font-weight:700; color:var(--hint); margin-top:10px;">Calculando...</div>
            </div>
        </div>

        <div id="mercado" class="section">
            <div class="card">
                <div class="card-title">Publicar Oferta</div>
                <div class="input-group"><input type="text" id="offer-input" placeholder="Ofrezco X busco Y..." maxlength="120"></div>
                <button class="btn-main" onclick="postOffer()" id="btn-post-offer"><i class="fa-solid fa-paper-plane"></i> Publicar</button>
                <div id="offer-cooldown" style="color:var(--danger); display:none; margin-top:10px; font-size: 12px; text-align: center;">Debe esperar para publicar.</div>
            </div>
            <div class="card"><div class="card-title">Mercado En Vivo</div><div id="offers-list">Cargando...</div></div>
        </div>

        <div id="rank" class="section">
            <div class="card"><div class="card-title">Top 10 Semanal</div><div id="ranking-list">Cargando...</div></div>
        </div>

        <div id="inventory" class="section">
            <div class="card">
                <div class="card-title">Tu Caja Fuerte</div>
                <p>📷 Fotos: <strong id="photo-count">--</strong> | 🎥 Videos: <strong id="video-count">--</strong></p>
                <br>
                <button class="btn-main btn-danger" onclick="clearInventory()"><i class="fa-solid fa-trash-can"></i> Vaciar Inventario</button>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            let user = tg.initDataUnsafe?.user;
            let userId = user?.id || 0;
            let botUsername = "BOT_USERNAME_PLACEHOLDER"; 
            let reqHeaders = { "Content-Type": "application/json", "Authorization": tg.initData || "" };

            function switchTab(tabId, el) {
                tg.HapticFeedback.impactOccurred('light');
                document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                el.classList.add('active');
            }

            // --- SISTEMA DE PORTAPAPELES UNIVERSAL ---
            function copyToClipboard(text, successMessage) {
                tg.HapticFeedback.impactOccurred('medium');
                let temp = document.createElement("input");
                temp.value = text;
                document.body.appendChild(temp);
                temp.select();
                try {
                    document.execCommand("copy");
                    tg.showAlert(successMessage + "\\n\\n" + text);
                } catch (err) {
                    tg.showAlert("No se pudo copiar automáticamente. Cópialo manualmente:\\n\\n" + text);
                }
                document.body.removeChild(temp);
            }

            function copyRefLink() {
                let link = `https://t.me/${botUsername}?start=${userId}`;
                copyToClipboard(link, "✅ Link copiado al portapapeles. ¡Envíalo a tus amigos!");
            }

            function copyOfferId(targetId) {
                copyToClipboard(targetId, "✅ ID copiado.\\n\\nVuelve al menú del bot y selecciona '🆔 Conectar ID' para pegarlo.");
            }
            // -----------------------------------------

            let chestsContainer = document.getElementById('chests-container');
            let isBonusReady = false;
            
            function initChests(ready) {
                isBonusReady = ready;
                chestsContainer.innerHTML = "";
                for(let i=0; i<3; i++) {
                    let w = document.createElement('div');
                    w.className = `chest-wrapper ${ready ? '' : 'disabled'}`;
                    w.onclick = () => ready ? openChest(w) : null;
                    w.innerHTML = `<img src="https://img.icons8.com/color/96/treasure-chest.png" class="chest-img">`;
                    chestsContainer.appendChild(w);
                }
            }

            let bonusTimer;
            function updateBonusUI(timeLeft) {
                let txt = document.getElementById("bonus-status");
                clearInterval(bonusTimer);
                if (timeLeft <= 0) {
                    if(!isBonusReady) initChests(true);
                    txt.innerText = "¡Toca un cofre!";
                    txt.style.color = "var(--success)";
                } else {
                    if(isBonusReady) initChests(false);
                    txt.style.color = "var(--hint)";
                    bonusTimer = setInterval(() => {
                        timeLeft--;
                        if (timeLeft <= 0) updateBonusUI(0);
                        else {
                            let h = Math.floor(timeLeft / 3600);
                            let m = Math.floor((timeLeft % 3600) / 60);
                            let s = Math.floor(timeLeft % 60);
                            txt.innerText = `⏳ Disponible en: ${h}h ${m}m ${s}s`;
                        }
                    }, 1000);
                }
            }

            async function openChest(el) {
                if(!isBonusReady) return;
                tg.HapticFeedback.impactOccurred('heavy');
                document.querySelectorAll('.chest-wrapper').forEach(w => w.classList.add('disabled'));
                document.getElementById("bonus-status").innerText = "Abriendo...";
                
                try {
                    let res = await fetch(`/api/bonus?id=${userId}`, { method: "POST", headers: reqHeaders, body: "{}" });
                    let data = await res.json();
                    
                    if(data.success) {
                        el.classList.remove('disabled');
                        el.querySelector('.chest-img').src = "https://img.icons8.com/color/96/open-box.png";
                        confetti({ particleCount: 120, spread: 80, origin: { y: 0.6 } });
                        tg.showAlert(`🎉 ¡Felicidades! Ganaste ${data.bonus} Puntos de Reputación.`);
                        loadData();
                    } else {
                        tg.showAlert("⚠️ Aún debes esperar el tiempo indicado.");
                        loadData();
                    }
                } catch(e) { 
                    // FIX: Reiniciar UI de cofres si hay error de red
                    tg.showAlert("❌ Error de conexión al servidor."); 
                    document.querySelectorAll('.chest-wrapper').forEach(w => w.classList.remove('disabled'));
                    document.getElementById("bonus-status").innerText = "¡Toca un cofre!";
                    loadData(); 
                }
            }

            async function loadData() {
                if (!userId) return;
                try {
                    let res = await fetch(`/api/data?id=${userId}`, { headers: reqHeaders });
                    let data = await res.json();
                    
                    document.getElementById("photo-count").innerText = data.fotos;
                    document.getElementById("video-count").innerText = data.videos;
                    document.getElementById("vip-text").innerText = `${data.reputation}/20`;
                    document.getElementById("vip-fill").style.width = Math.min(100, (data.reputation / 20) * 100) + "%";
                    document.getElementById("ref-count").innerText = data.referrals;
                    
                    updateBonusUI(data.time_left);
                    
                    let btnPost = document.getElementById("btn-post-offer");
                    let cdText = document.getElementById("offer-cooldown");
                    if (data.offer_cooldown > 0) {
                        btnPost.disabled = true;
                        cdText.style.display = "block";
                        cdText.innerText = `⏳ Próxima publicación en ${Math.ceil(data.offer_cooldown/60)} min.`;
                    } else {
                        btnPost.disabled = false;
                        cdText.style.display = "none";
                    }
                    
                    let rHTML = "";
                    data.leaderboard.forEach((u, i) => {
                        let icon = i === 0 ? "👑" : i === 1 ? "🥈" : i === 2 ? "🥉" : `<strong>#${i+1}</strong>`;
                        rHTML += `<div class="list-item" style="display:flex; justify-content:space-between;"><span>${icon} ID: ${u.id}</span><strong>${u.rep} Pts</strong></div>`;
                    });
                    document.getElementById("ranking-list").innerHTML = rHTML || '<div style="text-align:center;color:var(--hint); font-size:14px;">Aún no hay datos.</div>';

                    let oHTML = "";
                    data.offers.forEach(o => {
                        oHTML += `<div class="list-item">
                                    <div style="font-weight:700; font-size:14px; margin-bottom:4px;"><i class="fa-solid fa-circle-user"></i> ${o.name}</div>
                                    <div style="font-size:13px; line-height:1.4;">${o.text}</div>
                                    <button onclick="copyOfferId('${o.user_id}')" class="copy-btn"><i class="fa-regular fa-copy"></i> Copiar ID</button>
                                  </div>`;
                    });
                    document.getElementById("offers-list").innerHTML = oHTML || '<div style="text-align:center;color:var(--hint); font-size:14px;">El mercado está vacío.</div>';
                } catch(e) { console.error("Error loading data", e); }
            }

            async function postOffer() {
                let val = document.getElementById('offer-input').value.trim();
                if(val.length < 10) return tg.showAlert("⚠️ Oferta muy corta (min. 10 letras). Detalla qué ofreces y qué buscas.");
                
                let btn = document.getElementById("btn-post-offer");
                btn.disabled = true;
                btn.innerText = "Publicando...";

                try {
                    let res = await fetch(`/api/offer?id=${userId}`, { method: "POST", headers: reqHeaders, body: JSON.stringify({ text: val, name: user?.first_name || "Anónimo" }) });
                    let data = await res.json();
                    
                    if(data.success) {
                        document.getElementById('offer-input').value = "";
                        tg.HapticFeedback.notificationOccurred('success');
                        tg.showAlert("✅ Publicado con éxito en el mercado.");
                    } else { 
                        tg.showAlert(data.error); 
                    }
                } catch(e) { 
                    tg.showAlert("❌ Error de red al publicar."); 
                }
                
                btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i> Publicar';
                loadData();
            }

            async function clearInventory() {
                tg.showConfirm("⚠️ ¿Vaciar todas tus fotos y videos permanentemente?", async (ok) => {
                    if(ok) {
                        try {
                            await fetch(`/api/clear?id=${userId}`, { method: "POST", headers: reqHeaders });
                            tg.HapticFeedback.notificationOccurred('success');
                            tg.showAlert("🗑️ Caja fuerte vaciada.");
                            loadData();
                        } catch(e) {
                            tg.showAlert("❌ Error al vaciar inventario.");
                        }
                    }
                });
            }

            initChests(false);
            loadData();
        </script>
    </body>
    </html>
    """.replace("BOT_USERNAME_PLACEHOLDER", bot_username)
    return web.Response(text=html_content, content_type="text/html")

async def start_web_server():
    app = web.Application()
    
    # Ruta principal del panel de control
    app.router.add_get("/", handle_webapp)
    
    # --- NUEVAS RUTAS PARA EL EN VIVO ---
    app.router.add_get("/live", handle_live_webapp)
    app.router.add_post("/api/live_ping", api_live_ping)
    # ------------------------------------
    
    # APIs del sistema general
    app.router.add_get("/api/data", api_get_data)
    app.router.add_post("/api/bonus", api_claim_bonus)
    app.router.add_post("/api/offer", api_post_offer)
    app.router.add_post("/api/clear", api_clear_inv)
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()

async def backup_worker():
    while True:
        task = await backup_queue.get()
        try:
            caption = f"👤 Subido por: {task['name']} (`{task['user_id']}`)"
            file_id = task["file_id"]
            receivers = await get_backup_receivers()
            for receiver_id in receivers:
                try:
                    if task["type"] == "photo": await bot.send_photo(receiver_id, file_id, caption=caption)
                    elif task["type"] == "video": await bot.send_video(receiver_id, file_id, caption=caption)
                    else: await bot.send_document(receiver_id, file_id, caption=caption)
                except: pass
                await asyncio.sleep(2.5)
        except Exception as e: 
            print(f"❌ Error en cola: {e}")
        finally: 
            backup_queue.task_done()

async def setup_bot_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Menú principal"),
        BotCommand(command="help", description="📖 Guía de uso y ayuda"),
        BotCommand(command="leave", description="❌ Salir del chat actual")
    ], scope=BotCommandScopeDefault())

# --- COMANDOS ADMINISTRATIVOS ---
@router.message(Command("add_receiver"))
async def cmd_add_receiver(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    try:
        new_id = int(message.text.split()[1])
        await db.settings.update_one({"_id": "config"}, {"$addToSet": {"extra_receivers": new_id}}, upsert=True)
        await message.answer(f"✅ Añadido `{new_id}`.")
    except: await message.answer("⚠️ Uso: `/add_receiver ID`")

@router.message(Command("del_receiver"))
async def cmd_del_receiver(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    try:
        rem_id = int(message.text.split()[1])
        await db.settings.update_one({"_id": "config"}, {"$pull": {"extra_receivers": rem_id}}, upsert=True)
        await message.answer(f"✅ ID `{rem_id}` eliminado.")
    except: await message.answer("⚠️ Uso: `/del_receiver ID`")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    text = message.text.replace("/broadcast", "").strip()
    if not text: return await message.answer("⚠️ Escribe el mensaje a difundir.")
    await message.answer("⏳ Iniciando difusión...")
    count = 0
    async for user in db.users.find():
        try:
            # Cambiado a HTML para que reconozca las etiquetas <b> y el aviso salga en negrita correcta
            await bot.send_message(user["_id"], f"📢 <b>Aviso:</b>\n\n{text}", parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.05) 
        except: pass
    await message.answer(f"✅ Difusión completada a <code>{count}</code> usuarios.", parse_mode="HTML")

@router.message(Command("migrar_respaldo"))
async def cmd_migrar_respaldo(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    await message.answer("🔄 **Migrando...**")
    c_ok, c_err = 0, 0
    receivers = await get_backup_receivers()
    async for doc in db.inventory.find({}):
        caption = f"♻️ [Migrado] ID: `{doc['user_id']}`"
        ok = False
        for rec in receivers:
            try:
                if doc["type"] == "photo": await bot.send_photo(rec, doc["file_id"], caption=caption)
                elif doc["type"] == "video": await bot.send_video(rec, doc["file_id"], caption=caption)
                else: await bot.send_document(rec, doc["file_id"], caption=caption)
                ok = True
            except: pass
        if ok: c_ok += 1
        else: c_err += 1
        await asyncio.sleep(2.5)
    await message.answer(f"✅ **Finalizado.**\nExitosos: `{c_ok}` | Fallidos: `{c_err}`")
    
@router.message(Command("estadisticas"))
async def cmd_stats(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    
    total_users = await db.users.count_documents({})
    total_files = await db.inventory.count_documents({})
    active_chats_count = len(active_chats) // 2
    
    total_archivos_enviados = await db.exchange_history.count_documents({})
    operaciones_reales = total_archivos_enviados // 2
    
    vip_users = await db.users.count_documents({"in_vip": True})
    
    stats_text = (
        "📊 **ESTADÍSTICAS GLOBALES**\n\n"
        f"👥 Usuarios registrados: `{total_users}`\n"
        f"🌟 Usuarios VIP: `{vip_users}`\n"
        f"📁 Archivos en cofre: `{total_files}`\n"
        f"🔄 Intercambios exitosos: `{operaciones_reales}`\n"
        f"💬 Chats en vivo: `{active_chats_count}`"
    )
    
    await message.answer(stats_text, parse_mode="Markdown")

@router.message(Command("reinvitar"))
async def cmd_reinvite(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    try:
        t_id = int(message.text.split()[1])
        await bot.unban_chat_member(chat_id=VIP_GROUP_ID, user_id=t_id, only_if_banned=True)
        link = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
        await bot.send_message(t_id, f"🎉 ¡VIP Restablecido!\nÚnete: {link.invite_link}")
        await message.answer("✅ Reinvitado.")
    except: await message.answer("⚠️ Uso: `/reinvitar ID`")

# --- MENÚ PRINCIPAL ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    user = await get_user(user_id)
    lang = user.get("lang", "es")

    if len(args) > 1 and args[1].isdigit():
        inviter_id = int(args[1])
        if inviter_id != user_id and not await db.users.find_one({"referred_by": user_id}):
            await save_user(user_id, {"referred_by": inviter_id})
            await db.users.update_one({"_id": inviter_id}, {"$inc": {"referrals": 1}})
            await check_vip_status(inviter_id) 

    if not await check_force_sub(user_id):
        btn_join = "📢 Unirse al Canal" if lang == "es" else "📢 Join Channel"
        btn_ver = "✅ Verificar Ingreso" if lang == "es" else "✅ Verify Join"
        txt_res = "🛑 **Acceso Restringido**\nDebes unirte a nuestro canal para usar el bot." if lang == "es" else "🛑 **Access Restricted**\nYou must join our channel to use the bot."
        
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_join, url=FORCE_SUB_CHANNEL_LINK)],
            [InlineKeyboardButton(text=btn_ver, callback_data="verify_sub")]
        ])
        return await message.answer(txt_res, reply_markup=markup, parse_mode="Markdown")

    await show_main_menu(user_id)
    await state.set_state(BotStates.idle)

@router.callback_query(F.data == "verify_sub")
async def verify_sub(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    lang = user.get("lang", "es")
    
    if await check_force_sub(callback.from_user.id):
        await callback.message.delete()
        await show_main_menu(callback.from_user.id)
    else: 
        err_msg = "⚠️ Aún no te has unido." if lang == "es" else "⚠️ You haven't joined yet."
        await callback.answer(err_msg, show_alert=True)

async def show_main_menu(user_id):
    user = await get_user(user_id)
    lang = user.get("lang", "es")
    bot_info = await bot.get_me()
    my_link = f"https://t.me/{bot_info.username}?start={user['_id']}"
    
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://TU_DOMINIO_AQUI.onrender.com")
    webapp_url = f"{base_url}?bot={bot_info.username}"
    live_url = f"{base_url}/live?bot={bot_info.username}"
    
    btn_live = "🔴 EN VIVO - ¡Gana VIP Gratis! 🎁" if lang == "es" else "🔴 LIVE - Earn Free VIP! 🎁"
    btn_rnd = "💬 Buscar Chat" if lang == "es" else "💬 Random Chat"
    btn_id = "🆔 Conectar ID" if lang == "es" else "🆔 Connect ID"
    btn_prof = "👤 Mi Perfil" if lang == "es" else "👤 My Profile"
    btn_share = "🔗 Compartir Link" if lang == "es" else "🔗 Share Link"
    btn_panel = "✨ Abrir Panel de Control" if lang == "es" else "✨ Open Dashboard"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        # [InlineKeyboardButton(text=btn_live, web_app=WebAppInfo(url=live_url))],
        [InlineKeyboardButton(text=btn_panel, web_app=WebAppInfo(url=webapp_url))],
        [InlineKeyboardButton(text=btn_rnd, callback_data="find_chat"), InlineKeyboardButton(text=btn_id, callback_data="connect_id")],
        [InlineKeyboardButton(text=btn_prof, callback_data="my_profile"), InlineKeyboardButton(text="⚙️ Idioma / Language", callback_data="change_lang")],
        [InlineKeyboardButton(text=btn_share, url=f"https://t.me/share/url?url={my_link}")]
    ])
    
    if lang == "es":
        txt = (
            "👋 <b>¡Bienvenido a la red de intercambio!</b>\n\n"
            "⚠️ <b>REQUISITO CLAVE:</b> Sube material propio a este chat para poder hacer intercambios. "
            "¡Sin videos o fotos en tu inventario, no podrás recibir nada!\n\n"
            "🎁 Utiliza la nueva <b>Mini App</b> para reclamar tu bonus diario y ver tu progreso VIP. 🚀"
        )
    else:
        txt = (
            "👋 <b>Welcome to the exchange network!</b>\n\n"
            "⚠️ <b>KEY REQUIREMENT:</b> Upload your own media to this chat to be able to trade. "
            "Without videos or photos in your inventory, you won't receive anything!\n\n"
            "🎁 Use the new <b>Mini App</b> to claim your daily bonus and check your VIP progress. 🚀"
        )
        
    await bot.send_message(chat_id=user_id, text=txt, reply_markup=markup, parse_mode="HTML")

@router.message(Command("help"))
async def cmd_help(message: Message):
    user = await get_user(message.from_user.id)
    lang = user.get("lang", "es")
    
    if lang == "es":
        txt = "🤖 **Guía Completa**\n\n📦 **1. Carga inventario:** Sube fotos/videos aquí para llenarlo.\n💬 **2. Inicia Chat:** Conecta al azar o por ID.\n🤝 **3. Lotes:** Usa el botón 'Proponer' en el chat.\n🌟 **4. VIP:** Gana 20 puntos de reputación (con intercambios o bonus diarios) para entrar al grupo VIP."
    else:
        txt = "🤖 **Complete Guide**\n\n📦 **1. Load inventory:** Upload photos/videos here.\n💬 **2. Start Chat:** Connect randomly or by ID.\n🤝 **3. Batches:** Use the 'Propose' button in chat.\n🌟 **4. VIP:** Earn 20 reputation points (via trades or daily bonus) to enter the VIP group."
    await message.answer(txt, parse_mode="Markdown")

# --- PERFIL E IDIOMA ---
@router.callback_query(F.data == "change_lang")
async def change_lang(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    new_lang = "en" if user.get("lang") == "es" else "es"
    await save_user(callback.from_user.id, {"lang": new_lang})
    msg = "✅ Idioma actualizado" if new_lang == "es" else "✅ Language updated"
    await callback.answer(msg)
    await callback.message.delete()
    await show_main_menu(callback.from_user.id)

@router.callback_query(F.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    lang = user.get("lang", "es")
    uid = user["_id"]
    fotos = await db.inventory.count_documents({"user_id": uid, "type": "photo"})
    videos = await db.inventory.count_documents({"user_id": uid, "type": "video"})
    
    btn_mod = "🔄 Cambiar Modo" if lang == "es" else "🔄 Change Mode"
    btn_vol = "⬅️ Volver" if lang == "es" else "⬅️ Back"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_mod, callback_data="toggle_mode")],
        [InlineKeyboardButton(text=btn_vol, callback_data="back_main")]
    ])
    
    if lang == "es":
        modo = "🕵️‍♂️ Anónimo" if user.get("mode") == "anon" else "👤 Público"
        txt = f"👤 **Tu Perfil**\n\n🆔 ID: `{uid}`\n🌟 Reputación: `{user.get('reputation', 0)}/20`\n👥 Referidos: `{user.get('referrals', 0)}/3`\n🎭 Modo: **{modo}**\n\n📦 Inventario: 📷 {fotos} | 🎥 {videos}"
    else:
        modo = "🕵️‍♂️ Anonymous" if user.get("mode") == "anon" else "👤 Public"
        txt = f"👤 **Your Profile**\n\n🆔 ID: `{uid}`\n🌟 Reputation: `{user.get('reputation', 0)}/20`\n👥 Referrals: `{user.get('referrals', 0)}/3`\n🎭 Mode: **{modo}**\n\n📦 Inventory: 📷 {fotos} | 🎥 {videos}"
        
    await callback.message.edit_text(txt, reply_markup=markup, parse_mode="Markdown")

@router.callback_query(F.data == "toggle_mode")
async def toggle_mode(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await save_user(user["_id"], {"mode": "public" if user.get("mode") == "anon" else "anon"})
    await show_profile(callback)

@router.callback_query(F.data == "back_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.idle)
    await callback.message.delete()
    await show_main_menu(callback.from_user.id)

# --- EMPAREJAMIENTO Y CHAT ---
@router.callback_query(F.data == "connect_id")
async def ask_for_id(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.get("lang", "es")
    txt = "✏️ Escribe el **ID numérico** del usuario:" if lang == "es" else "✏️ Enter the user's **numeric ID**:"
    btn = "⬅️ Cancelar" if lang == "es" else "⬅️ Cancel"
    
    await state.set_state(BotStates.waiting_for_id)
    await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn, callback_data="back_main")]]), parse_mode="Markdown")

@router.message(StateFilter(BotStates.waiting_for_id))
async def process_connect_id(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user.get("lang", "es")
    
    if not message.text.isdigit(): 
        return await message.answer("⚠️ Debe ser un número." if lang == "es" else "⚠️ Must be a number.")
        
    t_id, u_id = int(message.text), message.from_user.id
    if t_id == u_id: return
    if t_id in active_chats or t_id in waiting_list: 
        return await message.answer("⚠️ Ocupado." if lang == "es" else "⚠️ Busy.")
    
    t_user = await get_user(t_id)
    t_lang = t_user.get("lang", "es")
    
    btn_acc = "✅ Aceptar" if t_lang == "es" else "✅ Accept"
    btn_rej = "❌ Rechazar" if t_lang == "es" else "❌ Reject"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_acc, callback_data=f"accept_id_{u_id}")], [InlineKeyboardButton(text=btn_rej, callback_data=f"reject_id_{u_id}")]])
    
    txt_notif = f"🔔 **Solicitud de Chat de ID:** `{u_id}`" if t_lang == "es" else f"🔔 **Chat Request from ID:** `{u_id}`"
    await bot.send_message(t_id, txt_notif, reply_markup=markup, parse_mode="Markdown")
    await message.answer("⏳ Solicitud enviada." if lang == "es" else "⏳ Request sent.")
    await state.set_state(BotStates.idle)

@router.callback_query(F.data.startswith("accept_id_"))
async def accept_id_connection(callback: CallbackQuery, state: FSMContext):
    t_id, u_id = int(callback.data.split("_")[2]), callback.from_user.id
    user = await get_user(u_id)
    t_user = await get_user(t_id)
    
    if t_id in active_chats or u_id in active_chats: 
        return await callback.answer("Ocupado." if user.get("lang") == "es" else "Busy.", show_alert=True)
        
    active_chats[u_id], active_chats[t_id] = t_id, u_id
    
    # --- CREAR TEMA EN EL GRUPO DE LOGS ---
    try:
        topic = await bot.create_forum_topic(chat_id=LOG_GROUP_ID, name=f"Chat {u_id} & {t_id}")
        chat_threads[u_id] = topic.message_thread_id
        chat_threads[t_id] = topic.message_thread_id
    except Exception as e:
        logging.error(f"Error creando tema en grupo log: {e}")
    # -------------------------------------

    await state.set_state(BotStates.chatting)
    await set_other_user_state(bot, dp.storage, t_id, BotStates.chatting)
    
    for uid, u_obj in [(u_id, user), (t_id, t_user)]:
        lng = u_obj.get("lang", "es")
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"), KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]], resize_keyboard=True)
        msg = "✅ **Conexión Establecida.**" if lng == "es" else "✅ **Connection Established.**"
        await bot.send_message(uid, msg, reply_markup=kb, parse_mode="Markdown")
        
    await callback.message.delete()

@router.callback_query(F.data == "find_chat")
async def find_chat(callback: CallbackQuery, state: FSMContext):
    u_id = callback.from_user.id
    user = await get_user(u_id)
    lang = user.get("lang", "es")
    
    if waiting_list:
        t_id = waiting_list.pop(0)
        t_user = await get_user(t_id)
        
        active_chats[u_id], active_chats[t_id] = t_id, u_id
        await state.set_state(BotStates.chatting)
        await set_other_user_state(bot, dp.storage, t_id, BotStates.chatting)
        
        # --- CREAR TEMA EN EL GRUPO DE LOGS ---
        try:
            topic = await bot.create_forum_topic(chat_id=LOG_GROUP_ID, name=f"Chat {u_id} & {t_id}")
            chat_threads[u_id] = topic.message_thread_id
            chat_threads[t_id] = topic.message_thread_id
        except Exception as e:
            logging.error(f"Error creando tema en grupo log: {e}")
        # -------------------------------------
        
        for uid, u_obj in [(u_id, user), (t_id, t_user)]:
            lng = u_obj.get("lang", "es")
            kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"), KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]], resize_keyboard=True)
            msg = "✅ **¡Chat encontrado!**" if lng == "es" else "✅ **Chat found!**"
            await bot.send_message(uid, msg, reply_markup=kb, parse_mode="Markdown")
            
        await callback.message.delete()
    else:
        waiting_list.append(u_id)
        await state.set_state(BotStates.searching)
        txt = "🔍 **Buscando...**" if lang == "es" else "🔍 **Searching...**"
        btn = "❌ Cancelar" if lang == "es" else "❌ Cancel"
        await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn, callback_data="leave_chat")]]), parse_mode="Markdown")

@router.message(F.text.in_(["❌ Desconectar", "❌ Disconnect"]))
@router.message(Command("leave"))
@router.callback_query(F.data == "leave_chat")
async def leave_chat(event, state: FSMContext):
    u_id = event.from_user.id
    user = await get_user(u_id)
    lang = user.get("lang", "es")
    
    if u_id in waiting_list: waiting_list.remove(u_id)
    t_id = active_chats.pop(u_id, None)
    
    if t_id:
        active_chats.pop(t_id, None)
        t_user = await get_user(t_id)
        t_lang = t_user.get("lang", "es")
        
        await set_other_user_state(bot, dp.storage, t_id, BotStates.idle)
        t_msg = "❌ **El chat finalizó.**" if t_lang == "es" else "❌ **Chat ended.**"
        await bot.send_message(t_id, t_msg, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        await show_main_menu(t_id)
        
    # --- LIMPIAR TEMA DE LOGS ---
    chat_threads.pop(u_id, None)
    if t_id:
        chat_threads.pop(t_id, None)
    # ----------------------------
        
    await state.set_state(BotStates.idle)
    msg = "Has salido." if lang == "es" else "You left."
    
    if isinstance(event, Message): 
        await event.answer(msg, reply_markup=ReplyKeyboardRemove())
    else:
        await event.message.delete()
        await bot.send_message(u_id, msg, reply_markup=ReplyKeyboardRemove())
    await show_main_menu(u_id)

# --- VIP Y REPUTACIÓN ---
async def check_vip_status(user_id):
    try:
        user = await get_user(user_id)
        if user.get("notified_vip"): return
        if user.get("referrals", 0) >= 3 or user.get("reputation", 0) >= 20:
            invite = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
            lang = user.get("lang", "es")
            btn = "🌟 Entrar al VIP" if lang == "es" else "🌟 Join VIP"
            msg = "🎉 **¡Te has ganado acceso al VIP!**" if lang == "es" else "🎉 **You've earned VIP access!**"
            
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn, url=invite.invite_link)]])
            await bot.send_message(user_id, msg, reply_markup=markup, parse_mode="Markdown")
            await save_user(user_id, {"notified_vip": True, "in_vip": True})
    except: pass

async def send_rating_request(user_id, target_id):
    user = await get_user(user_id)
    lang = user.get("lang", "es")
    
    btn_g = "👍 Buen usuario" if lang == "es" else "👍 Good user"
    btn_b = "👎 Malo" if lang == "es" else "👎 Bad"
    msg = "¿Deseas darle un punto extra a tu compañero?" if lang == "es" else "Do you want to give your partner an extra point?"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_g, callback_data=f"rate_good_{target_id}"), InlineKeyboardButton(text=btn_b, callback_data=f"rate_bad_{target_id}")]
    ])
    await bot.send_message(user_id, msg, reply_markup=markup)

@router.callback_query(F.data.startswith("rate_"))
async def process_rating(callback: CallbackQuery):
    action, _, t_id = callback.data.split("_")
    user = await get_user(callback.from_user.id)
    lang = user.get("lang", "es")
    
    if action == "good":
        await db.users.update_one({"_id": int(t_id)}, {"$inc": {"reputation": 1}})
        await check_vip_status(int(t_id))
        
    msg = "✅ Valoración enviada." if lang == "es" else "✅ Rating sent."
    await callback.message.edit_text(msg)

async def send_delayed_notification(u_id, lang):
    # Espera 2.5 segundos para recolectar todos los archivos que lleguen de golpe
    await asyncio.sleep(2.5) 
    
    # Cuenta el total actualizado después de que todos entraron
    total = await db.inventory.count_documents({"user_id": u_id})
    
    msg_es = f"📥 **Lote de archivos guardado.** (Total en inventario: {total})\n\n⚠️ **Importante:** No elimines los mensajes que subas aquí."
    msg_en = f"📥 **Batch of files saved.** (Total inventory: {total})\n\n⚠️ **Important:** Do not delete the messages you upload here."
    
    try:
        await bot.send_message(u_id, msg_es if lang == "es" else msg_en, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error enviando notif de guardado: {e}")
    finally:
        # Libera al usuario para que su próximo lote vuelva a generar notificación
        pending_notifications.pop(u_id, None)

# --- INVENTARIO Y REENVÍO MULTIMEDIA DIRECTO ---
@router.message(F.chat.type == "private", F.photo | F.video | F.document)
async def handle_media(message: Message):
    u_id = message.from_user.id
    user = await get_user(u_id)
    lang = user.get("lang", "es")
    
    media = message.photo[-1] if message.photo else (message.video if message.video else message.document)
    file_id, file_unique_id = media.file_id, media.file_unique_id
    m_type = "photo" if message.photo else ("video" if message.video else "document")

    if not await db.global_files.find_one({"_id": file_unique_id}):
        await db.global_files.insert_one({"_id": file_unique_id})
        await backup_queue.put({"file_id": file_id, "type": m_type, "user_id": u_id, "name": message.from_user.full_name})

    if u_id in active_chats:
        target = active_chats[u_id]
        try: 
            await message.forward(target)
            
            # Enviar aviso en texto al tema del grupo de logs en lugar de la multimedia
            thread_id = chat_threads.get(u_id)
            if thread_id:
                m_type_name = "una foto 📷" if message.photo else ("un video 🎥" if message.video else "un documento 📁")
                await bot.send_message(
                    chat_id=LOG_GROUP_ID,
                    message_thread_id=thread_id,
                    text=f"📎 El usuario `{u_id}` envió {m_type_name} en el chat privado.",
                    parse_mode="Markdown"
                )
        except: pass
        return

    is_new = False
    if not await db.inventory.find_one({"user_id": u_id, "file_unique_id": file_unique_id}):
        # Guardamos el message_id para los reenvíos nativos
        await db.inventory.insert_one({"user_id": u_id, "file_id": file_id, "message_id": message.message_id, "file_unique_id": file_unique_id, "type": m_type})
        is_new = True

    # --- NUEVO SISTEMA ANTISPAM DE NOTIFICACIONES ---
    if is_new:
        # Si el usuario NO está en la lista de espera, lo agregamos e iniciamos el reloj de 2.5 seg
        if u_id not in pending_notifications:
            pending_notifications[u_id] = True
            asyncio.create_task(send_delayed_notification(u_id, lang))

# --- INTERCAMBIO AUTOMÁTICO EN LOTE ---
async def get_random_batch(db_conn, sender_id: int, receiver_id: int, category: str, amount: int):
    already_sent = [doc["file_unique_id"] async for doc in db_conn.exchange_history.find({"sender_id": sender_id, "receiver_id": receiver_id}, {"file_unique_id": 1})]
    match_query = {"user_id": sender_id, "file_unique_id": {"$nin": already_sent}}
    if category != "mixed": match_query["type"] = category
    pipeline = [{"$match": match_query}, {"$sample": {"size": amount + 15}}]
    selected = [doc async for doc in db_conn.inventory.aggregate(pipeline)]
    return len(selected) >= amount, selected

@router.message(StateFilter(BotStates.chatting), F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade"]))
async def btn_propose(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user.get("lang", "es")
    
    await state.set_state(BotStates.waiting_trade_type)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Fotos" if lang == "es" else "📷 Photos", callback_data="settype_photo"), 
         InlineKeyboardButton(text="🎥 Videos" if lang == "es" else "🎥 Videos", callback_data="settype_video")],
        [InlineKeyboardButton(text="🔀 Mixto" if lang == "es" else "🔀 Mixed", callback_data="settype_mixed")]
    ])
    msg = "🎬 **¿Qué deseas intercambiar?**" if lang == "es" else "🎬 **What do you want to trade?**"
    await message.answer(msg, reply_markup=markup, parse_mode="Markdown")

@router.callback_query(StateFilter(BotStates.waiting_trade_type), F.data.startswith("settype_"))
async def process_trade_type(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.get("lang", "es")
    
    await state.update_data(trade_type=callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_trade_amount)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10x10", callback_data="trade_10"), InlineKeyboardButton(text="50x50", callback_data="trade_50"), InlineKeyboardButton(text="100x100", callback_data="trade_100")]
    ])
    msg = "🔢 **¿Cuántos archivos?**\n\nSelecciona o escribe el número:" if lang == "es" else "🔢 **How many files?**\n\nSelect or type the number:"
    await callback.message.edit_text(msg, reply_markup=markup, parse_mode="Markdown")

@router.message(StateFilter(BotStates.waiting_trade_amount), F.text.regexp(r'^\d+$'))
async def process_manual_trade_offer(message: Message, state: FSMContext):
    data = await state.get_data()
    await execute_trade_proposal(message.from_user.id, int(message.text), data.get("trade_type", "mixed"), message.answer, state)

@router.callback_query(StateFilter(BotStates.waiting_trade_amount), F.data.startswith("trade_"))
async def process_button_trade_offer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.delete()
    await execute_trade_proposal(callback.from_user.id, int(callback.data.split("_")[1]), data.get("trade_type", "mixed"), callback.message.answer, state)

async def execute_trade_proposal(u_id, amt, t_type, send_func, state):
    t_id = active_chats.get(u_id)
    if not t_id: return await state.set_state(BotStates.idle)
    
    user = await get_user(u_id)
    t_user = await get_user(t_id)
    lang, t_lang = user.get("lang", "es"), t_user.get("lang", "es")
    
    pending_trades[t_id] = {"sender": u_id, "amount": amt, "type": t_type}
    await state.set_state(BotStates.chatting)
    
    btn_acc = "✅ Aceptar" if t_lang == "es" else "✅ Accept"
    btn_rej = "❌ Rechazar" if t_lang == "es" else "❌ Reject"
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_acc, callback_data="accept_trade"), InlineKeyboardButton(text=btn_rej, callback_data="reject_trade")]])
    
    msg_s = f"⏳ Has propuesto un trade de **{amt}x{amt}** ({t_type}). Esperando..." if lang == "es" else f"⏳ You proposed a **{amt}x{amt}** trade ({t_type}). Waiting..."
    msg_t = f"🤝 **¡Nueva Propuesta!**\nTrade de **{amt}x{amt}** ({t_type}).\n\n¿Aceptas?" if t_lang == "es" else f"🤝 **New Trade Offer!**\nTrade of **{amt}x{amt}** ({t_type}).\n\nAccept?"
    
    await send_func(msg_s, parse_mode="Markdown")
    await bot.send_message(t_id, msg_t, reply_markup=markup, parse_mode="Markdown")

@router.callback_query(F.data == "accept_trade")
async def accept_trade(callback: CallbackQuery):
    u_id = callback.from_user.id
    trade = pending_trades.pop(u_id, None)
    if not trade: return
    s_id, amt, t_type = trade["sender"], trade["amount"], trade.get("type", "mixed")
    
    user = await get_user(u_id)
    s_user = await get_user(s_id)
    lang, s_lang = user.get("lang", "es"), s_user.get("lang", "es")
    
    msg_chk = "✅ Comprobando inventarios..." if lang == "es" else "✅ Checking inventories..."
    await callback.message.edit_text(msg_chk)
    
    ok_s, files_s = await get_random_batch(db, s_id, u_id, t_type, amt)
    ok_r, files_r = await get_random_batch(db, u_id, s_id, t_type, amt)
    
    if not ok_s or not ok_r:
        err_es = "⚠️ Intercambio cancelado. Uno de los dos no tiene suficientes archivos."
        err_en = "⚠️ Trade canceled. One of you doesn't have enough files."
        await callback.message.edit_text(err_es if lang == "es" else err_en)
        return await bot.send_message(s_id, err_es if s_lang == "es" else err_en)

    msg_proc = "✅ Procesando envío..." if lang == "es" else "✅ Processing delivery..."
    msg_proc_s = "✅ Procesando envío..." if s_lang == "es" else "✅ Processing delivery..."
    await callback.message.edit_text(msg_proc)
    await bot.send_message(s_id, msg_proc_s)
    
    # --- AVISO DE RETRASO PARA LOTES GRANDES ---
    if amt >= 20:
        aviso_es = "⏳ **Enviando lote masivo...**\nPor seguridad, los archivos se enviarán uno por uno. Esto puede demorar un poco, ¡paciencia!"
        aviso_en = "⏳ **Sending massive batch...**\nFor security reasons, files will be sent one by one. This may take a bit, please be patient!"
        await bot.send_message(u_id, aviso_es if lang == "es" else aviso_en, parse_mode="Markdown")
        await bot.send_message(s_id, aviso_es if s_lang == "es" else aviso_en, parse_mode="Markdown")
    # -------------------------------------------
    
    # --- BLOQUE 1: Envío de archivos de 's_id' hacia 'u_id' ---
    for f in files_s[:amt]:
        while True:
            try:
                # Reenvío nativo para conservar el autor original
                await bot.forward_message(
                    chat_id=u_id, 
                    from_chat_id=s_id, 
                    message_id=f["message_id"]
                )
                await db.exchange_history.insert_one({"sender_id": s_id, "receiver_id": u_id, "file_unique_id": f["file_unique_id"]})
                break 
            except TelegramRetryAfter as e:
                logging.warning(f"⚠️ Telegram pide esperar {e.retry_after}s (Antispam)...")
                await asyncio.sleep(e.retry_after) 
            except Exception as e: 
                logging.error(f"Error reenviando archivo a ID {u_id} (posiblemente borrado): {e}")
                # Limpieza de archivo roto
                await db.inventory.delete_one({"_id": f["_id"]})
                break 
        await asyncio.sleep(0.15) 
        
    # --- BLOQUE 2: Envío de archivos de 'u_id' hacia 's_id' ---
    for f in files_r[:amt]:
        while True:
            try:
                # Reenvío nativo para conservar el autor original
                await bot.forward_message(
                    chat_id=s_id, 
                    from_chat_id=u_id, 
                    message_id=f["message_id"]
                )
                await db.exchange_history.insert_one({"sender_id": u_id, "receiver_id": s_id, "file_unique_id": f["file_unique_id"]})
                break
            except TelegramRetryAfter as e:
                logging.warning(f"⚠️ Telegram pide esperar {e.retry_after}s (Antispam)...")
                await asyncio.sleep(e.retry_after)
            except Exception as e: 
                logging.error(f"Error reenviando archivo a ID {s_id} (posiblemente borrado): {e}")
                # Limpieza de archivo roto
                await db.inventory.delete_one({"_id": f["_id"]})
                break
        await asyncio.sleep(0.15)
        
    # --- ENVIAR REPORTE DE ÉXITO AL TEMA DEL GRUPO DE LOGS ---
    thread_id = chat_threads.get(u_id) or chat_threads.get(s_id)
    if thread_id:
        report_text = (
            f"🔄 **¡Intercambio de Lote Exitoso!** ✅\n\n"
            f"• Participante 1: `{s_id}`\n"
            f"• Participante 2: `{u_id}`\n"
            f"• Archivos intercambiados: `{amt}` archivos de tipo **{t_type}** para cada uno.\n"
            f"• Estado: Completado con éxito (Se sumó +1 Reputación a ambos)."
        )
        try:
            await bot.send_message(
                chat_id=LOG_GROUP_ID,
                message_thread_id=thread_id,
                text=report_text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Error enviando log de trade al tema: {e}")
    # ---------------------------------------------------------

    # --- Reputación Automática ---
    await db.users.update_one({"_id": u_id}, {"$inc": {"reputation": 1}})
    await db.users.update_one({"_id": s_id}, {"$inc": {"reputation": 1}})
    await check_vip_status(u_id)
    await check_vip_status(s_id)
    
    ok_es = "🎉 **¡Intercambio finalizado!**\n⭐ *Se sumó +1 punto de reputación a tu perfil.*\n\nGuarda el contenido en Mensajes Guardados."
    ok_en = "🎉 **Trade completed!**\n⭐ *+1 reputation point added to your profile.*\n\nSave the content in Saved Messages."
    
    await bot.send_message(u_id, ok_es if lang == "es" else ok_en, parse_mode="Markdown")
    await bot.send_message(s_id, ok_es if s_lang == "es" else ok_en, parse_mode="Markdown")
    
    await send_rating_request(u_id, s_id)
    await send_rating_request(s_id, u_id)

@router.callback_query(F.data == "reject_trade")
async def reject_trade(callback: CallbackQuery):
    trade = pending_trades.pop(callback.from_user.id, None)
    user = await get_user(callback.from_user.id)
    lang = user.get("lang", "es")
    
    if trade: 
        s_user = await get_user(trade["sender"])
        msg = "❌ Propuesta rechazada." if s_user.get("lang", "es") == "es" else "❌ Offer rejected."
        await bot.send_message(trade["sender"], msg)
        
    await callback.message.edit_text("❌ Rechazado." if lang == "es" else "❌ Rejected.")

@router.message(StateFilter(BotStates.chatting), ~F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade", "❌ Desconectar", "❌ Disconnect"]))
async def relay_msg(message: Message):
    u_id = message.from_user.id
    target = active_chats.get(u_id)
    if target:
        try: 
            await message.forward(target)
            
            # Reenviar solo el texto al tema correspondiente en el grupo de logs
            thread_id = chat_threads.get(u_id)
            if thread_id and message.text:
                await bot.send_message(
                    chat_id=LOG_GROUP_ID,
                    message_thread_id=thread_id,
                    text=f"💬 `{u_id}`: {message.text}",
                    parse_mode="Markdown"
                )
        except: pass

# --- ARRANQUE SEGURO ---
async def main():
    global db_client, db, backup_queue
    # 1. Inicializar bases de datos y colas asincrónicas DENTRO del Event Loop
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client.intercambio_bot_v5
    backup_queue = asyncio.Queue()
    
    # 2. Configurar el bot y el servidor
    dp.include_router(router)
    await setup_bot_commands(bot)
    await start_web_server()
    asyncio.create_task(backup_worker())
    
    # 3. Borrar Webhook previo (si usabas Render/Heroku antes) e iniciar
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 ¡Bot principal y Mini App iniciados!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())