import os
import asyncio
import logging
import time
import random
import json
from urllib.parse import parse_qsl
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    BotCommand, BotCommandScopeDefault, ReplyKeyboardMarkup, KeyboardButton, 
    ReplyKeyboardRemove, WebAppInfo
)

# =====================================================================
# 1. CONFIGURACIÓN DEL PANEL MASTER
# =====================================================================
MASTER_TOKEN = os.getenv("MASTER_TOKEN", "8972664077:AAFZuPYPIFypZN7ovV849CBuRRZYiMrVdDY")
MASTER_MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://carlosjrpelegrina_db_user:1DNyN9AFa9bh1tCr@cluster0.haf2f1l.mongodb.net")
PORT = int(os.environ.get("PORT", 8080))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://TU_DOMINIO.onrender.com")

master_db_client = AsyncIOMotorClient(MASTER_MONGO_URI)
master_db = master_db_client.saas_master_db
active_bots_tasks = {} # Estructura: {bot_id: {"bot": bot, "db": db, "queue": queue, ...}}
master_dp = Dispatcher()

# --- ESTADOS FSM ---
class CreateChildBot(StatesGroup):
    waiting_for_token = State()
    waiting_for_sub_id = State()
    waiting_for_sub_link = State()
    waiting_for_vip_id = State()
    waiting_for_db_version = State()

class BotStates(StatesGroup):
    idle = State()
    searching = State()
    chatting = State()
    waiting_trade_type = State()
    waiting_trade_amount = State()
    waiting_for_id = State()

# =====================================================================
# 2. SERVIDOR WEB Y APIS (ADAPTADO PARA SaaS MULTI-TENANT)
# =====================================================================
async def get_auth_user(request):
    init_data = request.headers.get("Authorization", "")
    if init_data:
        try:
            parsed = dict(parse_qsl(init_data))
            user_obj = json.loads(parsed.get('user', '{}'))
            if 'id' in user_obj: return int(user_obj['id'])
        except: pass
    try:
        query_id = int(request.query.get("id", 0))
        if query_id: return query_id
    except: pass
    return None

def get_child_db(request):
    bot_id = int(request.query.get("bot_id", 0))
    if bot_id in active_bots_tasks:
        return active_bots_tasks[bot_id]["db"]
    return None

def get_child_bot(request):
    bot_id = int(request.query.get("bot_id", 0))
    if bot_id in active_bots_tasks:
        return active_bots_tasks[bot_id]["bot"]
    return None

