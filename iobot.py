import os
import hmac
import hashlib
import asyncio
import logging
import time
import random
from datetime import datetime
from urllib.parse import parse_qsl
import json
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, WebAppInfo,
    ChatJoinRequest, LabeledPrice, PreCheckoutQuery
)

# =====================================================================
# 1. CONFIGURACIÓN DEL PANEL MASTER Y SEGURIDAD
# =====================================================================
MASTER_TOKEN = os.getenv("MASTER_TOKEN", "").strip()
MASTER_MONGO_URI = os.getenv("MONGO_URI", "").strip()
PORT = int(os.environ.get("PORT", 8080))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://tu-dominio.onrender.com").rstrip('/')

if not MASTER_MONGO_URI:
    raise RuntimeError(
        "❌ ERROR CRÍTICO: La variable 'MONGO_URI' está vacía o no existe en las variables de entorno."
    )

if not MASTER_TOKEN:
    raise RuntimeError(
        "❌ ERROR CRÍTICO: La variable 'MASTER_TOKEN' no está configurada."
    )

raw_admins = os.getenv("SUPER_ADMINS", "8983189714,7452819858")
SUPER_ADMIN_IDS = [int(i.strip()) for i in raw_admins.split(",") if i.strip().isdigit()]

master_db_client = AsyncIOMotorClient(MASTER_MONGO_URI)
master_db = master_db_client.saas_master_db
active_bots_tasks = {}
master_dp = Dispatcher()
MASTER_BOT_USERNAME = ""

class CreateChildBot(StatesGroup):
    waiting_for_token = State()
    waiting_for_sub_id = State()
    waiting_for_sub_link = State()
    waiting_for_vip_id = State()
    waiting_for_log_id = State()
    waiting_for_paid_vip_id = State()
    waiting_for_db_version = State()

class BotStates(StatesGroup):
    idle = State()
    searching = State()
    chatting = State()
    waiting_trade_type = State()
    waiting_trade_amount = State()
    waiting_for_id = State()

def extract_chat_id(msg: Message) -> str:
    if msg.forward_from_chat:
        return str(msg.forward_from_chat.id)
    if msg.text:
        return msg.text.strip()
    return "0"

def format_progress_bar(current: int, total: int, length: int = 10) -> str:
    ratio = min(max(current / total, 0.0), 1.0)
    filled = int(round(length * ratio))
    return "▰" * filled + "▱" * (length - filled)

# =====================================================================
# 2. SEGURIDAD DE WEBAPP & VALIDACIÓN TELEGRAM
# =====================================================================
def validate_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            return None
        check_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(computed_hash, check_hash):
            return json.loads(parsed_data.get("user", "{}"))
        return None
    except Exception:
        return None

async def authenticate_request(request):
    try:
        bot_id = int(request.query.get("bot_id", 0))
    except (ValueError, TypeError):
        bot_id = 0
        
    bot_ctx = active_bots_tasks.get(bot_id)
    if not bot_ctx and len(active_bots_tasks) == 1:
        bot_id = list(active_bots_tasks.keys())[0]
        bot_ctx = active_bots_tasks[bot_id]

    if not bot_ctx:
        return None, None, None

    init_data = request.headers.get("Authorization", "")
    user_id = None
    
    if init_data:
        user_data = validate_telegram_init_data(init_data, bot_ctx["bot"].token)
        if user_data and "id" in user_data:
            user_id = int(user_data["id"])
        else:
            try:
                parsed = dict(parse_qsl(init_data, keep_blank_values=True))
                if "user" in parsed:
                    u_obj = json.loads(parsed["user"])
                    if "id" in u_obj:
                        user_id = int(u_obj["id"])
            except Exception:
                pass
                
    if not user_id:
        try:
            query_id = int(request.query.get("id") or request.query.get("user_id") or 0)
            if query_id:
                user_id = query_id
        except Exception:
            pass

    if not user_id:
        return None, None, None
        
    return user_id, bot_ctx["db"], bot_ctx["bot"]

# =====================================================================
# 3. ENDPOINTS API Y MINI APP
# =====================================================================
async def api_get_data(request):
    user_id, child_db, bot = await authenticate_request(request)
    if not user_id or child_db is None or not bot:
        return web.json_response({"error": "No autorizado"}, status=401)
        
    dp = active_bots_tasks[bot.id]["dp"]
    active_viewers = dp["active_viewers"]
    now = time.time()
    active_viewers[user_id] = now
    
    for uid in list(active_viewers.keys()):
        if now - active_viewers[uid] > 60:
            active_viewers.pop(uid, None)

    user = await child_db.users.find_one({"_id": user_id}) or {}
    fotos = await child_db.inventory.count_documents({"user_id": user_id, "type": "photo"})
    videos = await child_db.inventory.count_documents({"user_id": user_id, "type": "video"})
    
    top_users = []
    async for u in child_db.users.find().sort("reputation", -1).limit(10):
        if u.get("reputation", 0) > 0:
            top_users.append({"id": u["_id"], "rep": u.get("reputation", 0)})
            
    waiting_list = dp["waiting_list"]
    active_chats = dp["active_chats"]
    active_ids = set(active_viewers.keys()) | set(waiting_list) | set(active_chats.keys())
    
    online_users = []
    for uid in active_ids:
        if uid == user_id:
            continue
        status_txt = "Disponible"
        is_free = True
        if uid in active_chats:
            status_txt = "Ocupado"
            is_free = False
        elif uid in waiting_list:
            status_txt = "Buscando..."
            is_free = False
            
        u_data = await child_db.users.find_one({"_id": uid}) or {}
        online_users.append({
            "id": uid,
            "rep": u_data.get("reputation", 0),
            "status": status_txt,
            "is_free": is_free
        })
    
    last_bonus = user.get("last_bonus", 0)
    time_left_bonus = max(0, int((last_bonus + (6 * 3600)) - now))
    
    return web.json_response({
        "fotos": fotos,
        "videos": videos,
        "reputation": user.get("reputation", 0),
        "referrals": user.get("referrals", 0),
        "time_left": time_left_bonus,
        "leaderboard": top_users,
        "online_users": online_users
    }, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"
    })

async def api_claim_bonus(request):
    user_id, child_db, _ = await authenticate_request(request)
    if not user_id or child_db is None:
        return web.json_response({"error": "No autorizado"}, status=401)
        
    now = time.time()
    cooldown = 6 * 3600
    pts = random.randint(1, 5)
    
    res = await child_db.users.find_one_and_update(
        {
            "_id": user_id, 
            "$or": [
                {"last_bonus": {"$lte": now - cooldown}},
                {"last_bonus": {"$exists": False}},
                {"last_bonus": 0}
            ]
        },
        {"$set": {"last_bonus": now}, "$inc": {"reputation": pts}},
        return_document=True
    )
    
    if not res:
        user = await child_db.users.find_one({"_id": user_id}) or {}
        time_left = max(0, int((user.get("last_bonus", 0) + cooldown) - now))
        return web.json_response({"success": False, "error": "Cooldown activo", "time_left": time_left})
        
    return web.json_response({
        "success": True, 
        "bonus": pts, 
        "new_rep": res.get("reputation", 0), 
        "time_left": cooldown
    })

async def api_clear_inv(request):
    user_id, child_db, _ = await authenticate_request(request)
    if not user_id or child_db is None:
        return web.json_response({"error": "No autorizado"}, status=401)
    await child_db.inventory.delete_many({"user_id": user_id})
    return web.json_response({"success": True})

