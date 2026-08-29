import os
import asyncio
import logging
import time
import random
import hmac
import hashlib
from urllib.parse import parse_qsl
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
MAIN_BOT_TOKEN = "8968171126:AAG5whyhyl66PcYqQh3UQy25vZj0RaQG87s"
MONGO_URI = "mongodb+srv://carlosjrpelegrina_db_user:1DNyN9AFa9bh1tCr@cluster0.haf2f1l.mongodb.net"

FORCE_SUB_CHANNEL_ID = -1004421551499 
FORCE_SUB_CHANNEL_LINK = "https://t.me/+Dsix9zoasFExZjUx"
VIP_GROUP_ID = -1004348635427 

ADMIN_IDS = [8983189714, 7452819858]
SUPER_ADMIN_IDS = ADMIN_IDS  

bot = Bot(token=MAIN_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client.intercambio_bot_v3

active_chats = {}
waiting_list = {} # Convertido a dict para manejar idiomas si se desea en el futuro, pero lo usaremos como list
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
        user = {"_id": user_id, "lang": "es", "referrals": 0, "reputation": 0, "mode": "anon", "in_vip": False, "notified_vip": False, "last_bonus": 0}
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
    return list(set(SUPER_ADMIN_IDS + extra_ids))

def validate_init_data(init_data: str):
    try:
        parsed_data = dict(parse_qsl(init_data))
        hash_val = parsed_data.pop('hash')
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", MAIN_BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == hash_val
    except:
        return False

# --- API Y FRONTEND PARA LA MINI APP ---
async def get_auth_user(request):
    init_data = request.headers.get("Authorization", "")
    if not validate_init_data(init_data): return None
    parsed = dict(parse_qsl(init_data))
    import json
    return json.loads(parsed.get('user', '{}')).get('id')

async def api_get_data(request):
    user_id = await get_auth_user(request)
    if not user_id: return web.json_response({"error": "Unauthorized"}, status=401)
    
    user = await get_user(user_id)
    fotos = await db.inventory.count_documents({"user_id": user_id, "type": "photo"})
    videos = await db.inventory.count_documents({"user_id": user_id, "type": "video"})
    
    # Leaderboard (Top 10)
    top_users = []
    async for u in db.users.find().sort("reputation", -1).limit(10):
        top_users.append({"id": u["_id"], "rep": u.get("reputation", 0)})
        
    # Mercado (Últimas 15 ofertas)
    offers = []
    async for o in db.offers.find().sort("time", -1).limit(15):
        offers.append({"user_id": o["user_id"], "name": o["name"], "text": o["text"]})
    
    now = time.time()
    last_bonus = user.get("last_bonus", 0)
    cooldown = 6 * 3600
    time_left = max(0, (last_bonus + cooldown) - now)
    
    return web.json_response({
        "fotos": fotos, "videos": videos,
        "reputation": user.get("reputation", 0),
        "referrals": user.get("referrals", 0),
        "time_left": time_left,
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
    
    data = await request.json()
    text = data.get("text", "").strip()[:100] # Máximo 100 caracteres
    name = data.get("name", "Anónimo")
    if len(text) > 5:
        await db.offers.insert_one({"user_id": user_id, "name": name, "text": text, "time": time.time()})
        return web.json_response({"success": True})
    return web.json_response({"success": False})

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
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg: var(--tg-theme-bg-color, #0f141c);
                --card-bg: var(--tg-theme-secondary-bg-color, #1a2332);
                --text: var(--tg-theme-text-color, #ffffff);
                --hint: var(--tg-theme-hint-color, #8a9ba8);
                --accent: var(--tg-theme-button-color, #2ea6ff);
                --accent-txt: var(--tg-theme-button-text-color, #ffffff);
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
            body { background: var(--bg); color: var(--text); padding: 12px; padding-bottom: 20px; }
            .header { text-align: center; margin-bottom: 16px; margin-top: 10px; }
            .header h1 { font-size: 20px; margin-bottom: 4px; }
            .tabs { display: flex; background: var(--card-bg); border-radius: 12px; padding: 4px; margin-bottom: 16px; overflow-x: auto; }
            .tab { flex: none; width: 33%; text-align: center; padding: 10px 4px; font-size: 13px; font-weight: 600; color: var(--hint); cursor: pointer; border-radius: 8px; transition: 0.3s; }
            .tab.active { background: var(--accent); color: var(--accent-txt); }
            .section { display: none; flex-direction: column; gap: 12px; }
            .section.active { display: flex; animation: fadeIn 0.3s; }
            @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
            .card { background: var(--card-bg); border-radius: 16px; padding: 16px; }
            .flex-between { display: flex; align-items: center; justify-content: space-between; }
            .value { font-size: 20px; font-weight: 700; }
            .progress-bg { background: rgba(255,255,255,0.1); border-radius: 8px; height: 12px; margin-top: 10px; overflow: hidden; width: 100%; }
            .progress-fill { background: linear-gradient(90deg, #FFD700, #FFA500); height: 100%; width: 0%; transition: 0.5s; }
            .btn-main { background: var(--accent); color: var(--accent-txt); border: none; border-radius: 12px; padding: 12px; width: 100%; font-size: 14px; font-weight: bold; cursor: pointer; }
            .btn-danger { background: rgba(255,77,79,0.1); color: #ff4d4f; border: 1px solid #ff4d4f; }
            
            /* RULETA CSS CORREGIDA */
            .roulette-box { background: #111; padding: 20px; border-radius: 16px; text-align: center; border: 2px solid var(--accent); position: relative; }
            .roulette-window { 
                width: 100px; 
                height: 100px; 
                margin: 0 auto 16px auto; 
                background: #222; 
                border-radius: 16px; 
                border: 3px solid #FFD700; 
                overflow: hidden; 
                position: relative; 
                box-shadow: 0 0 15px rgba(255, 215, 0, 0.2);
            }
            .roulette-window::after {
                content: '';
                position: absolute;
                top: 0; left: 0; right: 0; bottom: 0;
                box-shadow: inset 0px 20px 15px -10px rgba(0,0,0,0.9), inset 0px -20px 15px -10px rgba(0,0,0,0.9);
                pointer-events: none;
            }
            .roulette-track { 
                display: flex; 
                flex-direction: column; 
                width: 100%; 
            }
            .roulette-item { 
                width: 100%; 
                height: 100px; 
                flex-shrink: 0; 
                display: flex; 
                align-items: center; 
                justify-content: center; 
                font-size: 42px; 
                font-weight: 900; 
                color: #fff; 
            }
            .pointer { 
                position: absolute; 
                right: -2px; 
                top: 50%; 
                transform: translateY(-50%); 
                width: 0; 
                height: 0; 
                border-top: 12px solid transparent; 
                border-bottom: 12px solid transparent; 
                border-right: 18px solid #FFD700; 
                z-index: 10; 
            }
            
            /* MERCADO & RANKING */
            .list-item { background: rgba(255,255,255,0.05); padding: 12px; border-radius: 12px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; border-left: 3px solid var(--accent); }
            .list-item p { font-size: 13px; margin: 4px 0; color: #ddd; }
            .copy-btn { background: rgba(255,255,255,0.1); border: none; color: #fff; padding: 6px 10px; border-radius: 6px; font-size: 12px; cursor: pointer; }
            input[type="text"] { width: 100%; padding: 12px; border-radius: 8px; border: none; background: rgba(255,255,255,0.1); color: #fff; margin-bottom: 10px; font-family: 'Inter'; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>⚡ Exchange Hub</h1>
        </div>

        <div class="tabs">
            <div class="tab active" onclick="switchTab('stats', this)">📊 VIP</div>
            <div class="tab" onclick="switchTab('ruleta', this)">🎰 Bonus</div>
            <div class="tab" onclick="switchTab('mercado', this)">🛒 Mercado</div>
            <div class="tab" onclick="switchTab('rank', this)">🏆 Top</div>
            <div class="tab" onclick="switchTab('inventory', this)">📦 Cofre</div>
        </div>

        <!-- PESTAÑA VIP / REFERIDOS -->
        <div id="stats" class="section active">
            <div class="card">
                <div class="flex-between">
                    <h3>👑 Progreso hacia VIP</h3>
                    <span class="value" id="vip-text">--/20</span>
                </div>
                <div class="progress-bg"><div class="progress-fill" id="vip-fill"></div></div>
                <p style="font-size:12px; color:var(--hint); margin-top:8px;">Necesitas 20 puntos de Reputación o 3 amigos referidos.</p>
            </div>
            <div class="card">
                <h3>👥 Referidos (<span id="ref-count">0</span>/3)</h3>
                <p style="font-size:12px; color:var(--hint); margin-bottom:12px;">Invita amigos con tu link para ganar VIP al instante.</p>
                <button class="btn-main" onclick="copyRefLink()" style="background: rgba(46, 166, 255, 0.2); color: var(--accent);">🔗 Copiar mi Link de Invitación</button>
            </div>
        </div>

        <!-- PESTAÑA RULETA -->
        <div id="ruleta" class="section">
            <div class="card roulette-box">
                <h3 style="color: #FFD700; margin-bottom: 16px;">🎁 Ruleta de Puntos</h3>
                <div class="roulette-window">
                    <div class="pointer"></div>
                    <div class="roulette-track" id="r-track">
                        <!-- Generado por JS -->
                    </div>
                </div>
                <div id="bonus-status" style="font-size:14px; font-weight:bold; margin-bottom: 12px;">Calculando...</div>
                <button id="btn-spin" class="btn-main" style="background: linear-gradient(90deg, #FFD700, #FFA500); color: #000;" onclick="spinRoulette()" disabled>Tirar Ruleta</button>
            </div>
        </div>

        <!-- PESTAÑA MERCADO -->
        <div id="mercado" class="section">
            <div class="card">
                <h3>📢 Publicar Oferta</h3>
                <input type="text" id="offer-input" placeholder="Ej: Busco videos, ofrezco 50 fotos" maxlength="100">
                <button class="btn-main" onclick="postOffer()">Publicar en el Mercado</button>
            </div>
            <div class="card">
                <h3 style="margin-bottom:12px;">🛒 Mercado Actual</h3>
                <div id="offers-list">Cargando...</div>
            </div>
        </div>

        <!-- PESTAÑA RANKING -->
        <div id="rank" class="section">
            <div class="card">
                <h3 style="margin-bottom:12px;">🏆 Top 10 Reputación</h3>
                <div id="ranking-list">Cargando...</div>
            </div>
        </div>

        <!-- PESTAÑA INVENTARIO -->
        <div id="inventory" class="section">
            <div class="card flex-between">
                <div><h3>📷 Fotos</h3><div class="value" id="photo-count">--</div></div>
                <div><h3>🎥 Videos</h3><div class="value" id="video-count">--</div></div>
            </div>
            <button class="btn-main btn-danger" onclick="clearInventory()">🗑️ Vaciar mi Inventario</button>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            tg.setHeaderColor('#1a2332'); /* Estilo Nativo */
            
            let user = tg.initDataUnsafe?.user;
            let userId = user?.id || 0;
            let botUsername = "BOT_USERNAME_PLACEHOLDER"; 

            // Configurar cabecera segura para llamadas API
            let reqHeaders = {
                "Content-Type": "application/json",
                "Authorization": tg.initData
            };

            function switchTab(tabId, element) {
                tg.HapticFeedback.impactOccurred('light');
                document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                element.classList.add('active');
            }

            // RULETA LOGICA CORREGIDA
            let rTrack = document.getElementById('r-track');
            
            function initRoulette() {
                rTrack.style.transition = 'none';
                rTrack.style.transform = 'translateY(0)';
                rTrack.innerHTML = "";
                
                let startDiv = document.createElement('div');
                startDiv.className = 'roulette-item';
                startDiv.innerText = "🎁";
                rTrack.appendChild(startDiv);

                for(let i=0; i<30; i++) {
                    let div = document.createElement('div');
                    div.className = 'roulette-item';
                    div.innerText = Math.floor(Math.random() * 5) + 1;
                    rTrack.appendChild(div);
                }
            }
            initRoulette();

            let bonusTimer;
            function updateBonusUI(timeLeft) {
                let btn = document.getElementById("btn-spin");
                let txt = document.getElementById("bonus-status");
                clearInterval(bonusTimer);
                if (timeLeft <= 0) {
                    btn.disabled = false;
                    btn.style.opacity = "1";
                    txt.innerText = "¡Tiro Disponible!";
                } else {
                    btn.disabled = true;
                    btn.style.opacity = "0.5";
                    bonusTimer = setInterval(() => {
                        timeLeft--;
                        if (timeLeft <= 0) updateBonusUI(0);
                        else {
                            let h = Math.floor(timeLeft / 3600);
                            let m = Math.floor((timeLeft % 3600) / 60);
                            txt.innerText = `⏳ Próximo tiro en: ${h}h ${m}m`;
                        }
                    }, 1000);
                }
            }

            async function spinRoulette() {
                tg.HapticFeedback.impactOccurred('medium');
                document.getElementById("btn-spin").disabled = true;
                
                let res = await fetch('/api/bonus', { method: "POST", headers: reqHeaders, body: JSON.stringify({}) });
                let data = await res.json();
                
                if(data.success) {
                    let targetNum = data.bonus;
                    
                    let winDiv = document.createElement('div');
                    winDiv.className = 'roulette-item';
                    winDiv.style.color = '#FFD700';
                    winDiv.style.textShadow = '0 0 15px rgba(255, 215, 0, 0.6)';
                    winDiv.innerText = "+" + targetNum;
                    rTrack.appendChild(winDiv);
                    
                    void rTrack.offsetWidth; 
                    
                    let itemHeight = 100;
                    let scrollAmount = -((rTrack.children.length - 1) * itemHeight);
                    
                    rTrack.style.transition = 'transform 3s cubic-bezier(0.15, 0.9, 0.2, 1)';
                    rTrack.style.transform = `translateY(${scrollAmount}px)`;
                    
                    setTimeout(() => {
                        tg.HapticFeedback.notificationOccurred('success');
                        confetti({ particleCount: 150, spread: 70, origin: { y: 0.6 } });
                        tg.showAlert(`🎉 ¡Felicidades!\nHas ganado ${targetNum} Puntos de Reputación.`);
                        loadData();
                        
                        setTimeout(() => { initRoulette(); }, 3000);
                    }, 3000); 
                } else {
                    tg.showAlert("⚠️ Aún debes esperar.");
                    document.getElementById("btn-spin").disabled = false;
                }
            }

            async function loadData() {
                if (!userId) return;
                let res = await fetch('/api/data', { headers: reqHeaders });
                if(res.status !== 200) return;
                let data = await res.json();
                
                document.getElementById("photo-count").innerText = data.fotos;
                document.getElementById("video-count").innerText = data.videos;
                
                let rep = data.reputation;
                document.getElementById("vip-text").innerText = `${rep}/20 Pts`;
                document.getElementById("vip-fill").style.width = Math.min(100, (rep / 20) * 100) + "%";
                document.getElementById("ref-count").innerText = data.referrals;
                
                updateBonusUI(data.time_left);
                
                let rHTML = "";
                data.leaderboard.forEach((u, i) => {
                    let medal = i==0?"🥇":i==1?"🥈":i==2?"🥉":"🏅";
                    rHTML += `<div class="list-item"><b>${medal} ID: ${u.id}</b><span style="color:#FFD700">⭐ ${u.rep}</span></div>`;
                });
                document.getElementById("ranking-list").innerHTML = rHTML || "No hay jugadores aún.";

                let oHTML = "";
                data.offers.forEach(o => {
                    oHTML += `
                    <div class="list-item" style="flex-direction:column; align-items:flex-start;">
                        <div style="width:100%; display:flex; justify-content:space-between; margin-bottom:5px;">
                            <b style="color:var(--accent); font-size:12px;">👤 ${o.name}</b>
                            <button class="copy-btn" onclick="copyId(${o.user_id})">Copiar ID</button>
                        </div>
                        <p>💬 "${o.text}"</p>
                    </div>`;
                });
                document.getElementById("offers-list").innerHTML = oHTML || "El mercado está vacío.";
            }

            async function postOffer() {
                let val = document.getElementById('offer-input').value;
                if(val.length < 5) return tg.showAlert("⚠️ La oferta es muy corta.");
                tg.HapticFeedback.impactOccurred('light');
                let name = user?.first_name || "Anónimo";
                
                await fetch('/api/offer', { 
                    method: "POST", headers: reqHeaders, 
                    body: JSON.stringify({ text: val, name: name }) 
                });
                document.getElementById('offer-input').value = "";
                tg.showAlert("✅ Oferta publicada en el mercado.");
                loadData();
            }

            async function clearInventory() {
                tg.showConfirm("¿Estás seguro de vaciar TODAS tus fotos y videos de tu cofre? Esta acción no se puede deshacer.", async (ok) => {
                    if(ok) {
                        await fetch('/api/clear', { method: "POST", headers: reqHeaders });
                        tg.HapticFeedback.notificationOccurred('success');
                        tg.showAlert("🗑️ Cofre vaciado.");
                        loadData();
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
                tg.showAlert("✅ ID copiado: " + id + "\n\nVuelve al bot, selecciona 'Conectar ID' y pégalo.");
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
                tg.showAlert("✅ Link copiado.\nEnvíalo a tus amigos para ganar VIP rápido.");
            }

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
    await message.answer(f"📊 **ESTADÍSTICAS**\n👥 Usuarios: `{await db.users.count_documents({})}`\n📁 Archivos: `{await db.inventory.count_documents({})}`\n🔄 Intercambios: `{await db.exchange_history.count_documents({})}`\n💬 Chats: `{len(active_chats)//2}`", parse_mode="Markdown")

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
    
    # --- CAMBIO CLAVE PARA LA MINI APP ---
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://tu-servicio.onrender.com")
    # Agregamos "?bot=nombre_del_bot" al final del enlace
    webapp_url = f"{base_url}?bot={bot_info.username}"
    # -------------------------------------
    
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

# --- PERFIL E IDIOMA RESTAURADO ---
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

# AQUÍ ESTÁ LA CORRECCIÓN CLAVE PARA EL FILTRO DEL CHAT (Soporta ambos idiomas)
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