async def api_live_ping(request):
    user_id = await get_auth_user(request)
    child_db = get_child_db(request)
    bot = get_child_bot(request)
    if not user_id or not child_db or not bot: return web.json_response({"error": "Unauthorized"}, status=401)
    
    # Extraemos el diccionario active_viewers desde la RAM del bot hijo
    dp = active_bots_tasks[bot.id]["polling_task"].get_coro().cr_frame.f_locals['dp']
    active_viewers = dp["active_viewers"]
    
    now = time.time()
    active_viewers[user_id] = now
    
    for uid in list(active_viewers.keys()):
        if now - active_viewers[uid] > 45: del active_viewers[uid]
            
    viewers_count = len(active_viewers)
    user = await child_db.users.find_one({"_id": user_id}) or {}
    current_time = user.get("watch_time", 0) + 30 
    await child_db.users.update_one({"_id": user_id}, {"$set": {"watch_time": current_time}}, upsert=True)
    
    won = False
    if current_time >= 600 and not user.get("in_vip"):
        won = True
        await child_db.users.update_one({"_id": user_id}, {"$set": {"notified_vip": True, "in_vip": True}})
        try:
            bot_config = await master_db.child_bots.find_one({"bot_token": bot.token})
            vip_group_id = int(bot_config.get("vip_group_id", 0)) if bot_config else 0
            if vip_group_id:
                invite = await bot.create_chat_invite_link(chat_id=vip_group_id, member_limit=1)
                msg = "🎉 **¡Misión Cumplida!**\nGracias por quedarte en la transmisión. Como recompensa, aquí tienes tu acceso VIP exclusivo:"
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌟 Entrar al VIP", url=invite.invite_link)]])
                await bot.send_message(user_id, msg, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error enviando VIP por stream: {e}")

    return web.json_response({"success": True, "viewers": viewers_count, "watch_time": current_time, "won": won})

async def api_get_data(request):
    user_id = await get_auth_user(request)
    child_db = get_child_db(request)
    if not user_id or not child_db: return web.json_response({"error": "Unauthorized / Bot Inactivo"}, status=401)
    
    user = await child_db.users.find_one({"_id": user_id}) or {}
    fotos = await child_db.inventory.count_documents({"user_id": user_id, "type": "photo"})
    videos = await child_db.inventory.count_documents({"user_id": user_id, "type": "video"})
    
    now = time.time()
    await child_db.offers.delete_many({"time": {"$lt": now - 86400}})
    
    top_users = []
    async for u in child_db.users.find().sort("reputation", -1).limit(10):
        if u.get("reputation", 0) > 0:
            top_users.append({"id": u["_id"], "rep": u.get("reputation", 0)})
        
    offers = []
    async for o in child_db.offers.find().sort("time", -1).limit(20):
        offers.append({"user_id": o["user_id"], "name": o["name"], "text": o["text"], "time": o.get("time", now), "_id": str(o["_id"])})
    
    last_bonus = user.get("last_bonus", 0)
    time_left_bonus = max(0, (last_bonus + (6 * 3600)) - now)
    last_offer = user.get("last_offer", 0)
    time_left_offer = max(0, (last_offer + 3600) - now)
    
    return web.json_response({
        "fotos": fotos, "videos": videos,
        "reputation": user.get("reputation", 0), "referrals": user.get("referrals", 0),
        "time_left": time_left_bonus, "offer_cooldown": time_left_offer,
        "leaderboard": top_users, "offers": offers
    })

async def api_claim_bonus(request):
    user_id = await get_auth_user(request)
    child_db = get_child_db(request)
    if not user_id or not child_db: return web.json_response({"error": "Unauthorized"}, status=401)
    
    user = await child_db.users.find_one({"_id": user_id}) or {}
    now = time.time()
    last_bonus = user.get("last_bonus", 0)
    cooldown = 6 * 3600
    if now < last_bonus + cooldown: return web.json_response({"success": False, "error": "Cooldown active"})
        
    puntos = random.randint(1, 5)
    nueva_rep = user.get("reputation", 0) + puntos
    await child_db.users.update_one({"_id": user_id}, {"$set": {"last_bonus": now, "reputation": nueva_rep}}, upsert=True)
    return web.json_response({"success": True, "bonus": puntos, "new_rep": nueva_rep, "time_left": cooldown})

async def api_post_offer(request):
    user_id = await get_auth_user(request)
    child_db = get_child_db(request)
    if not user_id or not child_db: return web.json_response({"error": "Unauthorized"}, status=401)
    
    user = await child_db.users.find_one({"_id": user_id}) or {}
    now = time.time()
    if now < user.get("last_offer", 0) + 3600:
        return web.json_response({"success": False, "error": "Espera 1 hora."})
    
    data = await request.json()
    text = data.get("text", "").strip()[:120]
    name = data.get("name", "Anónimo")
    type_o = data.get("type", "mixed")
    
    if len(text) >= 10:
        await child_db.offers.insert_one({"user_id": user_id, "name": name, "text": text, "type": type_o, "time": now})
        await child_db.users.update_one({"_id": user_id}, {"$set": {"last_offer": now}}, upsert=True)
        return web.json_response({"success": True})
    return web.json_response({"success": False, "error": "Oferta muy corta."})

async def api_clear_inv(request):
    user_id = await get_auth_user(request)
    child_db = get_child_db(request)
    if not user_id or not child_db: return web.json_response({"error": "Unauthorized"}, status=401)
    await child_db.inventory.delete_many({"user_id": user_id})
    return web.json_response({"success": True})

async def handle_webapp(request):
    bot_username = request.query.get("bot", "")
    bot_id = request.query.get("bot_id", "")
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
            body { background: var(--bg); color: var(--text); padding: 16px; padding-bottom: 24px; max-width: 500px; margin: 0 auto; }
            .header { text-align: center; margin-bottom: 20px; margin-top: 10px; display: flex; justify-content: center; align-items: center; gap: 10px; }
            .header h1 { font-size: 24px; font-weight: 800; color: var(--text-strong); text-transform: uppercase; }
            .header-icon { font-size: 28px; color: var(--accent); }
            .tabs { display: flex; background: var(--card-bg); border-radius: 14px; padding: 6px; margin-bottom: 24px; overflow-x: auto; border: 1px solid var(--card-border); scrollbar-width: none; gap: 6px; -webkit-overflow-scrolling: touch; }
            .tab { flex: 0 0 28%; text-align: center; padding: 12px 4px; font-size: 13px; font-weight: 600; color: var(--hint); cursor: pointer; display: flex; flex-direction: column; gap: 4px; white-space: nowrap; transition: all 0.2s; }
            .tab.active { background: var(--accent); color: #fff; box-shadow: 0 4px 12px rgba(88, 166, 255, 0.3); border-radius: 10px; }
            .section { display: none; flex-direction: column; gap: 16px; animation: fadeIn 0.3s ease-in-out; }
            .section.active { display: flex; }
            @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
            .card { background: var(--card-bg); border-radius: 16px; padding: 20px; border: 1px solid var(--card-border); }
            .card-title { font-size: 16px; font-weight: 700; color: var(--text-strong); margin-bottom: 12px; }
            .card-title-flex { display: flex; justify-content: space-between; align-items: center; }
            .btn-main { background: var(--accent); color: #fff; border: none; border-radius: 12px; padding: 14px; width: 100%; font-size: 15px; font-weight: 700; cursor: pointer; }
            .btn-outline { background: transparent; border: 2px solid var(--accent); color: var(--accent); }
            .btn-danger { background: rgba(248, 81, 73, 0.1); color: var(--danger); border: 1px solid var(--danger); }
            .action-row { display: flex; gap: 8px; margin-top: 10px; }
            .action-btn { flex: 1; padding: 8px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 6px; text-decoration: none; border: none; }
            .btn-connect { background: rgba(88, 166, 255, 0.15); border: 1px solid rgba(88, 166, 255, 0.3); color: var(--accent); }
            .input-group { margin-bottom: 12px; }
            input[type="text"], select { width: 100%; padding: 14px; border-radius: 12px; border: 1px solid var(--card-border); background: rgba(0,0,0,0.2); color: #fff; font-family: 'Poppins'; outline: none;}
            .progress-bg { background: rgba(255,255,255,0.05); border-radius: 10px; height: 14px; width: 100%; }
            .progress-fill { background: var(--gradient-gold); height: 100%; width: 0%; border-radius: 10px; transition: width 0.8s ease-in-out; }
            .chests-container { display: flex; justify-content: center; gap: 15px; margin: 20px 0; }
            .chest-wrapper { width: 90px; height: 90px; cursor: pointer; position: relative; transition: transform 0.2s;}
            .chest-wrapper.disabled { opacity: 0.5; filter: grayscale(100%); pointer-events: none; }
            .chest-img { width: 100%; height: 100%; object-fit: contain; }
            .list-item { background: rgba(255,255,255,0.03); padding: 16px; border-radius: 12px; margin-bottom: 12px; border: 1px solid var(--card-border); }
            .pill-container { display: flex; gap: 6px; margin-bottom: 14px; overflow-x: auto; }
            .pill { background: rgba(255,255,255,0.05); border: 1px solid var(--card-border); color: var(--hint); padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; }
            .pill.active { background: var(--accent); color: #fff; border-color: var(--accent); }
            .badge { font-size: 10px; padding: 2px 6px; border-radius: 6px; font-weight: 700; background: rgba(227, 179, 65, 0.15); color: var(--gold); border: 1px solid rgba(227, 179, 65, 0.3); }
        </style>
    </head>
    <body>
        <div class="header"><i class="fa-solid fa-bolt header-icon"></i><h1>Exchange Hub</h1></div>
        
        <div class="tabs" id="nav-tabs">
            <div class="tab active" onclick="switchTab('stats', this)"><i class="fa-solid fa-star"></i> VIP</div>
            <div class="tab" onclick="switchTab('cofres', this)"><i class="fa-solid fa-box-open"></i> Bonus</div>
            <div class="tab" onclick="switchTab('mercado', this)"><i class="fa-solid fa-store"></i> Market</div>
            <div class="tab" onclick="switchTab('rank', this)"><i class="fa-solid fa-trophy"></i> Top</div>
            <div class="tab" onclick="switchTab('inventory', this)"><i class="fa-solid fa-vault"></i> Cofre</div>
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
                <div class="card-title" style="color:var(--gold); text-align:center;">Recompensa Diaria</div>
                <div class="chests-container" id="chests-container"></div>
                <div id="bonus-status" style="font-weight:700; color:var(--hint); margin-top:10px;">Calculando...</div>
            </div>
        </div>

        <div id="mercado" class="section">
            <div class="card">
                <div class="card-title"><i class="fa-solid fa-store"></i> Publicar Oferta</div>
                <div class="input-group"><input type="text" id="offer-give" placeholder="📦 ¿Qué ofreces? (Ej: 50 Videos)" maxlength="60"></div>
                <div class="input-group"><input type="text" id="offer-want" placeholder="🎯 ¿Qué buscas? (Ej: 50 Fotos)" maxlength="60"></div>
                <div class="input-group">
                    <select id="offer-type">
                        <option value="mixed">🔀 Categoría: Mixto</option>
                        <option value="video">🎥 Categoría: Solo Videos</option>
                        <option value="photo">📷 Categoría: Solo Fotos</option>
                    </select>
                </div>
                <button class="btn-main" onclick="postOffer()" id="btn-post-offer"><i class="fa-solid fa-paper-plane"></i> Publicar</button>
                <div id="offer-cooldown" style="color:var(--danger); display:none; margin-top:10px; font-size: 12px; text-align: center;">Debe esperar para publicar.</div>
            </div>
            <div class="card">
                <div class="card-title card-title-flex" style="margin-bottom:12px;">
                    <span>Mercado En Vivo</span>
                    <input type="text" id="market-search" placeholder="🔍 Buscar..." oninput="filterOffers()" style="width: 110px; padding: 6px; font-size: 12px; border-radius: 8px;">
                </div>
                <div class="pill-container">
                    <button class="pill active" onclick="setCategoryFilter('all', this)">Todos</button>
                    <button class="pill" onclick="setCategoryFilter('video', this)">Videos</button>
                    <button class="pill" onclick="setCategoryFilter('photo', this)">Fotos</button>
                    <button class="pill" onclick="setCategoryFilter('mixed', this)">Mixto</button>
                </div>
                <div id="offers-list">Cargando...</div>
            </div>
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
            let botId = "BOT_ID_PLACEHOLDER";
            let reqHeaders = { "Content-Type": "application/json", "Authorization": tg.initData || "" };
            let allOffers = [];
            let currentCatFilter = 'all';

            function switchTab(tabId, el) {
                tg.HapticFeedback.impactOccurred('light');
                document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                el.classList.add('active');
                el.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
            }

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
                    tg.showAlert("No se pudo copiar.\\n\\n" + text);
                }
                document.body.removeChild(temp);
            }

            function copyRefLink() {
                let link = `https://t.me/${botUsername}?start=${userId}`;
                copyToClipboard(link, "✅ Link copiado al portapapeles.");
            }

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
                    let res = await fetch(`/api/bonus?id=${userId}&bot_id=${botId}`, { method: "POST", headers: reqHeaders, body: "{}" });
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
                    tg.showAlert("❌ Error de conexión al servidor."); 
                    document.querySelectorAll('.chest-wrapper').forEach(w => w.classList.remove('disabled'));
                    document.getElementById("bonus-status").innerText = "¡Toca un cofre!";
                    loadData(); 
                }
            }

            async function loadData() {
                if (!userId) return;
                try {
                    let res = await fetch(`/api/data?id=${userId}&bot_id=${botId}`, { headers: reqHeaders });
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

                    allOffers = data.offers || [];
                    renderOffers(allOffers);
                } catch(e) { console.error("Error loading data", e); }
            }

            function renderOffers(offers) {
                let query = document.getElementById("market-search").value.toLowerCase();
                let oHTML = "";
                let now = Date.now() / 1000;
                
                offers.forEach(o => {
                    if (currentCatFilter !== 'all' && o.type !== currentCatFilter) return;
                    if (query && !o.text.toLowerCase().includes(query)) return;
                    
                    let timeLeftSecs = Math.max(0, 86400 - (now - o.time));
                    let hoursLeft = Math.floor(timeLeftSecs / 3600);
                    let isOwner = o.user_id == userId;
                    let directLink = `https://t.me/${botUsername}?start=trade_${o.user_id}`;

                    oHTML += `<div class="list-item" style="display:flex; flex-direction:column; gap:8px;">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <span style="font-weight:700; font-size:14px;"><i class="fa-solid fa-circle-user"></i> ${o.name} <span class="badge">⭐ ${o.rep || 0} Pts</span></span>
                            <span style="font-size:10px; color:var(--hint);">⏳ Expira en ${hoursLeft}h</span>
                        </div>
                        <div style="font-size:13px; line-height:1.4; background:rgba(0,0,0,0.2); padding:10px; border-radius:8px;">${o.text}</div>
                        <div class="action-row">
                            ${isOwner ? 
                                `<button onclick="tg.showAlert('En desarrollo.')" class="action-btn btn-connect"><i class="fa-solid fa-check"></i> Tu oferta</button>` :
                                `<a href="${directLink}" class="action-btn btn-connect"><i class="fa-solid fa-comments"></i> Conectar Directo</a>`
                            }
                        </div>
                    </div>`;
                });
                document.getElementById("offers-list").innerHTML = oHTML || '<div style="text-align:center;color:var(--hint); font-size:14px;">No hay ofertas disponibles.</div>';
            }

            function filterOffers() { renderOffers(allOffers); }
            
            function setCategoryFilter(cat, el) {
                document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
                el.classList.add('active');
                currentCatFilter = cat;
                renderOffers(allOffers);
            }

            async function postOffer() {
                let give = document.getElementById('offer-give').value.trim();
                let want = document.getElementById('offer-want').value.trim();
                let type = document.getElementById('offer-type').value;
                
                if(give.length < 3 || want.length < 3) return tg.showAlert("⚠️ Detalla claramente qué ofreces y qué buscas.");
                
                let combinedText = `🎁 <b>Ofrezco:</b> ${give}\\n🎯 <b>Busco:</b> ${want}`;
                let btn = document.getElementById("btn-post-offer");
                btn.disabled = true;
                btn.innerText = "Publicando...";

                try {
                    let res = await fetch(`/api/offer?id=${userId}&bot_id=${botId}`, { 
                        method: "POST", 
                        headers: reqHeaders, 
                        body: JSON.stringify({ text: combinedText, name: user?.first_name || "Anónimo", type: type }) 
                    });
                    let data = await res.json();
                    
                    if(data.success) {
                        document.getElementById('offer-give').value = "";
                        document.getElementById('offer-want').value = "";
                        tg.HapticFeedback.notificationOccurred('success');
                        tg.showAlert("✅ Publicado con éxito en el mercado.");
                        loadData();
                    } else { 
                        tg.showAlert(data.error); 
                    }
                } catch(e) { 
                    tg.showAlert("❌ Error de red al publicar."); 
                }
                btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i> Publicar';
            }

            async function clearInventory() {
                tg.showConfirm("⚠️ ¿Vaciar todas tus fotos y videos permanentemente?", async (ok) => {
                    if(ok) {
                        try {
                            await fetch(`/api/clear?id=${userId}&bot_id=${botId}`, { method: "POST", headers: reqHeaders });
                            tg.HapticFeedback.notificationOccurred('success');
                            tg.showAlert("🗑️ Caja fuerte vaciada.");
                            loadData();
                        } catch(e) {
                            tg.showAlert("❌ Error al vaciar inventario.");
                        }
                    }
                });
            }

            setInterval(() => { if (userId) loadData(); }, 35000);
            initChests(false);
            loadData();
        </script>
    </body>
    </html>
    """.replace("BOT_USERNAME_PLACEHOLDER", bot_username).replace("BOT_ID_PLACEHOLDER", str(bot_id))
    return web.Response(text=html_content, content_type="text/html")


# =====================================================================
# 3. CORE SAAS: FÁBRICA DE BOTS HIJOS (TU CÓDIGO 100% AISLADO)
# =====================================================================
def get_new_child_dp(child_config: dict, child_db) -> Dispatcher:
    """Fábrica de Dispatchers. Crea un ecosistema asíncrono y aislado para cada cliente."""
    dp = Dispatcher(storage=MemoryStorage())
    
    # ---------------------------------------------------------
    # MEMORIA RAM AISLADA PARA ESTE BOT
    # ---------------------------------------------------------
    active_chats = {}
    waiting_list = []
    pending_trades = {}
    processed_albums = set()
    active_viewers = {} 
    pending_notifications = {}
    chat_threads = {} 
    
    # Cola de backup exclusiva para este bot
    backup_queue = asyncio.Queue()
    dp["backup_queue"] = backup_queue 
    dp["active_viewers"] = active_viewers

    # Variables de Configuración dinámicas (vienen de Mongo)
    FORCE_SUB_CHANNEL_ID = child_config.get("force_sub_id", 0)
    if FORCE_SUB_CHANNEL_ID: FORCE_SUB_CHANNEL_ID = int(FORCE_SUB_CHANNEL_ID)
    FORCE_SUB_CHANNEL_LINK = child_config.get("force_sub_link", "")
    VIP_GROUP_ID = int(child_config.get("vip_group_id", 0)) if child_config.get("vip_group_id") else 0
    LOG_GROUP_ID = -1004402977057 # Grupo genérico de logs como solicitaste
    SUPER_ADMIN_IDS = [8983189714, 7452819858] # Los tuyos

    # ---------------------------------------------------------
    # FUNCIONES AUXILIARES (Usan child_db y el bot inyectado)
    # ---------------------------------------------------------
    async def set_other_user_state(bot: Bot, chat_id: int, state: State):
        key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
        fsm_ctx = FSMContext(storage=dp.storage, key=key)
        await fsm_ctx.set_state(state)

    async def get_user(user_id):
        user = await child_db.users.find_one({"_id": user_id})
        if not user:
            user = {"_id": user_id, "lang": "es", "referrals": 0, "reputation": 0, "mode": "anon", "in_vip": False, "notified_vip": False, "last_bonus": 0, "last_offer": 0}
            await child_db.users.insert_one(user)
        return user

    async def save_user(user_id, data):
        await child_db.users.update_one({"_id": user_id}, {"$set": data}, upsert=True)

    async def check_force_sub(user_id, bot: Bot):
        if user_id in SUPER_ADMIN_IDS: return True
        if not FORCE_SUB_CHANNEL_ID: return True
        try:
            member = await bot.get_chat_member(FORCE_SUB_CHANNEL_ID, user_id)
            return member.status in ["member", "administrator", "creator"]
        except Exception as e: 
            logging.error(f"Error Force Sub: {e}")
            return False

    async def check_vip_status(user_id, bot: Bot):
        if not VIP_GROUP_ID: return
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
        except Exception as e:
            logging.error(f"❌ Error crítico en check_vip_status para el usuario {user_id}: {e}")

    async def send_rating_request(user_id, target_id, bot: Bot):
        user = await get_user(user_id)
        lang = user.get("lang", "es")
        btn_g = "👍 Buen usuario" if lang == "es" else "👍 Good user"
        btn_b = "👎 Malo" if lang == "es" else "👎 Bad"
        msg = "¿Deseas darle un punto extra a tu compañero?" if lang == "es" else "Do you want to give your partner an extra point?"
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_g, callback_data=f"rate_good_{target_id}"), InlineKeyboardButton(text=btn_b, callback_data=f"rate_bad_{target_id}")]
        ])
        await bot.send_message(user_id, msg, reply_markup=markup)

    async def send_delayed_notification(u_id, lang, bot: Bot):
        await asyncio.sleep(2.5) 
        total = await child_db.inventory.count_documents({"user_id": u_id})
        msg_es = f"📥 **Lote de archivos guardado.** (Total en inventario: {total})\n\n⚠️ **Importante:** No elimines los mensajes que subas aquí."
        msg_en = f"📥 **Batch of files saved.** (Total inventory: {total})\n\n⚠️ **Important:** Do not delete the messages you upload here."
        try:
            await bot.send_message(u_id, msg_es if lang == "es" else msg_en, parse_mode="Markdown")
        except Exception as e: pass
        finally: pending_notifications.pop(u_id, None)
        
    async def get_random_batch(sender_id: int, receiver_id: int, category: str, amount: int):
        already_sent = [doc["file_unique_id"] async for doc in child_db.exchange_history.find({"sender_id": sender_id, "receiver_id": receiver_id}, {"file_unique_id": 1})]
        match_query = {"user_id": sender_id, "file_unique_id": {"$nin": already_sent}}
        if category != "mixed": match_query["type"] = category
        safe_amount = (amount * 2) + 50
        pipeline = [{"$match": match_query}, {"$sample": {"size": safe_amount}}]
        selected = [doc async for doc in child_db.inventory.aggregate(pipeline)]
        return len(selected) >= amount, selected

    async def show_main_menu(user_id, bot: Bot):
        user = await get_user(user_id)
        lang = user.get("lang", "es")
        bot_info = await bot.get_me()
        my_link = f"https://t.me/{bot_info.username}?start={user['_id']}"
        webapp_url = f"{RENDER_URL}/?bot={bot_info.username}&bot_id={bot.id}"
        
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
            txt = (
                "👋 <b>¡Bienvenido a la red de intercambio!</b>\n\n"
                "⚠️ <b>REQUISITO CLAVE:</b> Sube material propio a este chat para poder hacer intercambios. "
                "¡Sin videos o fotos en tu inventario, no podrás recibir nada!\n\n"
                "🎁 Utiliza la nueva <b>Mini App</b> para reclamar tu bonus diario y ver tu progreso VIP. 🚀"
            )
        else:
            txt = (
                "👋 <b>Welcome to the exchange network!</b>\n\n"
                "⚠️ <b>KEY REQUIREMENT:</b> Upload your own media to this chat to be able to trade.\n\n"
                "🎁 Use the new <b>Mini App</b> to claim your daily bonus and check your VIP progress. 🚀"
            )
        await bot.send_message(chat_id=user_id, text=txt, reply_markup=markup, parse_mode="HTML")

    # ---------------------------------------------------------
    # HANDLERS: ADMIN
    # ---------------------------------------------------------
    @dp.message(Command("add_receiver"))
    async def cmd_add_receiver(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            new_id = int(message.text.split()[1])
            await child_db.settings.update_one({"_id": "config"}, {"$addToSet": {"extra_receivers": new_id}}, upsert=True)
            await message.answer(f"✅ Añadido `{new_id}`.")
        except: await message.answer("⚠️ Uso: `/add_receiver ID`")

    @dp.message(Command("del_receiver"))
    async def cmd_del_receiver(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            rem_id = int(message.text.split()[1])
            await child_db.settings.update_one({"_id": "config"}, {"$pull": {"extra_receivers": rem_id}}, upsert=True)
            await message.answer(f"✅ ID `{rem_id}` eliminado.")
        except: await message.answer("⚠️ Uso: `/del_receiver ID`")

    @dp.message(Command("broadcast"))
    async def cmd_broadcast(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        text = message.text.replace("/broadcast", "").strip()
        if not text: return await message.answer("⚠️ Escribe el mensaje a difundir.")
        await message.answer("⏳ Iniciando difusión...")
        count = 0
        async for user in child_db.users.find():
            try:
                await bot.send_message(user["_id"], f"📢 <b>Aviso:</b>\n\n{text}", parse_mode="HTML")
                count += 1
                await asyncio.sleep(0.05) 
            except: pass
        await message.answer(f"✅ Difusión completada a <code>{count}</code> usuarios.", parse_mode="HTML")

    @dp.message(Command("estadisticas"))
    async def cmd_stats(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        total_users = await child_db.users.count_documents({})
        total_files = await child_db.inventory.count_documents({})
        active_chats_count = len(active_chats) // 2
        total_archivos_enviados = await child_db.exchange_history.count_documents({})
        operaciones_reales = total_archivos_enviados // 2
        vip_users = await child_db.users.count_documents({"in_vip": True})
        stats_text = (
            "📊 **ESTADÍSTICAS DEL BOT HIJO**\n\n"
            f"👥 Usuarios registrados: `{total_users}`\n"
            f"🌟 Usuarios VIP: `{vip_users}`\n"
            f"📁 Archivos en cofre: `{total_files}`\n"
            f"🔄 Intercambios exitosos: `{operaciones_reales}`\n"
            f"💬 Chats en vivo: `{active_chats_count}`"
        )
        await message.answer(stats_text, parse_mode="Markdown")

    @dp.message(Command("reinvitar"))
    async def cmd_reinvite(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS or not VIP_GROUP_ID: return
        try:
            t_id = int(message.text.split()[1])
            await bot.unban_chat_member(chat_id=VIP_GROUP_ID, user_id=t_id, only_if_banned=True)
            link = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
            await bot.send_message(t_id, f"🎉 ¡VIP Restablecido!\nÚnete: {link.invite_link}")
            await message.answer("✅ Reinvitado.")
        except: await message.answer("⚠️ Uso: `/reinvitar ID`")

    # ---------------------------------------------------------
    # HANDLERS: USUARIOS Y MENÚS
    # ---------------------------------------------------------
    @dp.message(CommandStart(), StateFilter("*"))
    async def cmd_start(message: Message, state: FSMContext, bot: Bot):
        await state.clear()
        user_id = message.from_user.id
        args = message.text.split(maxsplit=1)
        
        if user_id in waiting_list: waiting_list.remove(user_id)
            
        t_id = active_chats.pop(user_id, None)
        if t_id:
            active_chats.pop(t_id, None)
            await set_other_user_state(bot, t_id, BotStates.idle)
            try:
                await bot.send_message(t_id, "❌ **El chat finalizó porque el otro usuario regresó al menú.**", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
                await show_main_menu(t_id, bot)
            except: pass
            chat_threads.pop(user_id, None)
            chat_threads.pop(t_id, None)
        
        user = await get_user(user_id)
        lang = user.get("lang", "es")
        is_first_time = not user.get("started_bot", False)

        if len(args) > 1 and args[1].isdigit() and is_first_time:
            inviter_id = int(args[1])
            if inviter_id != user_id:
                try:
                    await save_user(user_id, {"referred_by": inviter_id})
                    await child_db.users.update_one({"_id": inviter_id}, {"$inc": {"referrals": 1}})
                    await check_vip_status(inviter_id, bot) 
                except: pass

        if is_first_time: await save_user(user_id, {"started_bot": True})

        has_subbed = await check_force_sub(user_id, bot)
        if not has_subbed:
            btn_join = "📢 Unirse al Canal" if lang == "es" else "📢 Join Channel"
            btn_ver = "✅ Verificar Ingreso" if lang == "es" else "✅ Verify Join"
            txt_res = "🛑 **Acceso Restringido**\nDebes unirte a nuestro canal para usar el bot." if lang == "es" else "🛑 **Access Restricted**\nYou must join our canal to use the bot."
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=btn_join, url=FORCE_SUB_CHANNEL_LINK)],
                [InlineKeyboardButton(text=btn_ver, callback_data="verify_sub")]
            ])
            return await message.answer(txt_res, reply_markup=markup, parse_mode="Markdown")

        try:
            await show_main_menu(user_id, bot)
        except Exception as e:
            await message.answer("✅ Bot iniciado. Usa el menú del teclado.")
            
        await state.set_state(BotStates.idle)

    @dp.callback_query(F.data == "verify_sub")
    async def verify_sub(callback: CallbackQuery, bot: Bot):
        user = await get_user(callback.from_user.id)
        lang = user.get("lang", "es")
        if await check_force_sub(callback.from_user.id, bot):
            await callback.message.delete()
            await show_main_menu(callback.from_user.id, bot)
        else: 
            err_msg = "⚠️ Aún no te has unido." if lang == "es" else "⚠️ You haven't joined yet."
            await callback.answer(err_msg, show_alert=True)

    @dp.message(Command("help"))
    async def cmd_help(message: Message, bot: Bot):
        user = await get_user(message.from_user.id)
        lang = user.get("lang", "es")
        if lang == "es":
            txt = "🤖 **Guía Completa**\n\n📦 **1. Carga inventario:** Sube fotos/videos aquí.\n💬 **2. Inicia Chat:** Conecta al azar o por ID.\n🤝 **3. Lotes:** Usa el botón 'Proponer' en el chat.\n🌟 **4. VIP:** Gana 20 puntos de reputación para entrar al grupo VIP."
        else:
            txt = "🤖 **Complete Guide**\n\n📦 **1. Load inventory:** Upload photos/videos here.\n💬 **2. Start Chat:** Connect randomly or by ID.\n🤝 **3. Batches:** Use the 'Propose' button in chat.\n🌟 **4. VIP:** Earn 20 reputation points to enter the VIP group."
        await message.answer(txt, parse_mode="Markdown")

    @dp.callback_query(F.data == "change_lang")
    async def change_lang(callback: CallbackQuery, bot: Bot):
        user = await get_user(callback.from_user.id)
        new_lang = "en" if user.get("lang") == "es" else "es"
        await save_user(callback.from_user.id, {"lang": new_lang})
        msg = "✅ Idioma actualizado" if new_lang == "es" else "✅ Language updated"
        await callback.answer(msg)
        await callback.message.delete()
        await show_main_menu(callback.from_user.id, bot)

    @dp.callback_query(F.data == "my_profile")
    async def show_profile(callback: CallbackQuery, bot: Bot):
        user_id = callback.from_user.id
        await check_vip_status(user_id, bot)
        
        user = await get_user(user_id)
        lang = user.get("lang", "es")
        uid = user["_id"]
        fotos = await child_db.inventory.count_documents({"user_id": uid, "type": "photo"})
        videos = await child_db.inventory.count_documents({"user_id": uid, "type": "video"})
        
        btn_mod = "🔄 Cambiar Modo" if lang == "es" else "🔄 Change Mode"
        btn_vol = "⬅️ Volver" if lang == "es" else "⬅️ Back"
        
        inline_kb = [[InlineKeyboardButton(text=btn_mod, callback_data="toggle_mode")]]
        
        if VIP_GROUP_ID and (user.get("in_vip") or user.get("referrals", 0) >= 3 or user.get("reputation", 0) >= 20):
            try:
                invite = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
                btn_vip = "🌟 Ir al grupo VIP" if lang == "es" else "🌟 Go to VIP Group"
                inline_kb.insert(0, [InlineKeyboardButton(text=btn_vip, url=invite.invite_link)])
            except: pass
                
        inline_kb.append([InlineKeyboardButton(text=btn_vol, callback_data="back_main")])
        markup = InlineKeyboardMarkup(inline_keyboard=inline_kb)
        
        if lang == "es":
            modo = "🕵️‍♂️ Anónimo" if user.get("mode") == "anon" else "👤 Público"
            txt = f"👤 **Tu Perfil**\n\n🆔 ID: `{uid}`\n🌟 Reputación: `{user.get('reputation', 0)}/20`\n👥 Referidos: `{user.get('referrals', 0)}/3`\n🎭 Modo: **{modo}**\n\n📦 Inventario: 📷 {fotos} | 🎥 {videos}"
        else:
            modo = "🕵️‍♂️ Anonymous" if user.get("mode") == "anon" else "👤 Public"
            txt = f"👤 **Your Profile**\n\n🆔 ID: `{uid}`\n🌟 Reputation: `{user.get('reputation', 0)}/20`\n👥 Referrals: `{user.get('referrals', 0)}/3`\n🎭 Mode: **{modo}**\n\n📦 Inventory: 📷 {fotos} | 🎥 {videos}"
            
        await callback.message.edit_text(txt, reply_markup=markup, parse_mode="Markdown")

    @dp.callback_query(F.data == "toggle_mode")
    async def toggle_mode(callback: CallbackQuery, bot: Bot):
        user = await get_user(callback.from_user.id)
        await save_user(user["_id"], {"mode": "public" if user.get("mode") == "anon" else "anon"})
        await show_profile(callback, bot)

    @dp.callback_query(F.data == "back_main")
    async def back_to_main(callback: CallbackQuery, state: FSMContext, bot: Bot):
        await state.set_state(BotStates.idle)
        await callback.message.delete()
        await show_main_menu(callback.from_user.id, bot)

    # ---------------------------------------------------------
    # HANDLERS: CHAT AL AZAR Y POR ID
    # ---------------------------------------------------------
    @dp.callback_query(F.data == "connect_id")
    async def ask_for_id(callback: CallbackQuery, state: FSMContext, bot: Bot):
        user = await get_user(callback.from_user.id)
        lang = user.get("lang", "es")
        txt = "✏️ Escribe el **ID numérico** del usuario:" if lang == "es" else "✏️ Enter the user's **numeric ID**:"
        btn = "⬅️ Cancelar" if lang == "es" else "⬅️ Cancel"
        
        await state.set_state(BotStates.waiting_for_id)
        await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn, callback_data="back_main")]]), parse_mode="Markdown")

    @dp.message(StateFilter(BotStates.waiting_for_id), ~F.text.startswith("/"))
    async def process_connect_id(message: Message, state: FSMContext, bot: Bot):
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

    @dp.callback_query(F.data.startswith("accept_id_"))
    async def accept_id_connection(callback: CallbackQuery, state: FSMContext, bot: Bot):
        t_id, u_id = int(callback.data.split("_")[2]), callback.from_user.id
        user = await get_user(u_id)
        t_user = await get_user(t_id)
        
        if t_id in active_chats or u_id in active_chats: 
            return await callback.answer("Ocupado." if user.get("lang") == "es" else "Busy.", show_alert=True)
            
        active_chats[u_id], active_chats[t_id] = t_id, u_id
        
        try:
            topic = await bot.create_forum_topic(chat_id=LOG_GROUP_ID, name=f"Chat {u_id} & {t_id}")
            chat_threads[u_id] = topic.message_thread_id
            chat_threads[t_id] = topic.message_thread_id
        except: pass

        await state.set_state(BotStates.chatting)
        await set_other_user_state(bot, t_id, BotStates.chatting)
        
        for uid, u_obj in [(u_id, user), (t_id, t_user)]:
            lng = u_obj.get("lang", "es")
            kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"), KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]], resize_keyboard=True)
            msg = "✅ **Conexión Establecida.**" if lng == "es" else "✅ **Connection Established.**"
            await bot.send_message(uid, msg, reply_markup=kb, parse_mode="Markdown")
            
        await callback.message.delete()

    @dp.callback_query(F.data == "find_chat")
    async def find_chat(callback: CallbackQuery, state: FSMContext, bot: Bot):
        u_id = callback.from_user.id
        if u_id in active_chats:
            return await callback.answer("⚠️ Ya tienes un chat activo.", show_alert=True)
        if u_id in waiting_list:
            return await callback.answer("⏳ Ya estás buscando un chat...", show_alert=True)

        user = await get_user(u_id)
        lang = user.get("lang", "es")
        
        if waiting_list:
            t_id = waiting_list.pop(0)
            if t_id == u_id:
                waiting_list.append(u_id)
                return await callback.answer("⏳ Buscando...", show_alert=False)
            if t_id in active_chats:
                waiting_list.append(u_id)
                return await callback.answer("⚠️ El usuario se ocupó. Buscando...", show_alert=True)
                
            t_user = await get_user(t_id)
            active_chats[u_id], active_chats[t_id] = t_id, u_id
            await state.set_state(BotStates.chatting)
            await set_other_user_state(bot, t_id, BotStates.chatting)
            
            try:
                topic = await bot.create_forum_topic(chat_id=LOG_GROUP_ID, name=f"Chat {u_id} & {t_id}")
                chat_threads[u_id] = topic.message_thread_id
                chat_threads[t_id] = topic.message_thread_id
            except: pass
            
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

    @dp.message(F.text.in_(["❌ Desconectar", "❌ Disconnect"]))
    @dp.message(Command("leave"))
    @dp.callback_query(F.data == "leave_chat")
    async def leave_chat(event, state: FSMContext, bot: Bot):
        u_id = event.from_user.id
        user = await get_user(u_id)
        lang = user.get("lang", "es")
        
        if u_id in waiting_list: waiting_list.remove(u_id)
        t_id = active_chats.pop(u_id, None)
        
        if t_id:
            active_chats.pop(t_id, None)
            t_user = await get_user(t_id)
            t_lang = t_user.get("lang", "es")
            
            await set_other_user_state(bot, t_id, BotStates.idle)
            t_msg = "❌ **El chat finalizó.**" if t_lang == "es" else "❌ **Chat ended.**"
            await bot.send_message(t_id, t_msg, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
            await show_main_menu(t_id, bot)
            
        chat_threads.pop(u_id, None)
        if t_id: chat_threads.pop(t_id, None)
            
        await state.set_state(BotStates.idle)
        msg = "Has salido." if lang == "es" else "You left."
        
        if isinstance(event, Message): 
            await event.answer(msg, reply_markup=ReplyKeyboardRemove())
        else:
            await event.message.delete()
            await bot.send_message(u_id, msg, reply_markup=ReplyKeyboardRemove())
        await show_main_menu(u_id, bot)

    # ---------------------------------------------------------
    # HANDLERS: LÓGICA DE INTERCAMBIOS
    # ---------------------------------------------------------
    @dp.message(F.chat.type == "private", F.photo | F.video | F.document)
    async def handle_media(message: Message, bot: Bot):
        u_id = message.from_user.id
        user = await get_user(u_id)
        lang = user.get("lang", "es")
        
        media = message.photo[-1] if message.photo else (message.video if message.video else message.document)
        file_id, file_unique_id = media.file_id, media.file_unique_id
        m_type = "photo" if message.photo else ("video" if message.video else "document")

        if not await child_db.global_files.find_one({"_id": file_unique_id}):
            await child_db.global_files.insert_one({"_id": file_unique_id})
            await backup_queue.put({"file_id": file_id, "type": m_type, "user_id": u_id, "name": message.from_user.full_name})

        if u_id in active_chats:
            target = active_chats[u_id]
            try: 
                await message.forward(target)
                thread_id = chat_threads.get(u_id)
                if thread_id:
                    m_type_name = "una foto 📷" if message.photo else ("un video 🎥" if message.video else "un documento 📁")
                    await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=f"📎 El usuario `{u_id}` envió {m_type_name} en el chat privado.", parse_mode="Markdown")
            except: pass
            return

        is_new = False
        if not await child_db.inventory.find_one({"user_id": u_id, "file_unique_id": file_unique_id}):
            await child_db.inventory.insert_one({"user_id": u_id, "file_id": file_id, "message_id": message.message_id, "file_unique_id": file_unique_id, "type": m_type})
            is_new = True

        if is_new:
            if u_id not in pending_notifications:
                pending_notifications[u_id] = True
                asyncio.create_task(send_delayed_notification(u_id, lang, bot))

    @dp.message(StateFilter(BotStates.chatting), F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade"]))
    async def btn_propose(message: Message, state: FSMContext, bot: Bot):
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

    @dp.callback_query(StateFilter(BotStates.waiting_trade_type), F.data.startswith("settype_"))
    async def process_trade_type(callback: CallbackQuery, state: FSMContext, bot: Bot):
        user = await get_user(callback.from_user.id)
        lang = user.get("lang", "es")
        await state.update_data(trade_type=callback.data.split("_")[1])
        await state.set_state(BotStates.waiting_trade_amount)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="10x10", callback_data="trade_10"), InlineKeyboardButton(text="50x50", callback_data="trade_50"), InlineKeyboardButton(text="100x100", callback_data="trade_100")]
        ])
        msg = "🔢 **¿Cuántos archivos?**\n\nSelecciona o escribe el número:" if lang == "es" else "🔢 **How many files?**\n\nSelect or type the number:"
        await callback.message.edit_text(msg, reply_markup=markup, parse_mode="Markdown")

    async def execute_trade_proposal(u_id, amt, t_type, send_func, state, bot: Bot):
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

    @dp.message(StateFilter(BotStates.waiting_trade_amount), F.text.regexp(r'^\d+$'))
    async def process_manual_trade_offer(message: Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await execute_trade_proposal(message.from_user.id, int(message.text), data.get("trade_type", "mixed"), message.answer, state, bot)

    @dp.callback_query(StateFilter(BotStates.waiting_trade_amount), F.data.startswith("trade_"))
    async def process_button_trade_offer(callback: CallbackQuery, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await callback.message.delete()
        await execute_trade_proposal(callback.from_user.id, int(callback.data.split("_")[1]), data.get("trade_type", "mixed"), callback.message.answer, state, bot)

    @dp.callback_query(F.data == "accept_trade")
    async def accept_trade(callback: CallbackQuery, bot: Bot):
        u_id = callback.from_user.id
        trade = pending_trades.pop(u_id, None)
        if not trade: return
        s_id, amt, t_type = trade["sender"], trade["amount"], trade.get("type", "mixed")
        
        user = await get_user(u_id)
        s_user = await get_user(s_id)
        lang, s_lang = user.get("lang", "es"), s_user.get("lang", "es")
        
        msg_chk = "✅ Comprobando inventarios..." if lang == "es" else "✅ Checking inventories..."
        await callback.message.edit_text(msg_chk)
        
        ok_s, files_s = await get_random_batch(s_id, u_id, t_type, amt)
        ok_r, files_r = await get_random_batch(u_id, s_id, t_type, amt)
        
        if not ok_s or not ok_r:
            err_es = "⚠️ Intercambio cancelado. Uno de los dos no tiene suficientes archivos."
            err_en = "⚠️ Trade canceled. One of you doesn't have enough files."
            await callback.message.edit_text(err_es if lang == "es" else err_en)
            return await bot.send_message(s_id, err_es if s_lang == "es" else err_en)

        msg_proc = "✅ Procesando envío..." if lang == "es" else "✅ Processing delivery..."
        msg_proc_s = "✅ Procesando envío..." if s_lang == "es" else "✅ Processing delivery..."
        await callback.message.edit_text(msg_proc)
        await bot.send_message(s_id, msg_proc_s)
        
        if amt >= 20:
            aviso_es = "⏳ **Enviando lote masivo...**\nPor seguridad, los archivos se enviarán uno por uno."
            aviso_en = "⏳ **Sending massive batch...**\nFiles will be sent one by one."
            await bot.send_message(u_id, aviso_es if lang == "es" else aviso_en, parse_mode="Markdown")
            await bot.send_message(s_id, aviso_es if s_lang == "es" else aviso_en, parse_mode="Markdown")
        
        sent_s, sent_r = 0, 0
        iter_s, iter_r = iter(files_s), iter(files_r)
        
        for _ in range(amt):
            success_s = False
            for f in iter_s:
                try:
                    await bot.forward_message(chat_id=u_id, from_chat_id=s_id, message_id=f["message_id"])
                    await child_db.exchange_history.insert_one({"sender_id": s_id, "receiver_id": u_id, "file_unique_id": f["file_unique_id"]})
                    success_s = True
                    break 
                except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after) 
                except Exception: await child_db.inventory.delete_one({"_id": f["_id"]})
            
            if not success_s: break
                
            success_r = False
            for f in iter_r:
                try:
                    await bot.forward_message(chat_id=s_id, from_chat_id=u_id, message_id=f["message_id"])
                    await child_db.exchange_history.insert_one({"sender_id": u_id, "receiver_id": s_id, "file_unique_id": f["file_unique_id"]})
                    success_r = True
                    break
                except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after)
                except Exception: await child_db.inventory.delete_one({"_id": f["_id"]})
                
            if not success_r:
                sent_s += 1
                break
                
            sent_s += 1
            sent_r += 1
            await asyncio.sleep(0.15)

        if sent_s == 0 and sent_r == 0:
            fail_msg = "❌ **Intercambio fallido.** Los archivos originales fueron borrados del chat."
            await bot.send_message(u_id, fail_msg, parse_mode="Markdown")
            await bot.send_message(s_id, fail_msg, parse_mode="Markdown")
            thread_id = chat_threads.get(u_id) or chat_threads.get(s_id)
            if thread_id:
                try: await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text="❌ **Intercambio Cancelado:** Archivos borrados.", parse_mode="Markdown")
                except: pass
            return
            
        thread_id = chat_threads.get(u_id) or chat_threads.get(s_id)
        if thread_id:
            report_text = f"🔄 **¡Intercambio Finalizado!** ✅\n\n• Part 1: `{s_id}` (Entregó `{sent_s}`)\n• Part 2: `{u_id}` (Entregó `{sent_r}`)\n• Tipo: **{t_type}**"
            try: await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=report_text, parse_mode="Markdown")
            except: pass

        await child_db.users.update_one({"_id": u_id}, {"$inc": {"reputation": 1}})
        await child_db.users.update_one({"_id": s_id}, {"$inc": {"reputation": 1}})
        await check_vip_status(u_id, bot)
        await check_vip_status(s_id, bot)
        
        ok_es_u = f"🎉 **¡Intercambio finalizado!**\n📥 Recibiste **{sent_s}** archivos.\n⭐ *Se sumó +1 punto de reputación.*"
        ok_en_u = f"🎉 **Trade completed!**\n📥 You received **{sent_s}** files.\n⭐ *+1 reputation point.*"
        await bot.send_message(u_id, ok_es_u if lang == "es" else ok_en_u, parse_mode="Markdown")

        ok_es_s = f"🎉 **¡Intercambio finalizado!**\n📥 Recibiste **{sent_r}** archivos.\n⭐ *Se sumó +1 punto de reputación.*"
        ok_en_s = f"🎉 **Trade completed!**\n📥 You received **{sent_r}** files.\n⭐ *+1 reputation point.*"
        await bot.send_message(s_id, ok_es_s if s_lang == "es" else ok_en_s, parse_mode="Markdown")
        
        await send_rating_request(u_id, s_id, bot)
        await send_rating_request(s_id, u_id, bot)

    @dp.callback_query(F.data == "reject_trade")
    async def reject_trade(callback: CallbackQuery, bot: Bot):
        trade = pending_trades.pop(callback.from_user.id, None)
        user = await get_user(callback.from_user.id)
        lang = user.get("lang", "es")
        
        if trade: 
            s_user = await get_user(trade["sender"])
            msg = "❌ Propuesta rechazada." if s_user.get("lang", "es") == "es" else "❌ Offer rejected."
            await bot.send_message(trade["sender"], msg)
            
        await callback.message.edit_text("❌ Rechazado." if lang == "es" else "❌ Rejected.")

    @dp.callback_query(F.data.startswith("rate_"))
    async def process_rating(callback: CallbackQuery, bot: Bot):
        action, _, t_id = callback.data.split("_")
        user = await get_user(callback.from_user.id)
        lang = user.get("lang", "es")
        
        if action == "good":
            await child_db.users.update_one({"_id": int(t_id)}, {"$inc": {"reputation": 1}})
            await check_vip_status(int(t_id), bot)
            
        msg = "✅ Valoración enviada." if lang == "es" else "✅ Rating sent."
        await callback.message.edit_text(msg)

    @dp.message(StateFilter(BotStates.chatting), ~F.text.startswith("/"), ~F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade", "❌ Desconectar", "❌ Disconnect"]))
    async def relay_msg(message: Message, bot: Bot):
        u_id = message.from_user.id
        target = active_chats.get(u_id)
        if target:
            try: 
                await message.forward(target)
                thread_id = chat_threads.get(u_id)
                if thread_id and message.text:
                    await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=f"💬 `{u_id}`: {message.text}", parse_mode="Markdown")
            except: pass

    return dp


# =====================================================================
# 4. TRABAJADORES EN SEGUNDO PLANO Y AISLAMIENTO DE PROCESOS
# =====================================================================
async def child_message_worker(bot_id: int):
    """Procesa los backups de fotos/videos enviándolos a los administradores del bot."""
    bot = active_bots_tasks[bot_id]["bot"]
    queue = active_bots_tasks[bot_id]["polling_task"].get_coro().cr_frame.f_locals['dp']["backup_queue"]
    child_db = active_bots_tasks[bot_id]["db"]
    
    try:
        while True:
            task = await queue.get()
            try:
                caption = f"👤 Subido por: {task.get('name', 'Usuario')} (`{task['user_id']}`)"
                file_id, m_type = task["file_id"], task["type"]
                
                doc = await child_db.settings.find_one({"_id": "config"})
                extra_ids = doc.get("extra_receivers", []) if doc else []
                receivers = list(set([8983189714] + extra_ids)) # Tu ID por defecto + configurados
                
                for receiver_id in receivers:
                    try:
                        if m_type == "photo": await bot.send_photo(receiver_id, file_id, caption=caption)
                        elif m_type == "video": await bot.send_video(receiver_id, file_id, caption=caption)
                        else: await bot.send_document(receiver_id, file_id, caption=caption)
                    except: pass
                    await asyncio.sleep(2.5)
            except Exception as e: print(f"❌ Error en cola: {e}")
            finally: queue.task_done()
    except asyncio.CancelledError:
        logging.info(f"Worker del bot {bot_id} cancelado correctamente.")

async def isolate_and_cleanup_bot(bot_id: int, revoked: bool = False):
    if bot_id in active_bots_tasks:
        tasks = active_bots_tasks[bot_id]
        tasks["polling_task"].cancel()
        tasks["worker_task"].cancel()
        await tasks["bot"].session.close()
        
        token = tasks["bot"].token
        del active_bots_tasks[bot_id]
        if revoked:
            await master_db.child_bots.update_one({"bot_token": token}, {"$set": {"status": "revoked"}})

async def child_polling_wrapper(dp: Dispatcher, bot: Bot, bot_id: int):
    try:
        await dp.start_polling(bot, handle_signals=False) # CRÍTICO PARA EL SAAS
    except TelegramUnauthorizedError:
        await isolate_and_cleanup_bot(bot_id, revoked=True)
    except asyncio.CancelledError: pass

async def start_child_bot(config: dict) -> bool:
    token = config["bot_token"]
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    try: bot_id = (await bot.get_me()).id
    except TelegramUnauthorizedError:
        await bot.session.close()
        await master_db.child_bots.update_one({"bot_token": token}, {"$set": {"status": "revoked"}})
        return False

    if bot_id in active_bots_tasks: return True
    db_version = config.get("db_version", "v1")
    child_db = master_db_client[f"child_{bot_id}_{db_version}"] 
    
    dp = get_new_child_dp(config, child_db)
    active_bots_tasks[bot_id] = {"bot": bot, "db": child_db}
    
    polling_task = asyncio.create_task(child_polling_wrapper(dp, bot, bot_id))
    worker_task = asyncio.create_task(child_message_worker(bot_id))
    active_bots_tasks[bot_id]["polling_task"] = polling_task
    active_bots_tasks[bot_id]["worker_task"] = worker_task
    return True

async def restore_bots():
    cursor = master_db.child_bots.find({"status": "active"})
    async for config in cursor: await start_child_bot(config)

# =====================================================================
# 5. HANDLERS DEL MASTER BOT (Asistente de Configuración)
# =====================================================================
@master_dp.message(F.text == "/start")
async def cmd_start_master(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 <b>Panel de Control SaaS</b>\nUsa /crear_bot para levantar tu Bot de intercambios.", parse_mode="HTML")

@master_dp.message(F.text == "/crear_bot")
async def cmd_crear_bot(message: Message, state: FSMContext):
    await message.answer("Paso 1/4: Envíame el <b>Token</b> del @BotFather.", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_token)

@master_dp.message(CreateChildBot.waiting_for_token)
async def process_token(message: Message, state: FSMContext):
    await state.update_data(token=message.text.strip())
    await message.answer("Paso 2/4: <b>ID del Canal de Suscripción</b> (Ej: -100123).", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_sub_id)

@master_dp.message(CreateChildBot.waiting_for_sub_id)
async def process_sub_id(message: Message, state: FSMContext):
    await state.update_data(sub_id=message.text.strip())
    await message.answer("Paso 3/4: <b>Enlace del Canal</b> (Ej: https://t.me/canal).", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_sub_link)

@master_dp.message(CreateChildBot.waiting_for_sub_link)
async def process_sub_link(message: Message, state: FSMContext):
    await state.update_data(sub_link=message.text.strip())
    await message.answer("Paso 4/4: <b>ID del Grupo VIP</b>.", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_vip_id)

@master_dp.message(CreateChildBot.waiting_for_vip_id)
async def process_vip_id(message: Message, state: FSMContext):
    await state.update_data(vip_id=message.text.strip())
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Versión v1", callback_data="set_db_v1")],
        [InlineKeyboardButton(text="Versión v2", callback_data="set_db_v2")]
    ])
    await message.answer("Selecciona la versión de la base de datos:", reply_markup=markup)
    await state.set_state(CreateChildBot.waiting_for_db_version)

@master_dp.callback_query(CreateChildBot.waiting_for_db_version)
async def process_db_version(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("⏳ Levantando bot...")
    db_version = callback.data.split("_")[-1] 
    data = await state.get_data()
    
    new_bot_config = {
        "owner_id": callback.from_user.id,
        "bot_token": data["token"],
        "status": "active",
        "force_sub_id": data["sub_id"], "force_sub_link": data["sub_link"],
        "vip_group_id": data["vip_id"], "db_version": db_version
    }
    
    await master_db.child_bots.insert_one(new_bot_config)
    success = await start_child_bot(new_bot_config)
    
    if success: await callback.message.edit_text("🎉 <b>¡BOT EN LÍNEA Y AISLADO!</b>", parse_mode="HTML")
    else: await callback.message.edit_text("❌ Token inválido.")
    await state.clear()


# =====================================================================
# 6. INICIO DEL SISTEMA (SERVER + BOTS)
# =====================================================================
async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_webapp)
    app.router.add_post("/api/live_ping", api_live_ping)
    app.router.add_get("/api/data", api_get_data)
    app.router.add_post("/api/bonus", api_claim_bonus)
    app.router.add_post("/api/offer", api_post_offer)
    app.router.add_post("/api/clear", api_clear_inv)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    return runner

async def main():
    logging.basicConfig(level=logging.INFO)
    master_bot = Bot(token=MASTER_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    runner = None
    try:
        runner = await web_server()
        await restore_bots()
        await master_bot.delete_webhook(drop_pending_updates=True)
        print("🚀 Sistema Master-Child corriendo exitosamente.")
        await master_dp.start_polling(master_bot)
    finally:
        await master_bot.session.close()
        for bid in list(active_bots_tasks.keys()): await isolate_and_cleanup_bot(bid)
        master_db_client.close()
        if runner: await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())