async def handle_webapp(request):
    html_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Exchange Hub</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.6.0/dist/confetti.browser.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #090d16);
            --secondary-bg: var(--tg-theme-secondary-bg-color, #131927);
            --text-color: var(--tg-theme-text-color, #f8fafc);
            --hint-color: var(--tg-theme-hint-color, #94a3b8);
            --accent-blue: #38bdf8;
            --accent-grad: linear-gradient(135deg, #38bdf8 0%, #2563eb 100%);
            --card-border: rgba(255, 255, 255, 0.08);
            --card-glass: rgba(19, 25, 39, 0.85);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; -webkit-tap-highlight-color: transparent; }
        body { background: var(--bg-color); color: var(--text-color); padding: 16px 16px 110px; }
        
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }
        .header-title { font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 8px; color: #fff; }
        .header-title i { color: var(--accent-blue); }
        .status-pill { font-size: 11px; padding: 4px 10px; border-radius: 20px; background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.25); color: var(--accent-blue); font-weight: 600; }
        
        .section-view { display: none; flex-direction: column; gap: 14px; }
        .section-view.active { display: flex !important; }
        
        .card { background: var(--card-glass); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px); border: 1px solid var(--card-border); border-radius: 20px; padding: 18px; }
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .card-title { font-size: 15px; font-weight: 700; color: #fff; }
        
        .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .stat-box { background: rgba(255, 255, 255, 0.03); border: 1px solid var(--card-border); border-radius: 16px; padding: 14px; text-align: center; }
        .stat-val { font-size: 20px; font-weight: 800; color: #fff; margin-top: 4px; }
        
        .progress-track { height: 8px; background: rgba(255,255,255,0.06); border-radius: 8px; overflow: hidden; margin: 10px 0 6px; }
        .progress-fill { height: 100%; width: 0%; background: var(--accent-grad); border-radius: 8px; transition: width 0.6s ease; }
        
        .btn-action { width: 100%; border: none; border-radius: 14px; padding: 13px; font-size: 13px; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; }
        .btn-outline { background: transparent; border: 1px solid rgba(56, 189, 248, 0.4); color: var(--accent-blue); }
        .btn-danger { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); color: #ef4444; }
        
        .chest-row { display: flex; justify-content: space-around; margin: 20px 0; }
        .chest-card { width: 88px; height: 88px; border-radius: 18px; background: rgba(255,255,255,0.03); border: 1px solid var(--card-border); display: flex; align-items: center; justify-content: center; cursor: pointer; transition: 0.2s transform; }
        .chest-card.ready:active { transform: scale(0.92); }
        .chest-card.disabled { opacity: 0.35; filter: grayscale(1); pointer-events: none; }
        .chest-card i { font-size: 36px; color: #f59e0b; }
        
        .user-row { display: flex; justify-content: space-between; align-items: center; padding: 12px; background: rgba(255,255,255,0.02); border: 1px solid var(--card-border); border-radius: 14px; margin-bottom: 8px; }
        .badge { font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 6px; }
        .badge-free { background: rgba(34, 197, 94, 0.1); color: #22c55e; border: 1px solid rgba(34, 197, 94, 0.2); }
        .badge-busy { background: rgba(239, 68, 68, 0.1); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.2); }

        .nav-dock {
            position: fixed;
            bottom: 16px;
            left: 12px;
            right: 12px;
            background: rgba(19, 25, 39, 0.95);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 20px;
            padding: 6px;
            display: flex;
            justify-content: space-around;
            align-items: center;
            z-index: 99999;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
        }
        .dock-btn {
            flex: 1;
            background: transparent;
            border: none;
            outline: none;
            padding: 8px 4px;
            color: var(--hint-color);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            border-radius: 14px;
            transition: all 0.2s ease;
        }
        .dock-btn i { font-size: 17px; pointer-events: none; }
        .dock-btn span { pointer-events: none; }
        .dock-btn.active {
            background: var(--accent-grad);
            color: #ffffff;
            box-shadow: 0 4px 12px rgba(56, 189, 248, 0.35);
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-title"><i class="fa-solid fa-arrows-split-up-and-left"></i> Exchange Hub</div>
        <div class="status-pill"><i class="fa-solid fa-circle fa-fade"></i> Online</div>
    </div>

    <!-- RADAR -->
    <div id="sec-radar" class="section-view active">
        <div class="card">
            <div class="card-header">
                <span class="card-title"><i class="fa-solid fa-radar"></i> Radar en Vivo</span>
                <button class="status-pill" style="cursor:pointer;" onclick="fetchData()"><i class="fa-solid fa-rotate-right"></i></button>
            </div>
            <div id="radar-list"><p style="color:var(--hint-color); font-size:13px; text-align:center; padding:10px;">Buscando usuarios...</p></div>
        </div>
    </div>

    <!-- BONOS -->
    <div id="sec-bonus" class="section-view">
        <div class="card" style="text-align:center;">
            <span class="card-title" style="display:block; margin-bottom:4px;">Cofre de Recompensa</span>
            <p style="font-size:12px; color:var(--hint-color);">Reclama entre +1 y +5 de reputación cada 6 horas.</p>
            <div class="chest-row">
                <div class="chest-card disabled" onclick="claimChest()"><i class="fa-solid fa-gem"></i></div>
                <div class="chest-card disabled" onclick="claimChest()"><i class="fa-solid fa-vault"></i></div>
                <div class="chest-card disabled" onclick="claimChest()"><i class="fa-solid fa-cube"></i></div>
            </div>
            <p id="bonus-countdown" style="font-size:13px; font-weight:700; color:var(--hint-color);">Sincronizando...</p>
        </div>
    </div>

    <!-- PERFIL -->
    <div id="sec-profile" class="section-view">
        <div class="card">
            <div class="card-header">
                <span class="card-title">Métricas de Reputación</span>
                <span id="vip-ratio" style="font-weight:800; font-size:13px; color:var(--accent-blue);">--/20</span>
            </div>
            <div class="progress-track"><div class="progress-fill" id="vip-fill"></div></div>
            <div class="stat-grid" style="margin-top:14px;">
                <div class="stat-box">
                    <span style="font-size:11px; color:var(--hint-color);">Referidos</span>
                    <div class="stat-val" id="ref-count">0</div>
                </div>
                <div class="stat-box">
                    <span style="font-size:11px; color:var(--hint-color);">Reputación</span>
                    <div class="stat-val" id="rep-count">0</div>
                </div>
            </div>
            <button class="btn-action btn-outline" style="margin-top:14px;" onclick="copyLink()"><i class="fa-solid fa-share-nodes"></i> Enlace de Invitación</button>
        </div>
        <div class="card">
            <span class="card-title" style="display:block; margin-bottom:10px;">Caja Fuerte Multimedia</span>
            <div class="stat-grid">
                <div class="stat-box"><i class="fa-regular fa-images"></i><div class="stat-val" id="cnt-photos">0</div></div>
                <div class="stat-box"><i class="fa-solid fa-film"></i><div class="stat-val" id="cnt-videos">0</div></div>
            </div>
            <button class="btn-action btn-danger" style="margin-top:14px;" onclick="wipeInventory()"><i class="fa-solid fa-trash"></i> Vaciar Inventario</button>
        </div>
    </div>

    <!-- TOP -->
    <div id="sec-top" class="section-view">
        <div class="card">
            <div class="card-header"><span class="card-title"><i class="fa-solid fa-trophy"></i> Top 10 Red</span></div>
            <div id="leaderboard-list">Cargando clasificación...</div>
        </div>
    </div>

    <!-- BARRA DOCK NAVEGABLE -->
    <div class="nav-dock">
        <button type="button" class="dock-btn active" data-target="sec-radar" onclick="switchSection('sec-radar', this)">
            <i class="fa-solid fa-satellite-dish"></i>
            <span>Radar</span>
        </button>
        <button type="button" class="dock-btn" data-target="sec-bonus" onclick="switchSection('sec-bonus', this)">
            <i class="fa-solid fa-gift"></i>
            <span>Bonus</span>
        </button>
        <button type="button" class="dock-btn" data-target="sec-profile" onclick="switchSection('sec-profile', this)">
            <i class="fa-solid fa-id-badge"></i>
            <span>Perfil</span>
        </button>
        <button type="button" class="dock-btn" data-target="sec-top" onclick="switchSection('sec-top', this)">
            <i class="fa-solid fa-crown"></i>
            <span>Top</span>
        </button>
    </div>

    <script>
        const tg = window.Telegram?.WebApp;
        if (tg) {
            try { tg.expand(); tg.ready(); } catch(e) {}
        }

        const params = new URLSearchParams(window.location.search);
        const botUsername = params.get('bot') || "";
        const botId = params.get('bot_id') || "";
        const userId = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) ? tg.initDataUnsafe.user.id : (params.get('user_id') || "0");

        const headers = {
            "Content-Type": "application/json",
            "Authorization": (tg && tg.initData) ? tg.initData : ""
        };

        function switchSection(sectionId, btnElement) {
            try {
                if (tg && tg.HapticFeedback && typeof tg.HapticFeedback.selectionChanged === 'function') {
                    tg.HapticFeedback.selectionChanged();
                }
            } catch(err) {}

            document.querySelectorAll('.section-view').forEach(s => {
                s.classList.remove('active');
                s.style.display = 'none';
            });

            document.querySelectorAll('.dock-btn').forEach(b => {
                b.classList.remove('active');
            });

            const target = document.getElementById(sectionId);
            if (target) {
                target.classList.add('active');
                target.style.display = 'flex';
            }

            if (btnElement) {
                btnElement.classList.add('active');
            }
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        let isBonusReady = false;
        let timerInterval;
        function renderTimer(seconds) {
            clearInterval(timerInterval);
            const display = document.getElementById("bonus-countdown");
            const cards = document.querySelectorAll(".chest-card");
            
            if (seconds <= 0) {
                isBonusReady = true;
                display.innerText = "¡Cofre listo! Toca para abrir";
                display.style.color = "#22c55e";
                cards.forEach(c => { c.classList.remove('disabled'); c.classList.add('ready'); });
                return;
            }
            
            isBonusReady = false;
            cards.forEach(c => { c.classList.add('disabled'); c.classList.remove('ready'); });
            display.style.color = "var(--hint-color)";
            
            let s = seconds;
            timerInterval = setInterval(() => {
                s--;
                if (s <= 0) renderTimer(0);
                else {
                    let h = Math.floor(s / 3600);
                    let m = Math.floor((s % 3600) / 60);
                    let sec = s % 60;
                    display.innerText = `Disponible en: ${h}h ${m}m ${sec}s`;
                }
            }, 1000);
        }

        async function fetchData() {
            try {
                const res = await fetch(`/api/data?bot_id=${botId}&id=${userId}&t=${Date.now()}`, { headers });
                const d = await res.json();
                if (d.error) return;

                document.getElementById("cnt-photos").innerText = d.fotos;
                document.getElementById("cnt-videos").innerText = d.videos;
                document.getElementById("rep-count").innerText = d.reputation;
                document.getElementById("ref-count").innerText = d.referrals;
                document.getElementById("vip-ratio").innerText = `${d.reputation}/20`;
                document.getElementById("vip-fill").style.width = Math.min(100, (d.reputation / 20) * 100) + "%";

                renderTimer(d.time_left);

                const radarList = document.getElementById("radar-list");
                if (!d.online_users || d.online_users.length === 0) {
                    radarList.innerHTML = '<p style="font-size:12px; color:var(--hint-color); text-align:center; padding:10px;">No hay otros usuarios en línea.</p>';
                } else {
                    radarList.innerHTML = d.online_users.map(u => `
                        <div class="user-row">
                            <div>
                                <div style="font-weight:700; font-size:13px;">ID: ${u.id}</div>
                                <span class="badge ${u.is_free ? 'badge-free' : 'badge-busy'}">${u.status}</span>
                                <span style="font-size:11px; color:var(--hint-color); margin-left:6px;">⭐ ${u.rep}</span>
                            </div>
                            ${u.is_free ? `<button class="btn-action btn-outline" style="width:auto; padding:6px 12px; font-size:12px;" onclick="connectUser('${u.id}')">Conectar</button>` : ''}
                        </div>
                    `).join('');
                }

                const lb = document.getElementById("leaderboard-list");
                lb.innerHTML = d.leaderboard.map((u, i) => `
                    <div class="user-row">
                        <span><strong>#${i+1}</strong> ID: ${u.id}</span>
                        <span style="font-weight:800; color:var(--accent-blue);">${u.rep} PTS</span>
                    </div>
                `).join('') || '<p style="color:var(--hint-color); font-size:12px;">Sin datos aún.</p>';
            } catch (e) {}
        }

        async function claimChest() {
            if (!isBonusReady) return;
            try {
                if (tg?.HapticFeedback?.impactOccurred) tg.HapticFeedback.impactOccurred('medium');
            } catch(e) {}
            
            try {
                const res = await fetch(`/api/bonus?bot_id=${botId}&id=${userId}&t=${Date.now()}`, { method: "POST", headers, body: "{}" });
                const d = await res.json();
                if (d.success) {
                    try {
                        confetti({ particleCount: 120, spread: 80, origin: { y: 0.6 } });
                        if (tg?.HapticFeedback?.notificationOccurred) tg.HapticFeedback.notificationOccurred('success');
                    } catch(e) {}
                    if (tg?.showAlert) tg.showAlert(`🎉 ¡Ganaste +${d.bonus} Puntos de Reputación!`);
                    else alert(`🎉 ¡Ganaste +${d.bonus} Puntos de Reputación!`);
                    fetchData();
                } else {
                    if (tg?.showAlert) tg.showAlert("⚠️ Cooldown activo.");
                    fetchData();
                }
            } catch(e) {}
        }

        function connectUser(targetId) {
            try {
                if (tg?.HapticFeedback?.impactOccurred) tg.HapticFeedback.impactOccurred('light');
            } catch(e) {}
            if (tg?.openTelegramLink) {
                tg.openTelegramLink(`https://t.me/${botUsername}?start=connect_${targetId}`);
            } else {
                window.location.href = `https://t.me/${botUsername}?start=connect_${targetId}`;
            }
        }

        function copyLink() {
            try {
                if (tg?.HapticFeedback?.notificationOccurred) tg.HapticFeedback.notificationOccurred('success');
            } catch(e) {}
            const link = `https://t.me/${botUsername}?start=${userId}`;
            navigator.clipboard.writeText(link).then(() => {
                if (tg?.showAlert) tg.showAlert("Enlace copiado al portapapeles.");
                else alert("Enlace copiado.");
            });
        }

        function wipeInventory() {
            const confirmMsg = "¿Eliminar todos tus archivos de forma permanente?";
            if (tg?.showConfirm) {
                tg.showConfirm(confirmMsg, async (ok) => {
                    if (ok) {
                        await fetch(`/api/clear?bot_id=${botId}&id=${userId}`, { method: "POST", headers });
                        try { if (tg?.HapticFeedback?.notificationOccurred) tg.HapticFeedback.notificationOccurred('warning'); } catch(e) {}
                        fetchData();
                    }
                });
            } else if (confirm(confirmMsg)) {
                fetch(`/api/clear?bot_id=${botId}&id=${userId}`, { method: "POST", headers }).then(fetchData);
            }
        }

        fetchData();
        setInterval(fetchData, 15000);
    </script>
</body>
</html>"""
    return web.Response(
        text=html_content, 
        content_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

# =====================================================================
# 4. CORE SAAS: DISPATCHER BOT HIJO
# =====================================================================
def get_new_child_dp(child_config: dict, child_db) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    
    active_chats = {}
    waiting_list = []
    pending_trades = {}
    active_viewers = {}
    chat_threads = {}
    pending_notifications = {}
    backup_queue = asyncio.Queue()
    
    dp["backup_queue"] = backup_queue
    dp["active_viewers"] = active_viewers
    dp["active_chats"] = active_chats
    dp["waiting_list"] = waiting_list

    FORCE_SUB_CHANNEL_ID = int(child_config.get("force_sub_id", 0)) if child_config.get("force_sub_id") else 0
    FORCE_SUB_CHANNEL_LINK = child_config.get("force_sub_link", "")
    VIP_GROUP_ID = int(child_config.get("vip_group_id", 0)) if child_config.get("vip_group_id") else 0
    LOG_GROUP_ID = int(child_config.get("log_group_id", 0)) if child_config.get("log_group_id") else 0
    PAID_VIP_CHANNEL_ID = int(child_config.get("paid_vip_channel_id", 0)) if child_config.get("paid_vip_channel_id") else 0

    async def get_or_create_chat_topic(bot: Bot, u_id: int, t_id: int):
        if not LOG_GROUP_ID:
            return None
        if u_id in chat_threads:
            return chat_threads[u_id]
        if t_id in chat_threads:
            chat_threads[u_id] = chat_threads[t_id]
            return chat_threads[t_id]
        try:
            topic = await bot.create_forum_topic(chat_id=LOG_GROUP_ID, name=f"Chat {u_id} & {t_id}")
            chat_threads[u_id] = topic.message_thread_id
            chat_threads[t_id] = topic.message_thread_id
            return topic.message_thread_id
        except Exception as e:
            logging.error(f"Error creando tema de foro: {e}")
            return None

    async def set_other_user_state(bot: Bot, chat_id: int, state: State):
        key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
        await FSMContext(storage=dp.storage, key=key).set_state(state)

    async def get_user(user_id):
        user = await child_db.users.find_one({"_id": user_id})
        if not user:
            user = {
                "_id": user_id, "lang": "es", "referrals": 0, "reputation": 0,
                "mode": "anon", "in_vip": False, "notified_vip": False,
                "last_bonus": 0, "vip_until": 0, "paid_vip_active": False,
                "blacklisted": False
            }
            await child_db.users.insert_one(user)
        return user

    async def save_user(user_id, data):
        await child_db.users.update_one({"_id": user_id}, {"$set": data}, upsert=True)

    async def is_maintenance_mode():
        cfg = await child_db.settings.find_one({"_id": "config"})
        return cfg.get("maintenance", False) if cfg else False

    async def is_blacklisted(user_id):
        u = await get_user(user_id)
        return u.get("blacklisted", False)

    async def check_force_sub(user_id, bot: Bot):
        if user_id in SUPER_ADMIN_IDS or not FORCE_SUB_CHANNEL_ID:
            return True
        try:
            member = await bot.get_chat_member(FORCE_SUB_CHANNEL_ID, user_id)
            return member.status in ["member", "administrator", "creator"]
        except Exception:
            return False

    async def check_vip_status(user_id, bot: Bot):
        if not VIP_GROUP_ID:
            return
        try:
            user = await get_user(user_id)
            if (user.get("referrals", 0) >= 3 or user.get("reputation", 0) >= 20) and not user.get("notified_vip"):
                invite = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
                lang = user.get("lang", "es")
                btn = "🌟 Entrar al Grupo VIP" if lang == "es" else "🌟 Join VIP Group"
                msg = "🎉 <b>¡Acceso al Grupo VIP desbloqueado!</b> Enlace exclusivo:" if lang == "es" else "🎉 <b>VIP Access Granted!</b> Exclusive link:"
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn, url=invite.invite_link)]])
                await bot.send_message(user_id, msg, reply_markup=kb, parse_mode="HTML")
                await save_user(user_id, {"notified_vip": True, "in_vip": True})
        except Exception as e:
            logging.error(f"Error en check_vip_status: {e}")

    async def send_rating_request(user_id, target_id, bot: Bot):
        user = await get_user(user_id)
        lang = user.get("lang", "es")
        btn_g = "👍 Buen usuario" if lang == "es" else "👍 Good user"
        btn_b = "👎 Malo" if lang == "es" else "👎 Bad"
        msg = "¿Deseas otorgarle un punto de reputación extra a tu compañero?" if lang == "es" else "Do you want to give a bonus reputation point to your partner?"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=btn_g, callback_data=f"rate_good_{target_id}"),
            InlineKeyboardButton(text=btn_b, callback_data=f"rate_bad_{target_id}")
        ]])
        await bot.send_message(user_id, msg, reply_markup=kb)

    async def send_delayed_notification(u_id, lang, bot: Bot):
        await asyncio.sleep(2.5)
        total = await child_db.inventory.count_documents({"user_id": u_id})
        msg = (
            f"📥 <b>Lote guardado en tu cofre.</b> (Total en inventario: <code>{total}</code>)\n\n"
            f"⚠️ <b>IMPORTANTE:</b> ¡No elimines los mensajes que acabas de subir aquí! Si los borras del chat, el bot no podrá reenviarlos y tus intercambios fallarán."
            if lang == "es" else
            f"📥 <b>Batch saved to your vault.</b> (Total in inventory: <code>{total}</code>)\n\n"
            f"⚠️ <b>IMPORTANT:</b> Do not delete uploaded messages from this chat! If deleted, the bot cannot forward them and your trades will fail."
        )
        try:
            await bot.send_message(u_id, msg, parse_mode="HTML")
        except Exception:
            pass
        finally:
            pending_notifications.pop(u_id, None)

    # Cálculo preciso de archivos totales vs únicos con respecto al compañero
    async def get_inventory_stats_for_trade(sender_id: int, receiver_id: int, category: str = "mixed"):
        total_query = {"user_id": sender_id}
        if category != "mixed":
            total_query["type"] = category
        total_count = await child_db.inventory.count_documents(total_query)

        already_sent = [doc["file_unique_id"] async for doc in child_db.exchange_history.find(
            {"sender_id": sender_id, "receiver_id": receiver_id}, {"file_unique_id": 1}
        )]
        
        unique_query = {"user_id": sender_id, "file_unique_id": {"$nin": already_sent}}
        if category != "mixed":
            unique_query["type"] = category
        unique_count = await child_db.inventory.count_documents(unique_query)
        return total_count, unique_count

    async def get_random_batch(sender_id: int, receiver_id: int, category: str, amount: int):
        already_sent = [doc["file_unique_id"] async for doc in child_db.exchange_history.find({"sender_id": sender_id, "receiver_id": receiver_id}, {"file_unique_id": 1})]
        match_query = {"user_id": sender_id, "file_unique_id": {"$nin": already_sent}}
        if category != "mixed":
            match_query["type"] = category
        pipeline = [{"$match": match_query}, {"$sample": {"size": (amount * 2) + 50}}]
        selected = [doc async for doc in child_db.inventory.aggregate(pipeline)]
        return len(selected) >= amount, selected

    async def show_main_menu(user_id, bot: Bot):
        user = await get_user(user_id)
        lang = user.get("lang", "es")
        bot_info = await bot.get_me()
        my_link = f"https://t.me/{bot_info.username}?start={user['_id']}"
        v_ts = int(time.time())
        webapp_url = f"{RENDER_URL}/?bot={bot_info.username}&bot_id={bot.id}&user_id={user_id}&v={v_ts}"
        
        btn_panel = "✨ Mini App de Intercambio" if lang == "es" else "✨ Exchange Mini App"
        btn_rnd = "💬 Buscar Chat" if lang == "es" else "💬 Random Chat"
        btn_id = "🆔 Conectar ID" if lang == "es" else "🆔 Connect ID"
        btn_manual = "📖 Manual de Uso" if lang == "es" else "📖 User Guide"
        btn_prof = "👤 Mi Perfil" if lang == "es" else "👤 My Profile"
        btn_lang = "⚙️ Idioma / Lang"
        btn_share = "🔗 Compartir Link" if lang == "es" else "🔗 Share Link"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_panel, web_app=WebAppInfo(url=webapp_url))],
            [InlineKeyboardButton(text=btn_rnd, callback_data="find_chat"), InlineKeyboardButton(text=btn_id, callback_data="connect_id")],
            [InlineKeyboardButton(text=btn_manual, callback_data="show_manual"), InlineKeyboardButton(text=btn_prof, callback_data="my_profile")],
            [InlineKeyboardButton(text=btn_lang, callback_data="change_lang"), InlineKeyboardButton(text=btn_share, url=f"https://t.me/share/url?url={my_link}")]
        ])

        txt = (
            "👋 <b>¡Bienvenido a la Red P2P de Intercambios!</b>\n\n"
            "⚠️ <b>REGLA CRÍTICA:</b> Sube videos o fotos directamente a este chat para llenar tu cofre privado. "
            "<b>No elimines los archivos que subas aquí</b>; si los borras, el bot no podrá reenviarlos y no podrás intercambiar.\n\n"
            "💡 <i>¿Tienes dudas de cómo funciona? Toca el botón de <b>Manual de Uso</b> abajo.</i>"
        ) if lang == "es" else (
            "👋 <b>Welcome to the P2P Exchange Network!</b>\n\n"
            "⚠️ <b>CRITICAL RULE:</b> Upload videos or photos directly to this chat to load your private vault. "
            "<b>Do not delete the files you upload here</b>; if deleted, the bot cannot forward them and your trades will fail.\n\n"
            "💡 <i>New here? Tap the <b>User Guide</b> button below to learn how it works.</i>"
        )
        await bot.send_message(chat_id=user_id, text=txt, reply_markup=kb, parse_mode="HTML")

    # ---- Manual de Uso en Dos Idiomas ----
    async def render_manual_text(lang: str) -> str:
        if lang == "es":
            return (
                "📖 <b>MANUAL DE USO — GUÍA COMPLETA</b>\n\n"
                "1️⃣ <b>Cargar tu Cofre:</b>\n"
                "• Envía fotos o videos a este chat privado con el bot.\n"
                "• Quedarán guardados automáticamente en tu inventario.\n"
                "• ⚠️ <b>ADVERTENCIA ESTRICTA:</b> Nunca borres los mensajes multimedia originales que subas. Si los eliminas del chat, el bot no podrá reenviarlos durante un trade y la transacción fallará.\n\n"
                "2️⃣ <b>Búsqueda de Chat:</b>\n"
                "• Al presionar <b>«Buscar Chat»</b>, entras a una sala de espera.\n"
                "• <i>Para que la conexión se concrete, otra persona debe presionar ese mismo botón o enviarte solicitud.</i> No te salgas, el bot te avisará cuando alguien conecte.\n"
                "• Si conoces el ID de un amigo, usa <b>«Conectar ID»</b> para emparejarse directamente.\n\n"
                "3️⃣ <b>Intercambios Seguros (Trades):</b>\n"
                "• Dentro de un chat conectado, presiona <b>«🤝 Proponer Intercambio»</b>.\n"
                "• El bot comprobará tu cofre y te dirá cuántos archivos tienes en total y cuántos son <b>únicos</b> (que tu compañero aún no ha recibido).\n"
                "• Elige la categoría (fotos, videos o mixto) y la cantidad.\n"
                "• Cuando ambos aceptan, el bot intercambia los archivos de manera 100% automatizada e imparcial.\n\n"
                "4️⃣ <b>Reputación y Grupo VIP:</b>\n"
                "• Cada intercambio exitoso suma +1 Reputación.\n"
                "• Abre el Cofre en la Mini App cada 6 horas para ganar hasta +5 puntos gratis.\n"
                "• Con <b>20 Puntos</b> o <b>3 Referidos</b> desbloqueas acceso automático al <b>Grupo VIP Gratuito</b>."
            )
        else:
            return (
                "📖 <b>USER GUIDE — COMPLETE TUTORIAL</b>\n\n"
                "1️⃣ <b>Loading your Vault:</b>\n"
                "• Send photos or videos directly to this private chat.\n"
                "• They will be automatically saved into your private inventory.\n"
                "• ⚠️ <b>STRICT WARNING:</b> Never delete the original media messages you upload here! If you delete them, the bot won't be able to forward them during a trade and the exchange will fail.\n\n"
                "2️⃣ <b>Finding a Chat:</b>\n"
                "• When you tap <b>«Random Chat»</b>, you enter a waiting queue.\n"
                "• <i>For the connection to happen, another user must also tap that button or send you a request.</i> Stay in the queue, the bot will notify you as soon as someone joins.\n"
                "• If you know a friend's ID, use <b>«Connect ID»</b> to connect directly.\n\n"
                "3️⃣ <b>Safe P2P Trading:</b>\n"
                "• Once connected, tap <b>«🤝 Propose Trade»</b>.\n"
                "• The bot will inspect your vault and display both your total files and <b>unique unrepeated files</b> for that specific partner.\n"
                "• Select category (photos, videos, mixed) and quantity.\n"
                "• Once both parties accept, delivery is executed automatically.\n\n"
                "4️⃣ <b>Reputation & VIP Access:</b>\n"
                "• Every completed trade awards +1 Reputation.\n"
                "• Open the Reward Chest in the Mini App every 6 hours for up to +5 points.\n"
                "• Reaching <b>20 Reputation</b> or <b>3 Referrals</b> grants immediate free access to the <b>VIP Group</b>."
            )

    @dp.callback_query(F.data == "show_manual")
    async def cb_manual(callback: CallbackQuery):
        user = await get_user(callback.from_user.id)
        lang = user.get("lang", "es")
        txt = await render_manual_text(lang)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Volver al Menú" if lang == "es" else "⬅️ Back to Menu", callback_data="back_main")]])
        await callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

    @dp.message(Command("manual"))
    async def cmd_manual(message: Message):
        user = await get_user(message.from_user.id)
        lang = user.get("lang", "es")
        txt = await render_manual_text(lang)
        await message.answer(txt, parse_mode="HTML")

    # ---- Comandos Administrativos del Bot Hijo ----
    @dp.message(Command("add_receiver"))
    async def cmd_add_receiver(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            new_id = int(message.text.split()[1])
            await child_db.settings.update_one({"_id": "config"}, {"$addToSet": {"extra_receivers": new_id}}, upsert=True)
            await message.answer(f"✅ Receptor <code>{new_id}</code> agregado.")
        except Exception:
            await message.answer("Uso: <code>/add_receiver ID</code>")

    @dp.message(Command("del_receiver"))
    async def cmd_del_receiver(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            rem_id = int(message.text.split()[1])
            await child_db.settings.update_one({"_id": "config"}, {"$pull": {"extra_receivers": rem_id}}, upsert=True)
            await message.answer(f"✅ Receptor <code>{rem_id}</code> eliminado.")
        except Exception:
            await message.answer("Uso: <code>/del_receiver ID</code>")

    @dp.message(Command("mantenimiento"))
    async def cmd_maintenance(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        cfg = await child_db.settings.find_one({"_id": "config"})
        new_state = not (cfg.get("maintenance", False) if cfg else False)
        await child_db.settings.update_one({"_id": "config"}, {"$set": {"maintenance": new_state}}, upsert=True)
        await message.answer(f"🛠️ Mantenimiento: <b>{'Activado 🔴' if new_state else 'Desactivado 🟢'}</b>")

    @dp.message(Command("blacklist"))
    async def cmd_blacklist(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            target_id = int(message.text.split()[1])
            await save_user(target_id, {"blacklisted": True})
            await message.answer(f"🚫 Usuario <code>{target_id}</code> bloqueado.")
        except Exception:
            await message.answer("Uso: <code>/blacklist ID</code>")

    @dp.message(Command("unblacklist"))
    async def cmd_unblacklist(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            target_id = int(message.text.split()[1])
            await save_user(target_id, {"blacklisted": False})
            await message.answer(f"✅ Usuario <code>{target_id}</code> desbloqueado.")
        except Exception:
            await message.answer("Uso: <code>/unblacklist ID</code>")

    @dp.message(Command("broadcast"))
    async def cmd_broadcast(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        text = message.text.replace("/broadcast", "").strip()
        if not text: return await message.answer("Escribe el mensaje tras el comando.")
        await message.answer("⏳ Transmitiendo aviso...")
        count = 0
        async for u in child_db.users.find():
            try:
                await bot.send_message(u["_id"], f"📢 <b>Aviso General:</b>\n\n{html.quote(text)}", parse_mode="HTML")
                count += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await message.answer(f"✅ Difusión completada a <code>{count}</code> usuarios.")

    @dp.message(Command("estadisticas"))
    async def cmd_stats(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        users = await child_db.users.count_documents({})
        files = await child_db.inventory.count_documents({})
        trades = (await child_db.exchange_history.count_documents({})) // 2
        vips = await child_db.users.count_documents({"in_vip": True})
        active_c = len(active_chats) // 2
        txt = (
            "📊 <b>ESTADÍSTICAS DEL NODO</b>\n\n"
            f"👥 Usuarios Registrados: <code>{users}</code>\n"
            f"🌟 Usuarios VIP: <code>{vips}</code>\n"
            f"📁 Archivos en Cofre: <code>{files}</code>\n"
            f"🔄 Intercambios Realizados: <code>{trades}</code>\n"
            f"💬 Chats en Vivo: <code>{active_c}</code>"
        )
        await message.answer(txt, parse_mode="HTML")

    # ---- Flujos Principales ----
    @dp.message(CommandStart(), StateFilter("*"))
    async def cmd_start(message: Message, state: FSMContext, bot: Bot):
        user_id = message.from_user.id
        if await is_blacklisted(user_id): return
        if await is_maintenance_mode() and user_id not in SUPER_ADMIN_IDS:
            return await message.answer("🛠 <b>Bot en Mantenimiento.</b> Vuelve más tarde.")

        await state.clear()
        if user_id in waiting_list: waiting_list.remove(user_id)
        
        t_id = active_chats.pop(user_id, None)
        if t_id:
            active_chats.pop(t_id, None)
            await set_other_user_state(bot, t_id, BotStates.idle)
            try:
                await bot.send_message(t_id, "❌ <b>El otro usuario ha regresado al menú principal.</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
                await show_main_menu(t_id, bot)
            except Exception: pass
            chat_threads.pop(user_id, None)
            chat_threads.pop(t_id, None)

        user = await get_user(user_id)
        args = message.text.split(maxsplit=1)
        lang = user.get("lang", "es")

        # Conexión directa Deep Link
        if len(args) > 1 and args[1].startswith("connect_"):
            target_str = args[1].split("_")[1]
            if target_str.isdigit():
                t_id = int(target_str)
                if t_id != user_id and t_id not in active_chats and t_id not in waiting_list:
                    t_user = await get_user(t_id)
                    t_lang = t_user.get("lang", "es")
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="✅ Aceptar" if t_lang == "es" else "✅ Accept", callback_data=f"accept_id_{user_id}"),
                        InlineKeyboardButton(text="❌ Rechazar" if t_lang == "es" else "❌ Reject", callback_data=f"reject_id_{user_id}")
                    ]])
                    try:
                        await bot.send_message(t_id, f"🔔 <b>Solicitud de Chat de ID:</b> <code>{user_id}</code>", reply_markup=kb, parse_mode="HTML")
                        await message.answer("⏳ Solicitud de chat enviada." if lang == "es" else "⏳ Chat request sent.")
                    except Exception: pass
                    return await state.set_state(BotStates.idle)

        # Sistema de referidos
        if len(args) > 1 and args[1].isdigit() and not user.get("started_bot"):
            inviter = int(args[1])
            if inviter != user_id:
                try:
                    await save_user(user_id, {"referred_by": inviter})
                    await child_db.users.update_one({"_id": inviter}, {"$inc": {"referrals": 1}})
                    await check_vip_status(inviter, bot)
                except Exception: pass

        await save_user(user_id, {"started_bot": True})

        if not await check_force_sub(user_id, bot):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Unirse al Canal" if lang == "es" else "📢 Join Channel", url=FORCE_SUB_CHANNEL_LINK)],
                [InlineKeyboardButton(text="✅ Verificar" if lang == "es" else "✅ Verify", callback_data="verify_sub")]
            ])
            return await message.answer("🛑 <b>Acceso Restringido</b>\nDebes unirte a nuestro canal para usar el servicio.", reply_markup=kb, parse_mode="HTML")

        await show_main_menu(user_id, bot)
        await state.set_state(BotStates.idle)

    @dp.callback_query(F.data == "verify_sub")
    async def verify_sub(callback: CallbackQuery, bot: Bot):
        if await is_blacklisted(callback.from_user.id): return
        if await check_force_sub(callback.from_user.id, bot):
            await callback.message.delete()
            await show_main_menu(callback.from_user.id, bot)
        else:
            await callback.answer("⚠️ No se ha detectado tu suscripción.", show_alert=True)

    @dp.callback_query(F.data == "change_lang")
    async def change_lang(callback: CallbackQuery, bot: Bot):
        u = await get_user(callback.from_user.id)
        new_lang = "en" if u.get("lang") == "es" else "es"
        await save_user(callback.from_user.id, {"lang": new_lang})
        await callback.answer("Idioma actualizado" if new_lang == "es" else "Language updated")
        await callback.message.delete()
        await show_main_menu(callback.from_user.id, bot)

    @dp.callback_query(F.data == "my_profile")
    async def show_profile(callback: CallbackQuery, bot: Bot):
        u_id = callback.from_user.id
        await check_vip_status(u_id, bot)
        user = await get_user(u_id)
        fotos = await child_db.inventory.count_documents({"user_id": u_id, "type": "photo"})
        videos = await child_db.inventory.count_documents({"user_id": u_id, "type": "video"})
        rep = user.get("reputation", 0)
        prog_bar = format_progress_bar(rep, 20)
        
        vip_expires = user.get("vip_until", 0)
        vip_txt = f"Hasta {datetime.fromtimestamp(vip_expires).strftime('%d/%m %H:%M')}" if vip_expires > time.time() else "Inactivo ❌"
        modo_txt = "🕵️‍♂️ Anónimo" if user.get("mode") == "anon" else "👤 Público"

        kb_list = [
            [InlineKeyboardButton(text="⭐ Comprar VIP 7 Días (Stars)", url=f"https://t.me/{MASTER_BOT_USERNAME}?start=paystars_{bot.id}")],
            [InlineKeyboardButton(text="🔄 Cambiar Modo", callback_data="toggle_mode")]
        ]
        
        if VIP_GROUP_ID and (user.get("in_vip") or user.get("referrals", 0) >= 3 or rep >= 20):
            try:
                inv = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
                kb_list.insert(0, [InlineKeyboardButton(text="🌟 Grupo VIP Gratuito", url=inv.invite_link)])
            except Exception: pass

        if PAID_VIP_CHANNEL_ID and user.get("vip_until", 0) > time.time():
            try:
                inv_p = await bot.create_chat_invite_link(chat_id=PAID_VIP_CHANNEL_ID, member_limit=1)
                kb_list.insert(0, [InlineKeyboardButton(text="💎 Canal VIP de Pago", url=inv_p.invite_link)])
            except Exception: pass

        kb_list.append([InlineKeyboardButton(text="⬅️ Volver", callback_data="back_main")])

        txt = (
            f"👤 <b>Tu Perfil</b>\n\n"
            f"🆔 ID: <code>{u_id}</code>\n"
            f"🌟 Reputación: <code>{rep}/20</code>\n"
            f"<code>[{prog_bar}]</code>\n"
            f"👥 Referidos: <code>{user.get('referrals', 0)}/3</code>\n"
            f"⭐ VIP Stars: <b>{vip_txt}</b>\n"
            f"🎭 Modo: <b>{modo_txt}</b>\n\n"
            f"📦 Caja Fuerte: 📷 {fotos} | 🎥 {videos}"
        )
        await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list), parse_mode="HTML")

    @dp.callback_query(F.data == "toggle_mode")
    async def toggle_mode(callback: CallbackQuery, bot: Bot):
        u = await get_user(callback.from_user.id)
        new_mode = "public" if u.get("mode") == "anon" else "anon"
        await save_user(callback.from_user.id, {"mode": new_mode})
        await show_profile(callback, bot)

    @dp.callback_query(F.data == "back_main")
    async def back_main(callback: CallbackQuery, state: FSMContext, bot: Bot):
        await state.set_state(BotStates.idle)
        await callback.message.delete()
        await show_main_menu(callback.from_user.id, bot)

    # Conexión manual por ID
    @dp.callback_query(F.data == "connect_id")
    async def ask_for_id(callback: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Cancelar", callback_data="back_main")]])
        await callback.message.edit_text("✏️ Escribe el <b>ID numérico</b> del usuario:", reply_markup=kb, parse_mode="HTML")

    @dp.message(StateFilter(BotStates.waiting_for_id), ~F.text.startswith("/"))
    async def process_connect_id(message: Message, state: FSMContext, bot: Bot):
        u_id = message.from_user.id
        if not message.text.isdigit():
            return await message.answer("⚠️ Debe ser un ID numérico.")
        t_id = int(message.text)
        if t_id == u_id: return
        if t_id in active_chats or t_id in waiting_list:
            return await message.answer("⚠️ El usuario está ocupado actualmente.")
        
        t_user = await get_user(t_id)
        t_lang = t_user.get("lang", "es")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Aceptar" if t_lang == "es" else "✅ Accept", callback_data=f"accept_id_{u_id}"),
            InlineKeyboardButton(text="❌ Rechazar" if t_lang == "es" else "❌ Reject", callback_data=f"reject_id_{u_id}")
        ]])
        try:
            await bot.send_message(t_id, f"🔔 <b>Solicitud de Chat recibida de:</b> <code>{u_id}</code>", reply_markup=kb, parse_mode="HTML")
            await message.answer("⏳ Solicitud enviada.")
        except Exception:
            await message.answer("❌ No se pudo entregar la solicitud.")
        await state.set_state(BotStates.idle)

    @dp.callback_query(F.data.startswith("accept_id_"))
    async def accept_id_conn(callback: CallbackQuery, state: FSMContext, bot: Bot):
        t_id = int(callback.data.split("_")[2])
        u_id = callback.from_user.id
        if t_id in active_chats or u_id in active_chats:
            return await callback.answer("Uno de los usuarios ya está ocupado.", show_alert=True)
            
        active_chats[u_id], active_chats[t_id] = t_id, u_id
        await state.set_state(BotStates.chatting)
        await set_other_user_state(bot, t_id, BotStates.chatting)
        
        for uid in (u_id, t_id):
            u_obj = await get_user(uid)
            lng = u_obj.get("lang", "es")
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"),
                           KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]],
                resize_keyboard=True
            )
            await bot.send_message(uid, "✅ <b>¡Conexión establecida!</b>", reply_markup=kb, parse_mode="HTML")
        await callback.message.delete()

    @dp.callback_query(F.data.startswith("reject_id_"))
    async def reject_id_conn(callback: CallbackQuery, bot: Bot):
        req_id = int(callback.data.split("_")[2])
        try:
            await bot.send_message(req_id, "❌ <b>Tu solicitud de chat fue rechazada.</b>", parse_mode="HTML")
        except Exception: pass
        await callback.message.delete()

    # Búsqueda aleatoria con mensaje de sala de espera explícito
    @dp.callback_query(F.data == "find_chat")
    async def find_chat(callback: CallbackQuery, state: FSMContext, bot: Bot):
        u_id = callback.from_user.id
        if await is_blacklisted(u_id): return
        if u_id in active_chats or u_id in waiting_list:
            return await callback.answer("Ya estás en una sesión o en espera.", show_alert=True)

        user = await get_user(u_id)
        lang = user.get("lang", "es")

        if waiting_list:
            t_id = waiting_list.pop(0)
            if t_id == u_id:
                waiting_list.append(u_id)
                return await callback.answer("Buscando...")
                
            active_chats[u_id], active_chats[t_id] = t_id, u_id
            await state.set_state(BotStates.chatting)
            await set_other_user_state(bot, t_id, BotStates.chatting)
            
            for uid in (u_id, t_id):
                u_obj = await get_user(uid)
                lng = u_obj.get("lang", "es")
                kb = ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"),
                               KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]],
                    resize_keyboard=True
                )
                await bot.send_message(uid, "✅ <b>¡Chat emparejado con éxito!</b> Ya pueden hablar o intercambiar.", reply_markup=kb, parse_mode="HTML")
            await callback.message.delete()
        else:
            waiting_list.append(u_id)
            await state.set_state(BotStates.searching)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancelar Búsqueda" if lang == "es" else "❌ Cancel Queue", callback_data="leave_chat")]])
            txt = (
                "🔍 <b>Buscando compañero de intercambio...</b>\n\n"
                "⏳ <i>Estás en la sala de espera. Para que la conexión se complete, <b>otro usuario debe presionar «Buscar Chat»</b> o enviarte una solicitud directa. En cuanto alguien más entre, se emparejarán automáticamente.</i>"
                if lang == "es" else
                "🔍 <b>Searching for trade partner...</b>\n\n"
                "⏳ <i>You are now in the queue. For the connection to establish, <b>another user must also tap «Random Chat»</b> or send you a request. You will be paired automatically once someone joins.</i>"
            )
            await callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

    @dp.message(F.text.in_(["❌ Desconectar", "❌ Disconnect"]))
    @dp.callback_query(F.data == "leave_chat")
    async def leave_chat(event, state: FSMContext, bot: Bot):
        u_id = event.from_user.id
        if u_id in waiting_list: waiting_list.remove(u_id)
        t_id = active_chats.pop(u_id, None)
        
        if t_id:
            active_chats.pop(t_id, None)
            await set_other_user_state(bot, t_id, BotStates.idle)
            try:
                await bot.send_message(t_id, "❌ <b>Tu compañero abandonó la sesión.</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
                await show_main_menu(t_id, bot)
            except Exception: pass
            
        chat_threads.pop(u_id, None)
        if t_id: chat_threads.pop(t_id, None)
        await state.set_state(BotStates.idle)
        
        if isinstance(event, Message):
            await event.answer("Has salido de la sesión.", reply_markup=ReplyKeyboardRemove())
        else:
            await event.message.delete()
            await bot.send_message(u_id, "Has salido de la sesión.", reply_markup=ReplyKeyboardRemove())
        await show_main_menu(u_id, bot)

    # Ingesta multimedia
    @dp.message(F.chat.type == "private", F.photo | F.video | F.document)
    async def handle_media(message: Message, bot: Bot):
        u_id = message.from_user.id
        if await is_blacklisted(u_id): return
        user = await get_user(u_id)
        
        media = message.photo[-1] if message.photo else (message.video or message.document)
        file_id, file_unique_id = media.file_id, media.file_unique_id
        m_type = "photo" if message.photo else ("video" if message.video else "document")

        if not await child_db.global_files.find_one({"_id": file_unique_id}):
            await child_db.global_files.insert_one({"_id": file_unique_id})
            await backup_queue.put({"file_id": file_id, "type": m_type, "user_id": u_id, "name": message.from_user.full_name})

        if u_id in active_chats:
            target = active_chats[u_id]
            try:
                await message.forward(target)
                thread_id = await get_or_create_chat_topic(bot, u_id, target)
                if thread_id and LOG_GROUP_ID:
                    await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=f"📎 <code>{u_id}</code> envió un archivo ({m_type}).", parse_mode="HTML")
            except Exception: pass
            return

        if not await child_db.inventory.find_one({"user_id": u_id, "file_unique_id": file_unique_id}):
            await child_db.inventory.insert_one({
                "user_id": u_id, "file_id": file_id, "message_id": message.message_id,
                "file_unique_id": file_unique_id, "type": m_type
            })
            if u_id not in pending_notifications:
                pending_notifications[u_id] = True
                asyncio.create_task(send_delayed_notification(u_id, user.get("lang", "es"), bot))

    # Motor de Intercambios con notificación de archivos totales vs únicos
    @dp.message(StateFilter(BotStates.chatting), F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade"]))
    async def btn_propose(message: Message, state: FSMContext):
        u_id = message.from_user.id
        t_id = active_chats.get(u_id)
        if not t_id:
            return await message.answer("⚠️ No tienes ningún chat activo.")

        user = await get_user(u_id)
        lng = user.get("lang", "es")
        
        tot, unq = await get_inventory_stats_for_trade(u_id, t_id, "mixed")
        
        if unq == 0:
            msg_no = (
                f"⚠️ <b>Inventario agotado para este usuario:</b>\n\n"
                f"• Total en tu cofre: <code>{tot}</code>\n"
                f"• <b>Archivos únicos disponibles:</b> <code>0</code> (Ya le has transferido todos tus archivos o tu cofre está vacío).\n\n"
                f"📥 <i>Sube más videos o fotos al bot para poder proponer un intercambio.</i>"
                if lng == "es" else
                f"⚠️ <b>No unrepeated files for this user:</b>\n\n"
                f"• Total in vault: <code>{tot}</code>\n"
                f"• <b>Unique files available:</b> <code>0</code> (All files have already been traded to this partner or your vault is empty).\n\n"
                f"📥 <i>Upload more media to this chat to continue trading.</i>"
            )
            return await message.answer(msg_no, parse_mode="HTML")

        await state.set_state(BotStates.waiting_trade_type)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Fotos" if lng == "es" else "📷 Photos", callback_data="settype_photo"),
             InlineKeyboardButton(text="🎥 Videos", callback_data="settype_video")],
            [InlineKeyboardButton(text="🔀 Mixto" if lng == "es" else "🔀 Mixed", callback_data="settype_mixed")]
        ])
        
        msg = (
            f"📊 <b>Estado de tu Inventario con este usuario:</b>\n"
            f"• Archivos totales en tu cofre: <code>{tot}</code>\n"
            f"• <b>Archivos únicos listos para enviar:</b> <code>{unq}</code>\n\n"
            f"⚠️ <i>Recuerda: Si eliminaste los mensajes originales del chat, no podrán reenviarse.</i>\n\n"
            f"🎬 <b>¿Qué categoría deseas intercambiar?</b>"
            if lng == "es" else
            f"📊 <b>Your Inventory Status with this user:</b>\n"
            f"• Total files in vault: <code>{tot}</code>\n"
            f"• <b>Unique unrepeated files:</b> <code>{unq}</code>\n\n"
            f"⚠️ <i>Remember: If you deleted original messages from the chat, delivery will fail.</i>\n\n"
            f"🎬 <b>What category do you want to trade?</b>"
        )
        await message.answer(msg, reply_markup=kb, parse_mode="HTML")

    @dp.callback_query(StateFilter(BotStates.waiting_trade_type), F.data.startswith("settype_"))
    async def process_trade_type(callback: CallbackQuery, state: FSMContext):
        u_id = callback.from_user.id
        t_id = active_chats.get(u_id)
        if not t_id:
            return await callback.answer("Chat desconectado.", show_alert=True)

        user = await get_user(u_id)
        lng = user.get("lang", "es")
        t_type = callback.data.split("_")[1]
        await state.update_data(trade_type=t_type)
        
        tot_cat, unq_cat = await get_inventory_stats_for_trade(u_id, t_id, t_type)
        await state.update_data(max_unique=unq_cat)
        
        if unq_cat == 0:
            err_msg = (
                f"⚠️ No tienes archivos únicos de categoría <b>{t_type}</b> disponibles para este usuario."
                if lng == "es" else
                f"⚠️ You don't have any unique <b>{t_type}</b> files available for this user."
            )
            return await callback.message.edit_text(err_msg, parse_mode="HTML")

        await state.set_state(BotStates.waiting_trade_amount)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="10x10", callback_data="trade_10"),
            InlineKeyboardButton(text="50x50", callback_data="trade_50"),
            InlineKeyboardButton(text="100x100", callback_data="trade_100")
        ]])
        
        msg = (
            f"📁 Categoría seleccionada: <b>{t_type.capitalize()}</b>\n"
            f"✨ Tienes <b>{unq_cat}</b> archivos únicos disponibles (de {tot_cat} totales en cofre).\n\n"
            f"🔢 <b>¿Cuántos archivos deseas intercambiar?</b> Elige una opción o escribe un número:"
            if lng == "es" else
            f"📁 Selected category: <b>{t_type.capitalize()}</b>\n"
            f"✨ You have <b>{unq_cat}</b> unique files available (out of {tot_cat} in vault).\n\n"
            f"🔢 <b>How many files do you want to trade?</b> Choose an option or type a number:"
        )
        await callback.message.edit_text(msg, reply_markup=kb, parse_mode="HTML")

    async def execute_trade_proposal(u_id, amt, t_type, send_func, state, bot: Bot):
        t_id = active_chats.get(u_id)
        if not t_id: return await state.set_state(BotStates.idle)
        
        user, t_user = await get_user(u_id), await get_user(t_id)
        lang, t_lang = user.get("lang", "es"), t_user.get("lang", "es")
        
        _, unq_available = await get_inventory_stats_for_trade(u_id, t_id, t_type)
        if amt > unq_available:
            err_amt = (
                f"⚠️ <b>Cantidad no disponible:</b>\n"
                f"Has solicitado <b>{amt}</b> archivos, pero solo tienes <b>{unq_available}</b> archivos únicos sin repetir de esta categoría para este usuario.\n\n"
                f"Por favor, elige una cantidad menor o sube más archivos."
                if lang == "es" else
                f"⚠️ <b>Amount not available:</b>\n"
                f"You requested <b>{amt}</b> files, but you only have <b>{unq_available}</b> unrepeated unique files in this category.\n\n"
                f"Please choose a smaller amount or upload more media."
            )
            return await send_func(err_amt, parse_mode="HTML")

        pending_trades[t_id] = {"sender": u_id, "amount": amt, "type": t_type}
        await state.set_state(BotStates.chatting)
        await get_or_create_chat_topic(bot, u_id, t_id)

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Aceptar" if t_lang == "es" else "✅ Accept", callback_data="accept_trade"),
            InlineKeyboardButton(text="❌ Rechazar" if t_lang == "es" else "❌ Reject", callback_data="reject_trade")
        ]])
        
        await send_func(f"⏳ Propuesta de trade <b>{amt}x{amt}</b> ({t_type}) enviada. Esperando confirmación...", parse_mode="HTML")
        await bot.send_message(t_id, f"🤝 <b>¡Oferta de Trade Recibida!</b>\nPropuesta: <b>{amt}x{amt}</b> ({t_type}). ¿Aceptas?", reply_markup=kb, parse_mode="HTML")

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
        
        await callback.message.edit_text("✅ <i>Comprobando inventarios en base de datos...</i>", parse_mode="HTML")
        ok_s, files_s = await get_random_batch(s_id, u_id, t_type, amt)
        ok_r, files_r = await get_random_batch(u_id, s_id, t_type, amt)
        
        if not ok_s or not ok_r:
            err = "⚠️ Uno de los dos usuarios no cuenta con suficientes archivos únicos para completar este trade."
            await callback.message.edit_text(err)
            return await bot.send_message(s_id, err)

        await callback.message.edit_text("✅ <i>Procesando intercambio seguro de archivos...</i>", parse_mode="HTML")
        await bot.send_message(s_id, "✅ <i>Procesando intercambio seguro de archivos...</i>", parse_mode="HTML")

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
            await asyncio.sleep(0.2)

        if sent_s == 0 and sent_r == 0:
            fail = "❌ Intercambio fallido: Los mensajes originales fueron eliminados del chat por los usuarios."
            await bot.send_message(u_id, fail)
            return await bot.send_message(s_id, fail)

        thread_id = chat_threads.get(u_id) or chat_threads.get(s_id)
        if thread_id and LOG_GROUP_ID:
            rep_log = f"🔄 <b>Intercambio Finalizado</b>\n• Remitente 1: <code>{s_id}</code> (Enviados: {sent_s})\n• Remitente 2: <code>{u_id}</code> (Enviados: {sent_r})\n• Tipo: {t_type}"
            try: await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=rep_log, parse_mode="HTML")
            except Exception: pass

        await child_db.users.update_one({"_id": u_id}, {"$inc": {"reputation": 1}})
        await child_db.users.update_one({"_id": s_id}, {"$inc": {"reputation": 1}})
        await check_vip_status(u_id, bot)
        await check_vip_status(s_id, bot)

        await bot.send_message(u_id, f"🎉 <b>¡Trade completado con éxito!</b> Recibiste {sent_s} archivos. (+1 Reputación)", parse_mode="HTML")
        await bot.send_message(s_id, f"🎉 <b>¡Trade completado con éxito!</b> Recibiste {sent_r} archivos. (+1 Reputación)", parse_mode="HTML")

        await send_rating_request(u_id, s_id, bot)
        await send_rating_request(s_id, u_id, bot)

    @dp.callback_query(F.data == "reject_trade")
    async def reject_trade(callback: CallbackQuery, bot: Bot):
        trade = pending_trades.pop(callback.from_user.id, None)
        if trade:
            try: await bot.send_message(trade["sender"], "❌ La propuesta de trade fue rechazada.")
            except Exception: pass
        await callback.message.edit_text("❌ Oferta rechazada.")

    @dp.callback_query(F.data.startswith("rate_"))
    async def process_rating(callback: CallbackQuery, bot: Bot):
        action, _, t_id_str = callback.data.split("_")
        t_id = int(t_id_str)
        if action == "good":
            await child_db.users.update_one({"_id": t_id}, {"$inc": {"reputation": 1}})
            await check_vip_status(t_id, bot)
        await callback.message.edit_text("✅ Valoración registrada.")

    # Retransmisión de texto
    @dp.message(StateFilter(BotStates.chatting), ~F.text.startswith("/"), ~F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade", "❌ Desconectar", "❌ Disconnect"]))
    async def relay_msg(message: Message, bot: Bot):
        u_id = message.from_user.id
        if await is_blacklisted(u_id): return
        target = active_chats.get(u_id)
        if target:
            try:
                await bot.send_message(target, f"💬 {html.quote(message.text)}", parse_mode="HTML")
                thread_id = await get_or_create_chat_topic(bot, u_id, target)
                if thread_id and LOG_GROUP_ID:
                    await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=f"💬 <code>{u_id}</code>: {html.quote(message.text)}", parse_mode="HTML")
            except Exception: pass

    # Aprobación de entrada a grupo VIP
    @dp.chat_join_request()
    async def process_vip_join(join_req: ChatJoinRequest, bot: Bot):
        if VIP_GROUP_ID and join_req.chat.id == VIP_GROUP_ID:
            user = await get_user(join_req.from_user.id)
            if user.get("referrals", 0) >= 3 or user.get("reputation", 0) >= 20:
                await join_req.approve()
                try: await bot.send_message(join_req.from_user.id, "🎉 ¡Tu solicitud al Grupo VIP gratuito ha sido aprobada!")
                except Exception: pass
            else:
                await join_req.decline()
                try: await bot.send_message(join_req.from_user.id, "❌ No cumples los requisitos mínimos (3 referidos o 20 de reputación).")
                except Exception: pass

    return dp

# =====================================================================
# 5. WATCHDOGS Y WORKERS EN SEGUNDO PLANO
# =====================================================================
async def child_message_worker(bot_id: int):
    bot = active_bots_tasks[bot_id]["bot"]
    queue = active_bots_tasks[bot_id]["dp"]["backup_queue"]
    child_db = active_bots_tasks[bot_id]["db"]
    
    try:
        while True:
            item = await queue.get()
            try:
                caption = f"👤 Remitente: {html.quote(item.get('name', 'Usuario'))} (<code>{item['user_id']}</code>)"
                file_id, m_type = item["file_id"], item["type"]
                doc = await child_db.settings.find_one({"_id": "config"})
                receivers = list(set(SUPER_ADMIN_IDS + (doc.get("extra_receivers", []) if doc else [])))
                
                for r_id in receivers:
                    try:
                        if m_type == "photo": await bot.send_photo(r_id, file_id, caption=caption, parse_mode="HTML")
                        elif m_type == "video": await bot.send_video(r_id, file_id, caption=caption, parse_mode="HTML")
                        else: await bot.send_document(r_id, file_id, caption=caption, parse_mode="HTML")
                        await asyncio.sleep(0.5)
                    except Exception: pass
            except Exception as e: logging.error(f"Error backup worker: {e}")
            finally: queue.task_done()
    except asyncio.CancelledError: pass

async def background_vip_cleaner_runner(bot: Bot, child_db, PAID_VIP_CHANNEL_ID: int):
    while True:
        try:
            now = time.time()
            cursor = child_db.users.find({"paid_vip_active": True, "vip_until": {"$gt": 0, "$lt": now}})
            async for u in cursor:
                uid = u["_id"]
                if PAID_VIP_CHANNEL_ID:
                    try:
                        await bot.ban_chat_member(PAID_VIP_CHANNEL_ID, uid)
                        await bot.unban_chat_member(PAID_VIP_CHANNEL_ID, uid)
                        await bot.send_message(uid, "⚠️ Tu membresía Stars de 7 días ha finalizado.")
                    except Exception: pass
                await child_db.users.update_one({"_id": uid}, {"$set": {"paid_vip_active": False, "vip_until": 0}})
        except Exception as e: logging.error(f"Error en limpiador VIP: {e}")
        await asyncio.sleep(3600)

async def isolate_and_cleanup_bot(bot_id: int, revoked: bool = False):
    if bot_id not in active_bots_tasks: return
    tasks = active_bots_tasks.pop(bot_id)
    for k in ["polling_task", "worker_task", "vip_cleaner_task"]:
        if k in tasks: tasks[k].cancel()
    await tasks["bot"].session.close()
    if revoked:
        await master_db.child_bots.update_one({"bot_token": tasks["bot"].token}, {"$set": {"status": "revoked"}})

async def health_check_monitor(master_bot: Bot):
    while True:
        await asyncio.sleep(600)
        for b_id, d in list(active_bots_tasks.items()):
            try:
                await d["bot"].get_me()
            except TelegramUnauthorizedError:
                await isolate_and_cleanup_bot(b_id, revoked=True)
                for admin_id in SUPER_ADMIN_IDS:
                    try: await master_bot.send_message(admin_id, f"🚨 <b>Alerta Anti-Ban:</b> Bot con ID <code>{b_id}</code> revocado.")
                    except Exception: pass
            except Exception: pass

async def child_polling_wrapper(dp: Dispatcher, bot: Bot, bot_id: int):
    try:
        # Elimina cualquier webhook previo del bot hijo antes de hacer polling
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, handle_signals=False)
    except TelegramUnauthorizedError:
        await isolate_and_cleanup_bot(bot_id, revoked=True)
    except asyncio.CancelledError:
        pass

async def start_child_bot(config: dict) -> bool:
    token = config["bot_token"]
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        me = await bot.get_me()
        bot_id = me.id
    except Exception:
        await bot.session.close()
        return False

    if bot_id in active_bots_tasks: return True
    db_ver = config.get("db_version", "v1")
    child_db = master_db_client[f"child_{bot_id}_{db_ver}"]
    dp = get_new_child_dp(config, child_db)
    
    paid_channel_id = int(config.get("paid_vip_channel_id", 0)) if config.get("paid_vip_channel_id") else 0
    
    active_bots_tasks[bot_id] = {
        "bot": bot, "db": child_db, "dp": dp,
        "polling_task": asyncio.create_task(child_polling_wrapper(dp, bot, bot_id)),
        "worker_task": asyncio.create_task(child_message_worker(bot_id)),
        "vip_cleaner_task": asyncio.create_task(background_vip_cleaner_runner(bot, child_db, paid_channel_id))
    }
    return True

# =====================================================================
# 6. HANDLERS MASTER BOT (WIZARD 7 PASOS Y STARS)
# =====================================================================
@master_dp.message(CommandStart())
async def cmd_start_master(message: Message, state: FSMContext, bot: Bot):
    args = message.text.split(maxsplit=1)
    
    if len(args) > 1 and args[1].startswith("paystars_"):
        target_bot_id_str = args[1].replace("paystars_", "")
        if target_bot_id_str.isdigit():
            target_bot_id = int(target_bot_id_str)
            prices = [LabeledPrice(label="Pase VIP 7 Días", amount=25)]
            payload = f"vip_stars_{target_bot_id}_{message.from_user.id}"
            try:
                await bot.send_invoice(
                    chat_id=message.from_user.id,
                    title="Pase VIP 7 Días (Canal de Pago)",
                    description="Acceso exclusivo por 1 semana procesado centralmente por el Master.",
                    payload=payload,
                    provider_token="",
                    currency="XTR",
                    prices=prices
                )
                return
            except Exception as e:
                logging.error(f"Error generando factura Stars: {e}")
                return await message.answer("❌ Error al generar la factura.")

    if message.from_user.id not in SUPER_ADMIN_IDS:
        return await message.answer("👋 Bienvenido al servicio centralizado de la red.")

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Crear Nuevo Bot", callback_data="master_crear")],
        [InlineKeyboardButton(text="📊 Administrar Bots Activos", callback_data="master_panel")]
    ])
    await message.answer("🛠 <b>Panel de Control SaaS Master</b>", reply_markup=kb, parse_mode="HTML")

@master_dp.pre_checkout_query()
async def process_pre_checkout(pre_q: PreCheckoutQuery):
    await pre_q.answer(ok=True)

@master_dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("vip_stars_"):
        parts = payload.split("_")
        target_bot_id, user_id = int(parts[2]), int(parts[3])
        child_info = active_bots_tasks.get(target_bot_id)
        if not child_info:
            return await message.answer("✅ Pago recibido. Contacta a soporte si tarda en activarse.")

        child_db = child_info["db"]
        child_bot = child_info["bot"]
        cfg = await master_db.child_bots.find_one({"bot_token": child_bot.token})
        paid_ch = int(cfg.get("paid_vip_channel_id", 0)) if cfg and cfg.get("paid_vip_channel_id") else 0
        
        user = await child_db.users.find_one({"_id": user_id}) or {}
        now = time.time()
        base = max(now, user.get("vip_until", 0))
        new_vip = base + (7 * 86400)
        
        await child_db.users.update_one({"_id": user_id}, {"$set": {"vip_until": new_vip, "paid_vip_active": True}}, upsert=True)
        
        if paid_ch:
            try:
                inv = await child_bot.create_chat_invite_link(chat_id=paid_ch, member_limit=1)
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Entrar al Canal VIP", url=inv.invite_link)]])
                await message.answer("🎉 <b>¡Pago con Estrellas confirmado!</b> Acceso exclusivo de 7 días:", reply_markup=kb, parse_mode="HTML")
            except Exception:
                await message.answer("🎉 <b>¡Pago confirmado!</b> Membresía actualizada en base de datos.")
        else:
            await message.answer("🎉 <b>¡Pago confirmado!</b> Tu tiempo VIP de 7 días ha sido registrado.")

# Wizard 7 Pasos
@master_dp.callback_query(F.data == "master_crear")
async def step1_token(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    await callback.message.edit_text("🤖 <b>Paso 1/7:</b> Envía el <b>Token</b> del bot dado por @BotFather:", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_token)

@master_dp.message(CreateChildBot.waiting_for_token)
async def step2_sub_id(message: Message, state: FSMContext):
    await state.update_data(token=message.text.strip())
    await message.answer("📢 <b>Paso 2/7:</b> Envía el <b>ID del Canal de Suscripción Obligatoria</b> (o reenvía un mensaje):", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_sub_id)

@master_dp.message(CreateChildBot.waiting_for_sub_id)
async def step3_sub_link(message: Message, state: FSMContext):
    await state.update_data(sub_id=extract_chat_id(message))
    await message.answer("🔗 <b>Paso 3/7:</b> Envía el <b>Enlace de invitación</b> del canal obligatorio:", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_sub_link)

@master_dp.message(CreateChildBot.waiting_for_sub_link)
async def step4_vip_id(message: Message, state: FSMContext):
    await state.update_data(sub_link=message.text.strip())
    await message.answer("🌟 <b>Paso 4/7:</b> Envía el <b>ID del Grupo VIP Gratuito</b> (referidos/puntos):", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_vip_id)

@master_dp.message(CreateChildBot.waiting_for_vip_id)
async def step5_log_id(message: Message, state: FSMContext):
    await state.update_data(vip_id=extract_chat_id(message))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Omitir Logs", callback_data="skip_logs")]])
    await message.answer("📂 <b>Paso 5/7:</b> Envía el <b>ID del Grupo de Logs</b>:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_log_id)

@master_dp.callback_query(CreateChildBot.waiting_for_log_id, F.data == "skip_logs")
async def step5_skip_logs(callback: CallbackQuery, state: FSMContext):
    await state.update_data(log_id="0")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Omitir VIP Paga", callback_data="skip_paid_vip")]])
    await callback.message.edit_text("💎 <b>Paso 6/7:</b> Envía el <b>ID del Canal VIP de Paga (Stars)</b>:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_paid_vip_id)

@master_dp.message(CreateChildBot.waiting_for_log_id)
async def step5_msg_logs(message: Message, state: FSMContext):
    await state.update_data(log_id=extract_chat_id(message))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Omitir VIP Paga", callback_data="skip_paid_vip")]])
    await message.answer("💎 <b>Paso 6/7:</b> Envía el <b>ID del Canal VIP de Paga (Stars)</b>:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_paid_vip_id)

@master_dp.callback_query(CreateChildBot.waiting_for_paid_vip_id, F.data == "skip_paid_vip")
async def step6_skip_paid_vip(callback: CallbackQuery, state: FSMContext):
    await state.update_data(paid_vip_id="0")
    await callback.message.edit_text("🗄️ <b>Paso 7/7:</b> Escribe el identificador de la <b>Versión de Base de Datos</b> (ej: <code>v1</code>):", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_db_version)

@master_dp.message(CreateChildBot.waiting_for_paid_vip_id)
async def step6_msg_paid_vip(message: Message, state: FSMContext):
    await state.update_data(paid_vip_id=extract_chat_id(message))
    await message.answer("🗄️ <b>Paso 7/7:</b> Escribe el identificador de la <b>Versión de Base de Datos</b> (ej: <code>v1</code>):", parse_mode="HTML")
    await state.set_state(CreateChildBot.waiting_for_db_version)

@master_dp.message(CreateChildBot.waiting_for_db_version)
async def step7_final(message: Message, state: FSMContext):
    db_ver = message.text.strip() or "v1"
    data = await state.get_data()
    
    new_cfg = {
        "owner_id": message.from_user.id,
        "bot_token": data["token"],
        "status": "active",
        "force_sub_id": data["sub_id"],
        "force_sub_link": data["sub_link"],
        "vip_group_id": data["vip_id"],
        "log_group_id": data.get("log_id", "0"),
        "paid_vip_channel_id": data.get("paid_vip_id", "0"),
        "db_version": db_ver,
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    await message.answer("⏳ <i>Desplegando bot hijo...</i>", parse_mode="HTML")
    success = await start_child_bot(new_cfg)
    
    if success:
        await master_db.child_bots.insert_one(new_cfg)
        temp_bot = Bot(token=data["token"])
        try:
            me = await temp_bot.get_me()
        finally:
            await temp_bot.session.close()
        
        summary = (
            "🎉 <b>¡BOT HIJO ACTIVO Y EN LÍNEA!</b>\n\n"
            f"🤖 Usuario: <code>@{me.username}</code> (ID: <code>{me.id}</code>)\n"
            f"📢 Canal Sub: <code>{data['sub_id']}</code>\n"
            f"🌟 VIP Gratis: <code>{data['vip_id']}</code>\n"
            f"💎 VIP Stars: <code>{data.get('paid_vip_id', '0')}</code>\n"
            f"📋 Logs: <code>{data.get('log_id', '0')}</code>\n"
            f"🗄️ Versión BD: <code>{db_ver}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Volver al Panel", callback_data="master_panel")]])
        await message.answer(summary, reply_markup=kb, parse_mode="HTML")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Reintentar", callback_data="master_crear")]])
        await message.answer("❌ Error: Token inválido o problema al iniciar sesión.", reply_markup=kb, parse_mode="HTML")
    await state.clear()

@master_dp.callback_query(F.data == "master_panel")
async def cb_master_panel(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    cursor = master_db.child_bots.find({"status": "active"})
    bots_list = [b async for b in cursor]
    txt = f"📊 <b>Bots Activos en Red:</b> <code>{len(bots_list)}</code>\n\n"
    keyboard = []
    
    for b in bots_list:
        temp_b = Bot(token=b["bot_token"])
        try:
            me = await temp_b.get_me()
            txt += f"• <b>@{me.username}</b> (ID: <code>{me.id}</code>)\n"
            keyboard.append([InlineKeyboardButton(text=f"⚙️ Administrar @{me.username}", callback_data=f"manage_bot_{me.id}")])
        except Exception:
            txt += "• <i>Bot Inaccesible</i>\n"
        finally:
            await temp_b.session.close()
            
    keyboard.append([InlineKeyboardButton(text="➕ Crear Nuevo Bot", callback_data="master_crear")])
    await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")

@master_dp.callback_query(F.data.startswith("manage_bot_"))
async def cb_manage_bot(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    bot_id = int(callback.data.split("_")[2])
    bot_ctx = active_bots_tasks.get(bot_id)
    if not bot_ctx:
        return await callback.answer("⚠️ Bot inactivo o no encontrado.", show_alert=True)
        
    child_db = bot_ctx["db"]
    u_count = await child_db.users.count_documents({})
    f_count = await child_db.inventory.count_documents({})
    cfg = await master_db.child_bots.find_one({"bot_token": bot_ctx["bot"].token}) or {}
    
    me = await bot_ctx["bot"].get_me()
    txt = (
        f"⚙️ <b>Gestión de Nodo: @{me.username}</b>\n\n"
        f"🆔 Bot ID: <code>{bot_id}</code>\n"
        f"👥 Usuarios: <code>{u_count}</code>\n"
        f"📁 Archivos guardados: <code>{f_count}</code>\n"
        f"🗄️ Versión BD: <code>{cfg.get('db_version', 'v1')}</code>\n"
        f"📢 Canal Obligatorio: <code>{cfg.get('force_sub_id', 'No')}</code>\n"
        f"🌟 Grupo VIP: <code>{cfg.get('vip_group_id', 'No')}</code>\n"
        f"💎 VIP Stars: <code>{cfg.get('paid_vip_channel_id', 'No')}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Detener / Desconectar Bot", callback_data=f"stop_bot_{bot_id}")],
        [InlineKeyboardButton(text="⬅️ Volver a Lista", callback_data="master_panel")]
    ])
    await callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

@master_dp.callback_query(F.data.startswith("stop_bot_"))
async def cb_stop_bot(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    bot_id = int(callback.data.split("_")[2])
    await isolate_and_cleanup_bot(bot_id, revoked=True)
    await callback.answer("Bot detenido y marcado como inactivo.", show_alert=True)
    await cb_master_panel(callback)

# =====================================================================
# 7. INICIO Y SERVIDOR WEB
# =====================================================================
async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_webapp)
    app.router.add_get("/api/data", api_get_data)
    app.router.add_post("/api/bonus", api_claim_bonus)
    app.router.add_post("/api/clear", api_clear_inv)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    return runner

async def main():
    global MASTER_BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    
    if not MASTER_TOKEN or not MASTER_MONGO_URI:
        raise RuntimeError("Configura MASTER_TOKEN y MONGO_URI en tus variables de entorno.")
        
    master_bot = Bot(token=MASTER_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    me = await master_bot.get_me()
    MASTER_BOT_USERNAME = me.username
    
    # 1. Limpieza inmediata del webhook en el bot Master
    await master_bot.delete_webhook(drop_pending_updates=True)
    
    # 2. Iniciar servidor web aiohttp
    runner = await web_server()
    
    # 3. Restaurar e iniciar bots hijos (cada uno limpiará su webhook al arrancar)
    cursor = master_db.child_bots.find({"status": "active"})
    async for cfg in cursor:
        await start_child_bot(cfg)
        
    asyncio.create_task(health_check_monitor(master_bot))
    print(f"🚀 SaaS Master (@{MASTER_BOT_USERNAME}) online en puerto {PORT}.")
    
    try:
        await master_dp.start_polling(master_bot)
    finally:
        await master_bot.session.close()
        for bid in list(active_bots_tasks.keys()):
            await isolate_and_cleanup_bot(bid)
        master_db_client.close()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())