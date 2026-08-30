import os
import asyncio
import logging
import time
import random
import hmac
import hashlib
from urllib.parse import parse_qsl
from urllib.parse import unquote
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    BotCommand, BotCommandScopeDefault, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, WebAppInfo
)
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient


# --- CONFIGURACIÓN PRINCIPAL ---
MAIN_BOT_TOKEN = "8948368352:AAHR111IyIDehtbbEdQRKvWLtKhQ8Jmhqgg"
MONGO_URI = "mongodb+srv://carlosjrpelegrina_db_user:1DNyN9AFa9bh1tCr@cluster0.haf2f1l.mongodb.net"

FORCE_SUB_CHANNEL_ID = -1003569446457 
FORCE_SUB_CHANNEL_LINK = "https://t.me/+PMymPYL1k2hlOGNh"
VIP_GROUP_ID = -1004303886159 

ADMIN_IDS = [8983189714, 7452819858]
SUPER_ADMIN_IDS = ADMIN_IDS  

bot = Bot(token=MAIN_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client.intercambio_bot_v4

active_chats = {}
waiting_list = []
pending_trades = {}
backup_queue = asyncio.Queue()
processed_albums = set()

class BotStates(StatesGroup):
    idle = State()
    searching = State()
    chatting = State()
    waiting_trade_type = State()
    waiting_trade_amount = State()
    waiting_for_id = State()

# --- FUNCIONES AUXILIARES ---
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

def validate_init_data(init_data: str):
    try:
        parsed_vals = {}
        for part in init_data.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                parsed_vals[k] = unquote(v)
        
        if 'hash' not in parsed_vals:
            return False
            
        hash_val = parsed_vals.pop('hash')
        
        # Ordenar alfabéticamente y construir el string de comprobación oficial
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_vals.items()))
        
        # Orden correcto de llaves para el HMAC de Telegram
        secret_key = hmac.new(b"WebAppData", MAIN_BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        return calculated_hash == hash_val
    except Exception as e:
        logging.error(f"Error validando init_data: {e}")
        return False

async def get_auth_user(request):
    init_data = request.headers.get("Authorization", "")
    
    # Si hay init_data, validamos y extraemos el ID del usuario de manera segura
    if init_data:
        if validate_init_data(init_data):
            try:
                parsed = dict(parse_qsl(init_data))
                import json
                user_data = json.loads(parsed.get('user', '{}'))
                return user_data.get('id')
            except: 
                pass
        else:
            logging.warning("⚠️ Falló la validación estricta de initData de Telegram.")

    # Respaldo (Fallback) temporal si estás probando desde ciertos navegadores o entornos de desarrollo
    try:
        query_id = int(request.query.get("id", 0))
        if query_id: 
            return query_id
    except: 
        pass
        
    return None

async def api_get_data(request):
    user_id = await get_auth_user(request)
    if not user_id: return web.json_response({"error": "Unauthorized"}, status=401)
    
    user = await get_user(user_id)
    fotos = await db.inventory.count_documents({"user_id": user_id, "type": "photo"})
    videos = await db.inventory.count_documents({"user_id": user_id, "type": "video"})
    
    now = time.time()
    
    # 1. Limpiar ofertas viejas (Mayores a 24 horas = 86400 segundos)
    await db.offers.delete_many({"time": {"$lt": now - 86400}})
    
    # 2. Leaderboard Semanal (Top 10)
    top_users = []
    async for u in db.users.find().sort("reputation", -1).limit(10):
        if u.get("reputation", 0) > 0:
            top_users.append({"id": u["_id"], "rep": u.get("reputation", 0)})
        
    # 3. Mercado (Últimas ofertas activas)
    offers = []
    async for o in db.offers.find().sort("time", -1).limit(20):
        offers.append({"user_id": o["user_id"], "name": o["name"], "text": o["text"], "time": o.get("time", now)})
    
    # 4. Cálculo de Tiempos y Cooldowns
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
    # SE USA STRING ESTÁNDAR TRIPLE (NO f-string) PARA NO ROMPER EL CSS/JS
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
                --bg: #0d1117;
                --card-bg: #161b22;
                --card-border: #30363d;
                --text: #c9d1d9;
                --text-strong: #ffffff;
                --hint: #8b949e;
                --accent: #58a6ff;
                --accent-hover: #3182ce;
                --accent-txt: #ffffff;
                --danger: #f85149;
                --success: #2ea043;
                --gold: #e3b341;
                --gradient-gold: linear-gradient(135deg, #f9d423 0%, #ff4e50 100%);
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Poppins', sans-serif; }
            body { background: var(--bg); color: var(--text); padding: 16px; padding-bottom: 24px; }
            
            .header { text-align: center; margin-bottom: 20px; margin-top: 10px; display: flex; align-items: center; justify-content: center; gap: 10px; }
            .header h1 { font-size: 24px; font-weight: 800; color: var(--text-strong); text-transform: uppercase; letter-spacing: 1px; }
            .header-icon { font-size: 28px; color: var(--accent); }

            .tabs { display: flex; background: var(--card-bg); border-radius: 14px; padding: 6px; margin-bottom: 24px; overflow-x: auto; border: 1px solid var(--card-border); scrollbar-width: none; }
            .tabs::-webkit-scrollbar { display: none; }
            .tab { flex: none; width: 32%; text-align: center; padding: 12px 6px; font-size: 14px; font-weight: 600; color: var(--hint); cursor: pointer; border-radius: 10px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); display: flex; flex-direction: column; align-items: center; gap: 4px; }
            .tab.active { background: var(--accent); color: var(--accent-txt); box-shadow: 0 4px 12px rgba(88, 166, 255, 0.3); transform: translateY(-2px); }

            .section { display: none; flex-direction: column; gap: 16px; }
            .section.active { display: flex; animation: slideUp 0.4s ease-out forwards; }
            @keyframes slideUp { from { opacity: 0; transform: translateY(15px); } to { opacity: 1; transform: translateY(0); } }

            .card { background: var(--card-bg); border-radius: 16px; padding: 20px; border: 1px solid var(--card-border); box-shadow: 0 4px 6px rgba(0,0,0,0.1); position: relative; overflow: hidden; }
            .card-title { font-size: 16px; font-weight: 700; color: var(--text-strong); margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
            .card-title i { color: var(--accent); }
            .flex-between { display: flex; align-items: center; justify-content: space-between; }
            
            .value { font-size: 24px; font-weight: 800; color: var(--text-strong); }
            .progress-container { margin-top: 12px; }
            .progress-bg { background: rgba(255,255,255,0.05); border-radius: 10px; height: 14px; overflow: hidden; width: 100%; border: 1px solid rgba(255,255,255,0.1); }
            .progress-fill { background: var(--gradient-gold); height: 100%; width: 0%; transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1); }
            .progress-text { font-size: 12px; color: var(--hint); margin-top: 6px; display: block; text-align: right; }

            .btn-main { background: var(--accent); color: var(--accent-txt); border: none; border-radius: 12px; padding: 14px; width: 100%; font-size: 15px; font-weight: 700; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; justify-content: center; gap: 8px; }
            .btn-main:active { transform: scale(0.97); }
            .btn-main:disabled { background: var(--card-border); color: var(--hint); cursor: not-allowed; transform: none; box-shadow: none; }
            .btn-outline { background: transparent; border: 2px solid var(--accent); color: var(--accent); }
            .btn-danger { background: rgba(248, 81, 73, 0.1); color: var(--danger); border: 1px solid var(--danger); }

            /* COFRES CSS */
            .chests-container { display: flex; justify-content: center; gap: 15px; margin: 20px 0; perspective: 1000px; }
            .chest-wrapper { width: 90px; height: 90px; position: relative; cursor: pointer; transition: transform 0.3s; }
            .chest-wrapper:hover { transform: translateY(-5px) scale(1.05); }
            .chest-wrapper.disabled { pointer-events: none; opacity: 0.5; filter: grayscale(100%); }
            .chest-img { width: 100%; height: 100%; object-fit: contain; filter: drop-shadow(0 10px 8px rgba(0,0,0,0.5)); transition: all 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275); }
            .chest-wrapper.open .chest-img { transform: scale(1.1) rotate(5deg); filter: drop-shadow(0 0 15px var(--gold)); }
            .chest-prize { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%) scale(0); font-size: 28px; font-weight: 900; color: #fff; text-shadow: 0 0 10px var(--gold), 0 2px 4px rgba(0,0,0,0.8); z-index: 10; opacity: 0; transition: all 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275); }
            .chest-wrapper.open .chest-prize { transform: translate(-50%, -120%) scale(1); opacity: 1; }
            .chest-glow { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 0; height: 0; background: radial-gradient(circle, rgba(255,215,0,0.8) 0%, rgba(255,215,0,0) 70%); border-radius: 50%; z-index: 0; opacity: 0; transition: all 0.5s; pointer-events: none; }
            .chest-wrapper.open .chest-glow { width: 150px; height: 150px; opacity: 1; animation: pulseGlow 2s infinite alternate; }
            @keyframes pulseGlow { 0% { opacity: 0.6; transform: translate(-50%, -50%) scale(0.9); } 100% { opacity: 1; transform: translate(-50%, -50%) scale(1.1); } }

            /* MERCADO & RANKING */
            .list-item { background: rgba(255,255,255,0.03); padding: 16px; border-radius: 12px; margin-bottom: 12px; border: 1px solid var(--card-border); transition: transform 0.2s; position: relative; overflow: hidden; }
            .list-item:hover { transform: translateX(4px); background: rgba(255,255,255,0.06); }
            .list-item::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--accent); border-radius: 4px 0 0 4px; }
            .list-item.rank-1::before { background: var(--gradient-gold); }
            .list-item.rank-2::before { background: #c0c0c0; }
            .list-item.rank-3::before { background: #cd7f32; }
            
            .item-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
            .item-name { font-weight: 700; color: var(--text-strong); font-size: 14px; display: flex; align-items: center; gap: 6px; }
            .item-time { font-size: 11px; color: var(--hint); background: rgba(255,255,255,0.1); padding: 2px 8px; border-radius: 10px; }
            .item-text { font-size: 13px; color: var(--text); line-height: 1.4; margin-bottom: 12px; padding: 8px; background: rgba(0,0,0,0.2); border-radius: 8px; border-left: 2px solid var(--accent); }
            
            .copy-btn { background: rgba(88, 166, 255, 0.1); border: 1px solid rgba(88, 166, 255, 0.3); color: var(--accent); padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; gap: 4px; }
            .copy-btn:hover { background: var(--accent); color: var(--accent-txt); }
            
            .input-group { display: flex; gap: 10px; margin-bottom: 12px; }
            input[type="text"] { flex: 1; width: 100%; padding: 14px; border-radius: 12px; border: 1px solid var(--card-border); background: rgba(0,0,0,0.2); color: var(--text-strong); font-family: 'Poppins'; font-size: 14px; margin-bottom: 10px; }
            input[type="text"]:focus { outline: none; border-color: var(--accent); }
            
            /* Top Banner Weekly */
            .weekly-banner { background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%); border-radius: 12px; padding: 16px; margin-bottom: 20px; text-align: center; box-shadow: 0 4px 15px rgba(59, 130, 246, 0.3); position: relative; overflow: hidden; }
            .weekly-title { color: #fff; font-weight: 800; font-size: 18px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 1px; }
            .weekly-subtitle { color: rgba(255,255,255,0.8); font-size: 12px; }
            
            .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
            .stat-box { background: rgba(0,0,0,0.2); border: 1px solid var(--card-border); border-radius: 12px; padding: 16px; text-align: center; }
            .stat-icon { font-size: 24px; margin-bottom: 8px; display: inline-block; }
            .stat-label { font-size: 12px; color: var(--hint); text-transform: uppercase; font-weight: 600; margin-bottom: 4px; }
            .stat-val { font-size: 20px; font-weight: 800; color: var(--text-strong); }
        </style>
    </head>
    <body>
        <div class="header">
            <i class="fa-solid fa-bolt header-icon"></i>
            <h1>Exchange Hub</h1>
        </div>

        <div class="tabs">
            <div class="tab active" onclick="switchTab('stats', this)"><i class="fa-solid fa-crown"></i> VIP</div>
            <div class="tab" onclick="switchTab('cofres', this)"><i class="fa-solid fa-gem"></i> Bonus</div>
            <div class="tab" onclick="switchTab('mercado', this)"><i class="fa-solid fa-store"></i> Market</div>
            <div class="tab" onclick="switchTab('rank', this)"><i class="fa-solid fa-trophy"></i> Top</div>
            <div class="tab" onclick="switchTab('inventory', this)"><i class="fa-solid fa-box-archive"></i> Cofre</div>
        </div>

        <div id="stats" class="section active">
            <div class="card">
                <div class="card-title"><i class="fa-solid fa-ranking-star"></i> Progreso VIP</div>
                <div class="flex-between">
                    <span style="font-size:13px; color:var(--hint);">Reputación Actual</span>
                    <span class="value" id="vip-text">--/20</span>
                </div>
                <div class="progress-container">
                    <div class="progress-bg"><div class="progress-fill" id="vip-fill"></div></div>
                    <span class="progress-text">Necesitas 20 pts o 3 referidos</span>
                </div>
            </div>
            
            <div class="card">
                <div class="card-title"><i class="fa-solid fa-users"></i> Programa de Referidos</div>
                <div class="stat-grid" style="grid-template-columns: 1fr;">
                    <div class="stat-box" style="display: flex; justify-content: space-between; align-items: center; padding: 12px 20px;">
                        <span class="stat-label" style="margin:0;">Amigos Invitados</span>
                        <span class="stat-val" style="color: var(--accent);"><span id="ref-count">0</span>/3</span>
                    </div>
                </div>
                <button class="btn-main btn-outline" onclick="copyRefLink()"><i class="fa-solid fa-link"></i> Copiar mi Link de Invitación</button>
            </div>
        </div>

        <div id="cofres" class="section">
            <div class="card" style="text-align: center; padding: 30px 20px;">
                <div class="card-title" style="justify-content: center; font-size: 18px; margin-bottom: 5px;"><i class="fa-solid fa-gift" style="color: var(--gold);"></i> Recompensa Diaria</div>
                <p style="font-size:13px; color:var(--hint); margin-bottom: 20px;">Elige un cofre para descubrir tu bono (1-5 pts).</p>
                
                <div class="chests-container" id="chests-container"></div>
                
                <div id="bonus-status" style="font-size:14px; font-weight:700; margin-top: 20px; color: var(--hint);">Calculando...</div>
            </div>
        </div>

        <div id="mercado" class="section">
            <div class="card">
                <div class="card-title"><i class="fa-solid fa-bullhorn"></i> Publicar Oferta</div>
                <p style="font-size:12px; color:var(--hint); margin-bottom:12px;">Se borran a las 24h. Límite: 1 por hora.</p>
                <div class="input-group">
                    <input type="text" id="offer-input" placeholder="Ej: Busco videos, ofrezco 50 fotos..." maxlength="120">
                </div>
                <button class="btn-main" onclick="postOffer()" id="btn-post-offer"><i class="fa-solid fa-paper-plane"></i> Publicar en el Mercado</button>
                <div id="offer-cooldown" style="font-size: 11px; color: var(--danger); text-align: center; margin-top: 8px; display: none;">Debe esperar para publicar de nuevo.</div>
            </div>
            
            <div class="card" style="padding: 16px;">
                <div class="card-title" style="margin-bottom:16px;"><i class="fa-solid fa-store"></i> Mercado en Vivo</div>
                <div id="offers-list"><div style="text-align:center; padding: 20px; color: var(--hint);">Cargando ofertas...</div></div>
            </div>
        </div>

        <div id="rank" class="section">
            <div class="weekly-banner">
                <div class="weekly-title">🏆 Top 10 Semanal</div>
                <div class="weekly-subtitle">Los traders con mejor reputación de la semana</div>
            </div>
            
            <div class="card" style="padding: 12px;">
                <div id="ranking-list"><div style="text-align:center; padding: 20px; color: var(--hint);">Cargando ranking...</div></div>
            </div>
        </div>

        <div id="inventory" class="section">
            <div class="card">
                <div class="card-title"><i class="fa-solid fa-vault"></i> Tu Caja Fuerte</div>
                <div class="stat-grid" style="margin-top: 16px;">
                    <div class="stat-box"><span class="stat-icon">📷</span><div class="stat-label">Fotos</div><div class="stat-val" id="photo-count">--</div></div>
                    <div class="stat-box"><span class="stat-icon">🎥</span><div class="stat-label">Videos</div><div class="stat-val" id="video-count">--</div></div>
                </div>
                <button class="btn-main btn-danger" onclick="clearInventory()"><i class="fa-solid fa-trash-can"></i> Vaciar mi Inventario</button>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            tg.setHeaderColor('#0d1117');
            tg.setBackgroundColor('#0d1117');
            
            let user = tg.initDataUnsafe?.user;
            let userId = user?.id || 0;
            let botUsername = "BOT_USERNAME_PLACEHOLDER"; 

            let reqHeaders = {
                "Content-Type": "application/json",
                "Authorization": tg.initData || ""
            };

            function switchTab(tabId, element) {
                tg.HapticFeedback.impactOccurred('light');
                document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                element.classList.add('active');
            }

            // --- LÓGICA DE COFRES ---
            const chestImageUrl = "https://cdn3d.iconscout.com/3d/premium/thumb/treasure-box-4993548-4161745.png";
            const chestOpenUrl = "https://cdn3d.iconscout.com/3d/premium/thumb/open-treasure-box-4993550-4161747.png";
            
            let chestsContainer = document.getElementById('chests-container');
            let isBonusReady = false;
            
            function initChests(ready) {
                isBonusReady = ready;
                chestsContainer.innerHTML = "";
                
                for(let i=0; i<3; i++) {
                    let wrapper = document.createElement('div');
                    wrapper.className = `chest-wrapper ${ready ? '' : 'disabled'}`;
                    wrapper.onclick = () => ready ? openChest(wrapper) : null;
                    
                    let glow = document.createElement('div');
                    glow.className = 'chest-glow';
                    
                    let img = document.createElement('img');
                    img.src = chestImageUrl;
                    img.className = 'chest-img';
                    
                    let prize = document.createElement('div');
                    prize.className = 'chest-prize';
                    prize.innerText = "+?";
                    
                    wrapper.appendChild(glow);
                    wrapper.appendChild(img);
                    wrapper.appendChild(prize);
                    chestsContainer.appendChild(wrapper);
                }
            }

            let bonusTimer;
            function updateBonusUI(timeLeft) {
                let txt = document.getElementById("bonus-status");
                clearInterval(bonusTimer);
                
                if (timeLeft <= 0) {
                    if(!isBonusReady) initChests(true);
                    txt.innerText = "¡Toca un cofre para reclamar!";
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

            async function openChest(selectedWrapper) {
                if(!isBonusReady) return;
                tg.HapticFeedback.impactOccurred('medium');
                
                document.querySelectorAll('.chest-wrapper').forEach(w => {
                    w.classList.add('disabled');
                    w.onclick = null;
                });
                
                let txt = document.getElementById("bonus-status");
                txt.innerText = "Abriendo...";
                txt.style.color = "var(--gold)";
                
                try {
                    let res = await fetch(`/api/bonus?id=${userId}`, { method: "POST", headers: reqHeaders, body: JSON.stringify({}) });
                    let data = await res.json();
                    
                    if(data.success) {
                        let targetNum = data.bonus;
                        
                        selectedWrapper.classList.remove('disabled');
                        selectedWrapper.classList.add('open');
                        selectedWrapper.querySelector('.chest-img').src = chestOpenUrl;
                        selectedWrapper.querySelector('.chest-prize').innerText = `+${targetNum}`;
                        
                        tg.HapticFeedback.notificationOccurred('success');
                        confetti({ particleCount: 150, spread: 80, origin: { y: 0.5 }, colors: ['#FFD700', '#FFA500', '#FFFFFF'] });
                        txt.innerText = `¡Has ganado ${targetNum} Puntos!`;
                        
                        setTimeout(() => {
                            tg.showAlert(`🎉 ¡Felicidades!\nEncontraste ${targetNum} Puntos de Reputación en el cofre.`);
                            loadData();
                        }, 2500); 
                    } else {
                        tg.showAlert("⚠️ Aún debes esperar.");
                        loadData(); 
                    }
                } catch(e) {
                    tg.showAlert("❌ Error de red.");
                    loadData();
                }
            }

            function timeAgo(timestamp) {
                const seconds = Math.floor(Date.now() / 1000 - timestamp);
                if (seconds < 60) return "hace instantes";
                const minutes = Math.floor(seconds / 60);
                if (minutes < 60) return `hace ${minutes}m`;
                const hours = Math.floor(minutes / 60);
                if (hours < 24) return `hace ${hours}h`;
                return `hace ${Math.floor(hours / 24)}d`;
            }

            async function loadData() {
                if (!userId) return;
                try {
                    let res = await fetch(`/api/data?id=${userId}`, { headers: reqHeaders });
                    if(res.status !== 200) return;
                    let data = await res.json();
                    
                    document.getElementById("photo-count").innerText = data.fotos;
                    document.getElementById("video-count").innerText = data.videos;
                    
                    let rep = data.reputation;
                    document.getElementById("vip-text").innerText = `${rep}/20 Pts`;
                    document.getElementById("vip-fill").style.width = Math.min(100, (rep / 20) * 100) + "%";
                    document.getElementById("ref-count").innerText = data.referrals;
                    
                    updateBonusUI(data.time_left);
                    
                    // Cooldown del mercado
                    let btnPost = document.getElementById("btn-post-offer");
                    let cdText = document.getElementById("offer-cooldown");
                    if (data.offer_cooldown > 0) {
                        btnPost.disabled = true;
                        cdText.style.display = "block";
                        let hm = Math.ceil(data.offer_cooldown / 60);
                        cdText.innerText = `⏳ Próxima publicación en ${hm} min.`;
                    } else {
                        btnPost.disabled = false;
                        cdText.style.display = "none";
                    }
                    
                    // Ranking
                    let rHTML = "";
                    data.leaderboard.forEach((u, i) => {
                        let rankClass = i < 3 ? `rank-${i+1}` : '';
                        let medal = i==0?'👑':i==1?'🥈':i==2?'🥉':`#${i+1}`;
                        let color = i==0?'var(--gold)':i==1?'#c0c0c0':i==2?'#cd7f32':'var(--hint)';
                        
                        rHTML += `
                        <div class="list-item ${rankClass}" style="display:flex; justify-content:space-between; align-items:center;">
                            <div style="display:flex; align-items:center; gap:12px;">
                                <span style="font-size:20px; font-weight:800; width:30px; text-align:center; color:${color};">${medal}</span>
                                <div><div class="item-name"><i class="fa-solid fa-user-ninja" style="font-size:12px; color:var(--hint);"></i> ID: ${u.id}</div></div>
                            </div>
                            <div style="text-align:right;">
                                <div style="font-weight:800; color:var(--text-strong); font-size:16px;">${u.rep}</div>
                                <div style="font-size:10px; color:var(--hint); text-transform:uppercase;">Pts</div>
                            </div>
                        </div>`;
                    });
                    document.getElementById("ranking-list").innerHTML = rHTML || '<div style="text-align:center; padding: 20px; color: var(--hint);">Aún no hay datos esta semana.</div>';

                    // Mercado
                    let oHTML = "";
                    data.offers.forEach(o => {
                        let tAgo = timeAgo(o.time);
                        oHTML += `
                        <div class="list-item">
                            <div class="item-header">
                                <div class="item-name"><i class="fa-solid fa-circle-user"></i> ${o.name}</div>
                                <div class="item-time"><i class="fa-regular fa-clock"></i> ${tAgo}</div>
                            </div>
                            <p class="item-text">${o.text}</p>
                            <div style="display:flex; justify-content:flex-end;">
                                <button class="copy-btn" onclick="copyId(${o.user_id})"><i class="fa-regular fa-copy"></i> Copiar ID</button>
                            </div>
                        </div>`;
                    });
                    document.getElementById("offers-list").innerHTML = oHTML || '<div style="text-align:center; padding: 20px; color: var(--hint);"><i class="fa-solid fa-shop-slash" style="font-size:24px; margin-bottom:8px; display:block;"></i>El mercado está vacío.<br>¡Sé el primero en publicar!</div>';
                    
                } catch(e) { console.error("Error loading data", e); }
            }

            async function postOffer() {
                let val = document.getElementById('offer-input').value.trim();
                if(val.length < 10) return tg.showAlert("⚠️ La oferta es muy corta. Detalla qué buscas y ofreces (min. 10 letras).");
                
                let btn = document.getElementById("btn-post-offer");
                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Publicando...';
                
                tg.HapticFeedback.impactOccurred('light');
                let name = user?.first_name || "Anónimo";
                
                try {
                    let res = await fetch(`/api/offer?id=${userId}`, { 
                        method: "POST", headers: reqHeaders, 
                        body: JSON.stringify({ text: val, name: name }) 
                    });
                    let data = await res.json();
                    
                    if(data.success) {
                        document.getElementById('offer-input').value = "";
                        tg.HapticFeedback.notificationOccurred('success');
                        tg.showAlert("✅ Oferta publicada en el mercado con éxito.");
                    } else { tg.showAlert(data.error || "❌ No se pudo publicar."); }
                } catch(e) { tg.showAlert("❌ Error de red."); }
                
                btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i> Publicar en el Mercado';
                loadData();
            }

            async function clearInventory() {
                tg.showConfirm("⚠️ ¿Estás seguro de vaciar TODAS tus fotos y videos?\n\nEsta acción eliminará permanentemente tu inventario actual.", async (ok) => {
                    if(ok) {
                        try {
                            await fetch(`/api/clear?id=${userId}`, { method: "POST", headers: reqHeaders });
                            tg.HapticFeedback.notificationOccurred('success');
                            tg.showAlert("🗑️ Caja fuerte vaciada correctamente.");
                            loadData();
                        } catch(e) { tg.showAlert("❌ Error de red."); }
                    }
                });
            }

            function copyId(id) {
                tg.HapticFeedback.impactOccurred('light');
                let temp = document.createElement("input");
                temp.value = id;
                document.body.appendChild(temp);
                temp.select();
                document.execCommand("copy");
                document.body.removeChild(temp);
                tg.showAlert(`✅ ID copiado: ${id}\n\nVuelve al menú principal del bot, selecciona '🆔 Conectar ID' y pégalo.`);
            }

            function copyRefLink() {
                tg.HapticFeedback.impactOccurred('light');
                let link = `https://t.me/${botUsername}?start=${userId}`;
                let temp = document.createElement("input");
                temp.value = link;
                document.body.appendChild(temp);
                temp.select();
                document.execCommand("copy");
                document.body.removeChild(temp);
                tg.showAlert("✅ Link de invitación copiado.\n\nEnvíalo a tus amigos o compártelo en grupos para ganar VIP más rápido.");
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
    app.router.add_get("/", handle_webapp)
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
        except Exception as e: print(f"❌ Error en cola: {e}")
        finally: backup_queue.task_done()

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
            await bot.send_message(user["_id"], f"📢 **Aviso:**\n\n{text}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05) 
        except: pass
    await message.answer(f"✅ Difusión completada a `{count}` usuarios.")

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
    
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://tu-servicio.onrender.com")
    webapp_url = f"{base_url}?bot={bot_info.username}"
    
    btn_rnd = "💬 Buscar Chat" if lang == "es" else "💬 Random Chat"
    btn_id = "🆔 Conectar ID" if lang == "es" else "🆔 Connect ID"
    btn_prof = "👤 Mi Perfil" if lang == "es" else "👤 My Profile"
    btn_share = "🔗 Compartir Link" if lang == "es" else "🔗 Share Link"
    btn_panel = "✨ Abrir Panel de Control" if lang == "es" else "✨ Open Dashboard"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_panel, web_app=WebAppInfo(url=webapp_url))],
        [InlineKeyboardButton(text=btn_rnd, callback_data="find_chat"), InlineKeyboardButton(text=btn_id, callback_data="connect_id")],
        [InlineKeyboardButton(text=btn_prof, callback_data="my_profile"), InlineKeyboardButton(text="⚙️ Idioma / Language", callback_data="change_lang")],
        [InlineKeyboardButton(text=btn_share, url=f"https://t.me/share/url?url={my_link}")]
    ])
    
    if lang == "es":
        txt = "👋 **¡Bienvenido a la red de intercambio!**\n\n⚠️ **REQUISITO CLAVE:** Sube material propio a este chat para poder hacer intercambios. ¡Sin videos o fotos en tu inventario, no podrás recibir nada!\n\nUtiliza la nueva **Mini App** para reclamar tu bonus diario y ver tu progreso VIP. 🚀"
    else:
        txt = "👋 **Welcome to the exchange network!**\n\n⚠️ **KEY REQUIREMENT:** Upload your own media to this chat to be able to trade. Without videos or photos in your inventory, you won't receive anything!\n\nUse the new **Mini App** to claim your daily bonus and check your VIP progress. 🚀"
        
    await bot.send_message(chat_id=user_id, text=txt, reply_markup=markup, parse_mode="Markdown")

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
    
    await state.set_state(BotStates.chatting)
    await dp.fsm.resolve_context(bot, t_id, t_id).set_state(BotStates.chatting)
    
    for uid, u_obj in [(u_id, user), (t_id, t_user)]:
        lng = u_obj.get("lang", "es")
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"), KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]], resize_keyboard=True)
        msg = "✅ **Conexión Establecida.**" if lng == "es" else "✅ **Connection Established.**"
        await bot.send_message(uid, msg, reply_markup=kb, parse_mode="Markdown")
        
    await callback.message.delete()

@router.callback_query(F.data.startswith("reject_id_"))
async def reject_id_connection(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    t_id = int(callback.data.split("_")[2])
    t_user = await get_user(t_id)
    
    msg1 = "❌ Rechazaste la solicitud." if user.get("lang", "es") == "es" else "❌ You rejected the request."
    msg2 = "❌ Solicitud rechazada." if t_user.get("lang", "es") == "es" else "❌ Request rejected."
    
    await callback.message.edit_text(msg1)
    await bot.send_message(t_id, msg2)

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
        await dp.fsm.resolve_context(bot, t_id, t_id).set_state(BotStates.chatting)
        
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
        
        await dp.fsm.resolve_context(bot, t_id, t_id).set_state(BotStates.idle)
        t_msg = "❌ **El chat finalizó.**" if t_lang == "es" else "❌ **Chat ended.**"
        await bot.send_message(t_id, t_msg, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        await show_main_menu(t_id)
        
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

@router.message(F.chat.id == VIP_GROUP_ID, F.photo | F.video | F.document)
async def vip_group_activity(message: Message):
    await save_user(message.from_user.id, {"in_vip": True})

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
        try: await message.forward(active_chats[u_id])
        except: pass
        return

    is_new = False
    if not await db.inventory.find_one({"user_id": u_id, "file_unique_id": file_unique_id}):
        await db.inventory.insert_one({"user_id": u_id, "file_id": file_id, "message_id": message.message_id, "file_unique_id": file_unique_id, "type": m_type})
        is_new = True

    if is_new:
        group_id = message.media_group_id
        send_reply = True
        if group_id:
            if group_id in processed_albums: send_reply = False
            else:
                processed_albums.add(group_id)
                if len(processed_albums) > 1000: processed_albums.clear()
        
        if send_reply:
            if group_id: await asyncio.sleep(0.5)
            total = await db.inventory.count_documents({"user_id": u_id})
            
            msg_es = f"📥 **Archivo(s) guardado(s).** (Total: {total})\n\n⚠️ **Importante:** No elimines los mensajes que subas aquí."
            msg_en = f"📥 **File(s) saved.** (Total: {total})\n\n⚠️ **Important:** Do not delete the messages you upload here."
            await message.answer(msg_es if lang == "es" else msg_en, parse_mode="Markdown")

# --- INTERCAMBIO AUTOMÁTICO EN LOTE ---
async def get_random_batch(db, sender_id: int, receiver_id: int, category: str, amount: int):
    already_sent = [doc["file_unique_id"] async for doc in db.exchange_history.find({"sender_id": sender_id, "receiver_id": receiver_id}, {"file_unique_id": 1})]
    match_query = {"user_id": sender_id, "file_unique_id": {"$nin": already_sent}}
    if category != "mixed": match_query["type"] = category
    pipeline = [{"$match": match_query}, {"$sample": {"size": amount + 15}}]
    selected = [doc async for doc in db.inventory.aggregate(pipeline)]
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
    
    for f in files_s[:amt]:
        try:
            await bot.forward_message(chat_id=u_id, from_chat_id=s_id, message_id=f["message_id"])
            await db.exchange_history.insert_one({"sender_id": s_id, "receiver_id": u_id, "file_unique_id": f["file_unique_id"]})
        except: await db.inventory.delete_one({"file_unique_id": f["file_unique_id"]})
        await asyncio.sleep(0.05)
        
    for f in files_r[:amt]:
        try:
            await bot.forward_message(chat_id=s_id, from_chat_id=u_id, message_id=f["message_id"])
            await db.exchange_history.insert_one({"sender_id": u_id, "receiver_id": s_id, "file_unique_id": f["file_unique_id"]})
        except: await db.inventory.delete_one({"file_unique_id": f["file_unique_id"]})
        await asyncio.sleep(0.05)
        
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
    target = active_chats.get(message.from_user.id)
    if target:
        try: await message.forward(target)
        except: pass

# --- ARRANQUE ---
async def main():
    dp.include_router(router)
    await setup_bot_commands(bot)
    await start_web_server()
    asyncio.create_task(backup_worker())
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 ¡Bot principal y Mini App iniciados!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())