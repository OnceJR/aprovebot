import os
import hmac
import hashlib
import asyncio
import logging
import time
import random
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl
import json
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient

from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import (
    TelegramUnauthorizedError,
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramForbiddenError
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, WebAppInfo,
    ChatJoinRequest, LabeledPrice, PreCheckoutQuery, TelegramObject,
    InputMediaPhoto, InputMediaVideo
)

# =====================================================================
# 1. CONFIGURACIÓN DEL PANEL MASTER Y VARIABLES DE ENTORNO
# =====================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

MASTER_TOKEN = os.getenv("MASTER_TOKEN", "").strip()
MASTER_MONGO_URI = os.getenv("MONGO_URI", "").strip()
PORT = int(os.environ.get("PORT", 8080))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://tu-dominio.onrender.com").rstrip('/')

if not MASTER_MONGO_URI:
    raise RuntimeError("❌ ERROR CRÍTICO: La variable 'MONGO_URI' no está configurada.")

if not MASTER_TOKEN:
    raise RuntimeError("❌ ERROR CRÍTICO: La variable 'MASTER_TOKEN' no está configurada.")

raw_admins = os.getenv("SUPER_ADMINS", "")
SUPER_ADMIN_IDS = [int(i.strip()) for i in raw_admins.split(",") if i.strip().isdigit()]

# Reglas de negocio globales
VIP_MIN_REFERRALS = 3
VIP_MIN_REPUTATION = 20
VIP_DURATION_DAYS = 7
BONUS_COOLDOWN_SECONDS = 6 * 3600

# Planes VIP escalonados con Telegram Stars
VIP_TIERS = {
    "1d": {"days": 1, "stars": 5, "label": "Pase VIP 24 Horas"},
    "7d": {"days": 7, "stars": 25, "label": "Pase VIP 7 Días"},
    "30d": {"days": 30, "stars": 80, "label": "Pase VIP 30 Días"}
}

# Regex anti-enlaces y phishing para chats privados entre usuarios
LINK_REGEX = re.compile(r'(https?://\S+|t\.me/\S+|@[a-zA-Z0-9_]{4,}|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/\S*)?)', re.IGNORECASE)

master_db_client = AsyncIOMotorClient(MASTER_MONGO_URI)
master_db = master_db_client.saas_master_db
active_bots_tasks = {}
active_vip_bots_tasks = {}
master_dp = Dispatcher()
MASTER_BOT_USERNAME = ""
GLOBAL_MASTER_BOT: Bot = None

class CreateChildBot(StatesGroup):
    waiting_for_token = State()
    waiting_for_sub_id = State()
    waiting_for_sub_link = State()
    waiting_for_vip_id = State()
    waiting_for_log_id = State()
    waiting_for_paid_vip_id = State()
    waiting_for_db_version = State()

class CreateVipManagerBot(StatesGroup):
    waiting_for_token = State()
    waiting_for_paid_vip_id = State()
    waiting_for_vip_group_id = State()

class BotStates(StatesGroup):
    idle = State()
    searching = State()
    chatting = State()
    waiting_trade_type = State()
    waiting_trade_amount = State()
    waiting_for_id = State()

def clean_chat_id(val) -> int:
    """Sanitiza y normaliza cualquier ID asegurando el prefijo -100."""
    if not val:
        return 0
    s = str(val).strip()
    if not s or s in ("0", "None"):
        return 0
    if s.isdigit() and len(s) >= 9:
        s = f"-100{s}"
    elif s.startswith("-") and not s.startswith("-100") and len(s) >= 10:
        s = f"-100{s.lstrip('-')}"
    try:
        return int(s)
    except ValueError:
        return 0

def extract_chat_id(msg: Message) -> str:
    if msg.forward_from_chat:
        return str(clean_chat_id(msg.forward_from_chat.id))
    if msg.text:
        cleaned = clean_chat_id(msg.text.strip())
        return str(cleaned) if cleaned != 0 else msg.text.strip()
    return "0"

def format_progress_bar(current: int, total: int, length: int = 10) -> str:
    ratio = min(max(current / total, 0.0), 1.0)
    filled = int(round(length * ratio))
    return "▰" * filled + "▱" * (length - filled)

async def notify_admins_alert(text: str):
    """Notifica diagnósticos o fallos a los administradores."""
    if not GLOBAL_MASTER_BOT: return
    for admin_id in SUPER_ADMIN_IDS:
        try:
            await GLOBAL_MASTER_BOT.send_message(chat_id=admin_id, text=f"🚨 <b>Alerta del Sistema:</b>\n{text}", parse_mode="HTML")
        except Exception:
            pass

async def create_invite_link_smart(bot: Bot, chat_id: int) -> tuple[str | None, str | None]:
    if not chat_id:
        return None, "Chat ID no configurado o es 0"

    last_error = None
    bots_to_try = [bot]
    if GLOBAL_MASTER_BOT and GLOBAL_MASTER_BOT.id != bot.id:
        bots_to_try.append(GLOBAL_MASTER_BOT)

    for b in bots_to_try:
        try:
            inv = await b.create_chat_invite_link(chat_id=chat_id, member_limit=1)
            return inv.invite_link, None
        except Exception as e:
            last_error = str(e)
            logging.warning(f"Fallo crear link con member_limit=1 en {chat_id} con bot {b.id}: {e}")

        try:
            inv = await b.create_chat_invite_link(chat_id=chat_id)
            return inv.invite_link, None
        except Exception as e:
            last_error = str(e)
            logging.warning(f"Fallo crear link sin límite en {chat_id} con bot {b.id}: {e}")

    return None, last_error

# =====================================================================
# 2. MIDDLEWARE ANTI-SPAM (COMPATIBLE CON ÁLBUMES Y RÁFAGAS)
# =====================================================================
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 0.8):
        self.limit = limit
        self.cache = {}

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            if event.media_group_id or event.photo or event.video or event.document:
                return await handler(event, data)

        user = getattr(event, "from_user", None)
        if user and user.id not in SUPER_ADMIN_IDS:
            now = time.time()
            last_time = self.cache.get(user.id, 0)
            if now - last_time < self.limit:
                if isinstance(event, CallbackQuery):
                    try: await event.answer("⚠️ Espera un segundo antes de volver a pulsar.", show_alert=False)
                    except Exception: pass
                return
            self.cache[user.id] = now
            if len(self.cache) > 2000:
                cutoff = now - 5.0
                self.cache = {k: v for k, v in self.cache.items() if v > cutoff}
        return await handler(event, data)

# =====================================================================
# 3. SEGURIDAD DE WEBAPP & VALIDACIÓN TELEGRAM
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
# 4. ENDPOINTS API Y MINI APP (CON RADAR MANUAL Y ANTI-CACHÉ)
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
    waiting_vip = dp.get("waiting_vip", [])
    active_chats = dp["active_chats"]
    active_ids = set(active_viewers.keys()) | set(waiting_list) | set(waiting_vip) | set(active_chats.keys())
    
    online_users = []
    for uid in active_ids:
        if uid == user_id:
            continue
        u_data = await child_db.users.find_one({"_id": uid}) or {}
        
        if not u_data.get("radar_visible", False):
            continue

        status_txt = "Disponible"
        is_free = True
        if uid in active_chats:
            status_txt = "Ocupado"
            is_free = False
        elif uid in waiting_list or uid in waiting_vip:
            status_txt = "Buscando..."
            is_free = False
            
        online_users.append({
            "id": uid,
            "rep": u_data.get("reputation", 0),
            "status": status_txt,
            "is_free": is_free
        })
    
    last_bonus = user.get("last_bonus", 0)
    time_left_bonus = max(0, int((last_bonus + BONUS_COOLDOWN_SECONDS) - now))
    
    return web.json_response({
        "fotos": fotos,
        "videos": videos,
        "reputation": user.get("reputation", 0),
        "referrals": user.get("referrals", 0),
        "time_left": time_left_bonus,
        "radar_visible": user.get("radar_visible", False),
        "leaderboard": top_users,
        "online_users": online_users
    }, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    })

async def api_toggle_radar(request):
    user_id, child_db, _ = await authenticate_request(request)
    if not user_id or child_db is None:
        return web.json_response({"error": "No autorizado"}, status=401)
    
    user = await child_db.users.find_one({"_id": user_id}) or {}
    new_state = not user.get("radar_visible", False)
    await child_db.users.update_one({"_id": user_id}, {"$set": {"radar_visible": new_state}}, upsert=True)
    return web.json_response({"success": True, "radar_visible": new_state})

async def api_claim_bonus(request):
    user_id, child_db, _ = await authenticate_request(request)
    if not user_id or child_db is None:
        return web.json_response({"error": "No autorizado"}, status=401)
        
    now = time.time()
    pts = random.randint(1, 5)
    
    res = await child_db.users.find_one_and_update(
        {
            "_id": user_id, 
            "$or": [
                {"last_bonus": {"$lte": now - BONUS_COOLDOWN_SECONDS}},
                {"last_bonus": {"$exists": False}},
                {"last_bonus": 0}
            ]
        },
        {"$set": {"last_bonus": now}, "$inc": {"reputation": pts}},
        return_document=True
    )
    
    if not res:
        user = await child_db.users.find_one({"_id": user_id}) or {}
        time_left = max(0, int((user.get("last_bonus", 0) + BONUS_COOLDOWN_SECONDS) - now))
        return web.json_response({"success": False, "error": "Cooldown activo", "time_left": time_left})
        
    return web.json_response({
        "success": True, 
        "bonus": pts, 
        "new_rep": res.get("reputation", 0), 
        "time_left": BONUS_COOLDOWN_SECONDS
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
        .status-pill { font-size: 11px; padding: 5px 12px; border-radius: 20px; background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.25); color: var(--accent-blue); font-weight: 700; cursor: pointer; transition: 0.2s; }
        .status-pill:active { transform: scale(0.95); }
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
        .nav-dock { position: fixed; bottom: 16px; left: 12px; right: 12px; background: rgba(19, 25, 39, 0.95); backdrop-filter: blur(20px); border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 20px; padding: 6px; display: flex; justify-content: space-around; align-items: center; z-index: 99999; }
        .dock-btn { flex: 1; background: transparent; border: none; padding: 8px 4px; color: var(--hint-color); display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 11px; font-weight: 600; cursor: pointer; border-radius: 14px; }
        .dock-btn.active { background: var(--accent-grad); color: #ffffff; }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-title"><i class="fa-solid fa-arrows-split-up-and-left"></i> Exchange Hub</div>
        <div class="status-pill" id="live-indicator"><i class="fa-solid fa-circle fa-fade"></i> Online</div>
    </div>
    <div id="sec-radar" class="section-view active">
        <div class="card">
            <div class="card-header">
                <span class="card-title"><i class="fa-solid fa-radar"></i> Radar de Usuarios</span>
                <button class="status-pill" id="btn-toggle-radar" onclick="toggleRadarVisibility()">⚪ Invisible</button>
            </div>
            <p style="font-size:12px; color:var(--hint-color); margin-bottom:12px;">Activa tu radar para aparecer disponible y recibir solicitudes directas de trade.</p>
            <div id="radar-list"><p style="color:var(--hint-color); font-size:13px; text-align:center; padding:10px;">Cargando...</p></div>
        </div>
    </div>
    <div id="sec-bonus" class="section-view">
        <div class="card" style="text-align:center;">
            <span class="card-title">Cofre de Recompensa</span>
            <p style="font-size:12px; color:var(--hint-color); margin: 6px 0;">Reclama hasta +5 de reputación cada 6 horas.</p>
            <div class="chest-row">
                <div class="chest-card disabled" onclick="claimChest()"><i class="fa-solid fa-gem"></i></div>
                <div class="chest-card disabled" onclick="claimChest()"><i class="fa-solid fa-vault"></i></div>
                <div class="chest-card disabled" onclick="claimChest()"><i class="fa-solid fa-cube"></i></div>
            </div>
            <p id="bonus-countdown" style="font-size:13px; font-weight:700; color:var(--hint-color);">Cargando...</p>
        </div>
    </div>
    <div id="sec-profile" class="section-view">
        <div class="card">
            <div class="card-header"><span class="card-title">Métricas</span><span id="vip-ratio" style="font-weight:800; font-size:13px; color:var(--accent-blue);">--/20</span></div>
            <div class="progress-track"><div class="progress-fill" id="vip-fill"></div></div>
            <div class="stat-grid" style="margin-top:14px;">
                <div class="stat-box"><span style="font-size:11px; color:var(--hint-color);">Referidos</span><div class="stat-val" id="ref-count">0</div></div>
                <div class="stat-box"><span style="font-size:11px; color:var(--hint-color);">Reputación</span><div class="stat-val" id="rep-count">0</div></div>
            </div>
            <button class="btn-action btn-outline" style="margin-top:14px;" onclick="copyLink()"><i class="fa-solid fa-share-nodes"></i> Enlace de Invitación</button>
        </div>
        <div class="card">
            <span class="card-title">Caja Fuerte Multimedia</span>
            <div class="stat-grid" style="margin-top:10px;">
                <div class="stat-box"><i class="fa-regular fa-images"></i><div class="stat-val" id="cnt-photos">0</div></div>
                <div class="stat-box"><i class="fa-solid fa-film"></i><div class="stat-val" id="cnt-videos">0</div></div>
            </div>
            <button class="btn-action btn-danger" style="margin-top:14px;" onclick="wipeInventory()"><i class="fa-solid fa-trash"></i> Vaciar Inventario</button>
        </div>
    </div>
    <div id="sec-top" class="section-view">
        <div class="card">
            <div class="card-header"><span class="card-title"><i class="fa-solid fa-trophy"></i> Top 10 Red</span></div>
            <div id="leaderboard-list">Cargando...</div>
        </div>
    </div>
    <div class="nav-dock">
        <button class="dock-btn active" onclick="switchSection('sec-radar', this)"><i class="fa-solid fa-satellite-dish"></i><span>Radar</span></button>
        <button class="dock-btn" onclick="switchSection('sec-bonus', this)"><i class="fa-solid fa-gift"></i><span>Bonus</span></button>
        <button class="dock-btn" onclick="switchSection('sec-profile', this)"><i class="fa-solid fa-id-badge"></i><span>Perfil</span></button>
        <button class="dock-btn" onclick="switchSection('sec-top', this)"><i class="fa-solid fa-crown"></i><span>Top</span></button>
    </div>
    <script>
        const tg = window.Telegram?.WebApp;
        if (tg) { try { tg.expand(); tg.ready(); } catch(e) {} }
        const params = new URLSearchParams(window.location.search);
        const botUsername = params.get('bot') || "";
        const botId = params.get('bot_id') || "";
        const userId = tg?.initDataUnsafe?.user?.id || params.get('user_id') || "0";
        const headers = { "Content-Type": "application/json", "Authorization": tg?.initData || "" };

        function switchSection(id, btn) {
            document.querySelectorAll('.section-view').forEach(s => s.classList.remove('active'));
            document.querySelectorAll('.dock-btn').forEach(b => b.classList.remove('active'));
            document.getElementById(id)?.classList.add('active');
            btn?.classList.add('active');
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }
        let isBonusReady = false, timerInterval;
        function renderTimer(seconds) {
            clearInterval(timerInterval);
            const d = document.getElementById("bonus-countdown");
            const cards = document.querySelectorAll(".chest-card");
            if (seconds <= 0) {
                isBonusReady = true; d.innerText = "¡Cofre listo! Abre uno"; d.style.color = "#22c55e";
                cards.forEach(c => { c.classList.remove('disabled'); c.classList.add('ready'); });
                return;
            }
            isBonusReady = false; cards.forEach(c => { c.classList.add('disabled'); c.classList.remove('ready'); });
            d.style.color = "var(--hint-color)";
            let s = seconds;
            timerInterval = setInterval(() => {
                s--;
                if (s <= 0) renderTimer(0);
                else {
                    let h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
                    d.innerText = `Disponible en: ${h}h ${m}m ${sec}s`;
                }
            }, 1000);
        }

        function updateRadarBtn(isVisible) {
            const btn = document.getElementById("btn-toggle-radar");
            if (!btn) return;
            if (isVisible) {
                btn.innerText = "🟢 Visible para trade";
                btn.style.color = "#22c55e";
                btn.style.borderColor = "rgba(34, 197, 94, 0.4)";
                btn.style.background = "rgba(34, 197, 94, 0.1)";
            } else {
                btn.innerText = "⚪ Invisible";
                btn.style.color = "var(--hint-color)";
                btn.style.borderColor = "rgba(255, 255, 255, 0.15)";
                btn.style.background = "rgba(255, 255, 255, 0.05)";
            }
        }

        async function toggleRadarVisibility() {
            try {
                const res = await fetch(`/api/toggle_radar?bot_id=${botId}&id=${userId}`, { method: "POST", headers, body: "{}" });
                const d = await res.json();
                if (d.success) {
                    updateRadarBtn(d.radar_visible);
                    fetchData();
                }
            } catch(e) {}
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
                document.getElementById("vip-fill").style.width = Math.min(100, (d.reputation/20)*100) + "%";
                renderTimer(d.time_left);
                if (d.radar_visible !== undefined) {
                    updateRadarBtn(d.radar_visible);
                }

                const rList = document.getElementById("radar-list");
                rList.innerHTML = (!d.online_users || d.online_users.length === 0) 
                    ? '<p style="font-size:12px; color:var(--hint-color); text-align:center; padding:10px;">No hay otros usuarios visibles en el radar.</p>'
                    : d.online_users.map(u => `
                        <div class="user-row">
                            <div><div style="font-weight:700; font-size:13px;">ID: ${u.id}</div><span class="badge ${u.is_free ? 'badge-free' : 'badge-busy'}">${u.status}</span><span style="font-size:11px; margin-left:6px;">⭐ ${u.rep}</span></div>
                            ${u.is_free ? `<button class="btn-action btn-outline" style="width:auto; padding:6px 12px; font-size:12px;" onclick="connectUser('${u.id}')">Conectar</button>` : ''}
                        </div>`).join('');
                document.getElementById("leaderboard-list").innerHTML = d.leaderboard.map((u, i) => `
                    <div class="user-row"><span><strong>#${i+1}</strong> ID: ${u.id}</span><span style="font-weight:800; color:var(--accent-blue);">${u.rep} PTS</span></div>`).join('') || '<p style="color:var(--hint-color); font-size:12px;">Sin datos aún.</p>';
            } catch (e) {}
        }

        async function claimChest() {
            if (!isBonusReady) return;
            try {
                const res = await fetch(`/api/bonus?bot_id=${botId}&id=${userId}`, { method: "POST", headers, body: "{}" });
                const d = await res.json();
                if (d.success) {
                    confetti({ particleCount: 100, spread: 70, origin: { y: 0.6 } });
                    alert(`🎉 ¡Ganaste +${d.bonus} Puntos de Reputación!`);
                } else alert("⚠️ Cooldown activo.");
                fetchData();
            } catch(e) {}
        }
        function connectUser(tId) { window.location.href = `https://t.me/${botUsername}?start=connect_${tId}`; }
        function copyLink() {
            navigator.clipboard.writeText(`https://t.me/${botUsername}?start=${userId}`).then(() => alert("Enlace copiado."));
        }
        function wipeInventory() {
            if (confirm("¿Eliminar todos tus archivos de forma permanente?")) {
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
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

# =====================================================================
# 5. ENRUTADOR MODULAR DEL BOT HIJO (CHILD NODE)
# =====================================================================
def create_child_router(child_config: dict, child_db, ctx_vars: dict) -> Router:
    r = Router()
    r.message.middleware(ThrottlingMiddleware(limit=0.8))
    r.callback_query.middleware(ThrottlingMiddleware(limit=0.8))

    active_chats = ctx_vars["active_chats"]
    waiting_list = ctx_vars["waiting_list"]
    waiting_vip = ctx_vars.setdefault("waiting_vip", [])
    pending_trades = ctx_vars["pending_trades"]
    backup_queue = ctx_vars["backup_queue"]
    chat_threads = ctx_vars.setdefault("chat_threads", {})
    dp_storage = ctx_vars["dp"].storage

    upload_buffers = ctx_vars.setdefault("upload_buffers", {})
    upload_tasks = ctx_vars.setdefault("upload_tasks", {})

    FORCE_SUB_CHANNEL_ID = clean_chat_id(child_config.get("force_sub_id"))
    FORCE_SUB_CHANNEL_LINK = child_config.get("force_sub_link", "")
    VIP_GROUP_ID = clean_chat_id(child_config.get("vip_group_id"))
    LOG_GROUP_ID = clean_chat_id(child_config.get("log_group_id"))
    PAID_VIP_CHANNEL_ID = clean_chat_id(child_config.get("paid_vip_channel_id"))
    OWNER_ID = int(child_config.get("owner_id", 0)) if child_config.get("owner_id") else 0

    async def persist_rooms():
        try:
            await child_db.session_state.update_one(
                {"_id": "rooms"},
                {"$set": {
                    "active_chats": {str(k): v for k, v in active_chats.items()},
                    "waiting_list": waiting_list,
                    "waiting_vip": waiting_vip
                }},
                upsert=True
            )
        except Exception as e:
            logging.error(f"Error persistiendo salas de chat: {e}")

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
        await FSMContext(storage=dp_storage, key=key).set_state(state)

    async def get_user(user_id):
        user = await child_db.users.find_one({"_id": user_id})
        if not user:
            user = {
                "_id": user_id, "lang": "es", "referrals": 0, "reputation": 0,
                "mode": "anon", "in_vip": False, "notified_vip": False,
                "last_bonus": 0, "vip_until": 0, "paid_vip_active": False,
                "blacklisted": False, "radar_visible": False
            }
            await child_db.users.insert_one(user)

        now = time.time()
        central_vip = await master_db.vip_subscriptions.find_one({"_id": user_id, "vip_until": {"$gt": now}})
        if central_vip:
            user["paid_vip_active"] = True
            user["vip_until"] = max(user.get("vip_until", 0), central_vip.get("vip_until", 0))
            await child_db.users.update_one({"_id": user_id}, {"$set": {"paid_vip_active": True, "vip_until": user["vip_until"]}})

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

    async def check_vip_status(user_id: int, bot: Bot):
        if not VIP_GROUP_ID:
            return
        try:
            user = await get_user(user_id)
            if not user: return
            now = time.time()
            central_vip = await master_db.vip_subscriptions.find_one({"_id": user_id, "vip_until": {"$gt": now}})
            is_paid_vip = user.get("paid_vip_active", False) or user.get("vip_until", 0) > now or (central_vip is not None)
            has_requirements = (user.get("referrals", 0) >= VIP_MIN_REFERRALS or user.get("reputation", 0) >= VIP_MIN_REPUTATION or is_paid_vip)

            if has_requirements and not user.get("notified_vip"):
                invite_link, _ = await create_invite_link_smart(bot, VIP_GROUP_ID)
                if invite_link:
                    lang = user.get("lang", "es")
                    btn = "🌟 Entrar al Grupo VIP" if lang == "es" else "🌟 Join VIP Group"
                    msg = (
                        "🎉 <b>¡Acceso al Grupo VIP desbloqueado!</b> Enlace exclusivo:" 
                        if lang == "es" else 
                        "🎉 <b>VIP Access Granted!</b> Exclusive link:"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn, url=invite_link)]])
                    await bot.send_message(user_id, msg, reply_markup=kb, parse_mode="HTML")
                    await save_user(user_id, {"notified_vip": True, "in_vip": True})
        except Exception as e:
            logging.error(f"Error check VIP: {e}")

    async def send_rating_request(user_id, target_id, bot: Bot):
        user = await get_user(user_id)
        lang = user.get("lang", "es")
        btn_g = "👍 Buen usuario" if lang == "es" else "👍 Good user"
        btn_b = "👎 Malo" if lang == "es" else "👎 Bad"
        msg = "¿Deseas otorgarle un punto de reputación a tu compañero?" if lang == "es" else "Do you want to rate your partner?"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=btn_g, callback_data=f"rate_good_{target_id}"),
            InlineKeyboardButton(text=btn_b, callback_data=f"rate_bad_{target_id}")
        ]])
        await bot.send_message(user_id, msg, reply_markup=kb)

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
            [InlineKeyboardButton(text=btn_rnd, callback_data="find_chat_menu"), InlineKeyboardButton(text=btn_id, callback_data="connect_id")],
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
            "<b>Do not delete uploaded media</b>; if deleted, your trades will fail.\n\n"
            "💡 <i>Tap the <b>User Guide</b> button below to learn more.</i>"
        )
        await bot.send_message(chat_id=user_id, text=txt, reply_markup=kb, parse_mode="HTML")

    async def render_manual_text(lang: str) -> str:
        if lang == "es":
            return (
                "📖 <b>MANUAL DE USO — GUÍA COMPLETA</b>\n\n"
                "1️⃣ <b>Cargar tu Cofre:</b>\n"
                "• Envía fotos o videos a este chat privado con el bot.\n"
                "• Quedarán guardados automáticamente en tu inventario.\n"
                "• ⚠️ <b>ADVERTENCIA:</b> Nunca borres los mensajes originales que subas. Si los eliminas, la entrega fallará.\n\n"
                "2️⃣ <b>Búsqueda de Chat:</b>\n"
                "• Presiona <b>«Buscar Chat»</b> para entrar a la sala de espera.\n"
                "• Para que se emparejen, otro usuario debe buscar o conectarse con tu ID.\n\n"
                "3️⃣ <b>Intercambios Seguros:</b>\n"
                "• En un chat activo, presiona <b>«🤝 Proponer Intercambio»</b>.\n"
                "• Elige la categoría (fotos, videos o mixto) y la cantidad.\n"
                "• El bot transferirá los archivos de forma 100% automatizada y anónima.\n\n"
                "4️⃣ <b>Acceso VIP:</b>\n"
                "• Desbloqueas el <b>Grupo VIP Gratuito</b> acumulando <b>20 Puntos</b>, <b>3 Referidos</b> o adquiriendo el pase Stars."
            )
        else:
            return (
                "📖 <b>USER GUIDE — COMPLETE TUTORIAL</b>\n\n"
                "1️⃣ <b>Loading your Vault:</b>\n"
                "• Send photos or videos to this private chat.\n"
                "• Never delete uploaded messages or trades will fail.\n\n"
                "2️⃣ <b>Finding a Chat:</b>\n"
                "• Tap <b>«Random Chat»</b> to enter queue or connect directly by ID.\n\n"
                "3️⃣ <b>Trading:</b>\n"
                "• Use <b>«🤝 Propose Trade»</b>, select category and amount.\n\n"
                "4️⃣ <b>VIP Access:</b>\n"
                "• Reach <b>20 Reputation</b>, <b>3 Referrals</b> or buy Stars VIP to unlock VIP Groups."
            )

    @r.callback_query(F.data == "show_manual")
    async def cb_manual(callback: CallbackQuery):
        user = await get_user(callback.from_user.id)
        lang = user.get("lang", "es")
        txt = await render_manual_text(lang)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Volver al Menú" if lang == "es" else "⬅️ Back to Menu", callback_data="back_main")]])
        await callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

    @r.message(Command("manual"))
    async def cmd_manual(message: Message):
        user = await get_user(message.from_user.id)
        lang = user.get("lang", "es")
        txt = await render_manual_text(lang)
        await message.answer(txt, parse_mode="HTML")

    @r.message(Command("recuperar_vip"))
    async def cmd_recuperar_vip(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS and message.from_user.id != OWNER_ID: return
        if not VIP_GROUP_ID: return await message.answer("❌ No hay un Grupo VIP configurado.")
        status_msg = await message.answer("🔄 Buscando usuarios calificados...")
        now = time.time()
        query = {"$or": [{"referrals": {"$gte": VIP_MIN_REFERRALS}}, {"reputation": {"$gte": VIP_MIN_REPUTATION}}, {"paid_vip_active": True}, {"vip_until": {"$gt": now}}]}
        users = await child_db.users.find(query).to_list(length=1000)
        if not users: return await status_msg.edit_text("ℹ️ No hay ningún usuario calificado.")

        sent, blocked, failed = 0, 0, 0
        for u in users:
            uid = u["_id"]
            try:
                invite_link, _ = await create_invite_link_smart(bot, VIP_GROUP_ID)
                if invite_link:
                    txt = "🎉 <b>¡Tu acceso al Grupo VIP está listo!</b>\n\nAquí tienes tu enlace exclusivo:"
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌟 Entrar al Grupo VIP", url=invite_link)]])
                    await bot.send_message(chat_id=uid, text=txt, reply_markup=kb, parse_mode="HTML")
                    await child_db.users.update_one({"_id": uid}, {"$set": {"notified_vip": True, "in_vip": True}})
                    sent += 1
                else:
                    failed += 1
                await asyncio.sleep(0.3)
            except TelegramForbiddenError: blocked += 1
            except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after)
            except Exception: failed += 1

        await message.answer(f"✅ <b>Proceso completado</b>\n\n👥 Total: <code>{len(users)}</code>\n📩 Enviados: <code>{sent}</code>\n🚫 Bloqueados: <code>{blocked}</code>\n⚠️ Errores: <code>{failed}</code>", parse_mode="HTML")

    @r.message(Command("reinvitar"))
    async def cmd_reinvitar(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS and message.from_user.id != OWNER_ID: return
        args = message.text.split()
        if len(args) < 2 or not args[1].isdigit(): return await message.answer("⚠️ Uso: <code>/reinvitar ID</code>", parse_mode="HTML")
        if not VIP_GROUP_ID: return await message.answer("❌ No hay Grupo VIP configurado.")
        target_uid = int(args[1])
        try:
            invite_link, err = await create_invite_link_smart(bot, VIP_GROUP_ID)
            if invite_link:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌟 Entrar al Grupo VIP", url=invite_link)]])
                await bot.send_message(target_uid, "🎉 <b>Aquí tienes tu enlace exclusivo al Grupo VIP:</b>", reply_markup=kb, parse_mode="HTML")
                await child_db.users.update_one({"_id": target_uid}, {"$set": {"in_vip": True, "notified_vip": True}}, upsert=True)
                await message.answer(f"✅ Enlace VIP enviado al usuario <code>{target_uid}</code>.", parse_mode="HTML")
            else:
                await message.answer(f"❌ Error al generar la invitación: {err}")
        except Exception as e:
            await message.answer(f"❌ Error inesperado: {e}")

    @r.message(Command("enviar_vip"))
    async def cmd_enviar_vip(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS and message.from_user.id != OWNER_ID: return
        args = message.text.split()
        if len(args) < 2 or not args[1].isdigit(): return await message.answer("⚠️ Uso: <code>/enviar_vip ID [dias]</code>", parse_mode="HTML")
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 and args[2].isdigit() else VIP_DURATION_DAYS
        try:
            now = time.time()
            u_data = await child_db.users.find_one({"_id": target_uid}) or {}
            base_time = max(now, u_data.get("vip_until", 0))
            new_vip_until = base_time + (days * 86400)
            
            await child_db.users.update_one({"_id": target_uid}, {"$set": {"vip_until": new_vip_until, "paid_vip_active": True, "in_vip": True, "notified_vip": True}}, upsert=True)
            await master_db.vip_subscriptions.update_one(
                {"_id": target_uid}, 
                {"$set": {"vip_until": new_vip_until, "paid_vip_active": True, "tier": f"{days}d", "updated_at": datetime.now(timezone.utc)}}, 
                upsert=True
            )
            
            buttons = []
            errors_reported = []

            if PAID_VIP_CHANNEL_ID:
                link_p, err_p = await create_invite_link_smart(bot, PAID_VIP_CHANNEL_ID)
                if link_p:
                    buttons.append([InlineKeyboardButton(text=f"💎 Canal VIP Stars ({days} Días)", url=link_p)])
                else:
                    errors_reported.append(f"Canal VIP (ID: <code>{PAID_VIP_CHANNEL_ID}</code>): {err_p}")

            if VIP_GROUP_ID:
                link_g, err_g = await create_invite_link_smart(bot, VIP_GROUP_ID)
                if link_g:
                    buttons.append([InlineKeyboardButton(text="🌟 Grupo VIP de la Comunidad", url=link_g)])
                else:
                    errors_reported.append(f"Grupo VIP (ID: <code>{VIP_GROUP_ID}</code>): {err_g}")
            
            kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
            txt_user = f"🎉 <b>¡Tu membresía VIP de {days} días ha sido activada!</b>\nTienes acceso completo a ambos espacios:"
            await bot.send_message(chat_id=target_uid, text=txt_user, reply_markup=kb, parse_mode="HTML")
            
            admin_msg = f"✅ Membresía VIP por {days} días entregada a <code>{target_uid}</code>."
            if errors_reported:
                admin_msg += "\n\n⚠️ <b>Aviso Enlaces No Generados:</b>\n" + "\n".join([f"• {e}" for e in errors_reported])
            await message.answer(admin_msg, parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Error al enviar acceso: {e}")

    @r.message(Command("add_receiver"))
    async def cmd_add_receiver(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            new_id = int(message.text.split()[1])
            await child_db.settings.update_one({"_id": "config"}, {"$addToSet": {"extra_receivers": new_id}}, upsert=True)
            await message.answer(f"✅ Receptor <code>{new_id}</code> agregado.")
        except Exception:
            await message.answer("Uso: <code>/add_receiver ID</code>")

    @r.message(Command("del_receiver"))
    async def cmd_del_receiver(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            rem_id = int(message.text.split()[1])
            await child_db.settings.update_one({"_id": "config"}, {"$pull": {"extra_receivers": rem_id}}, upsert=True)
            await message.answer(f"✅ Receptor <code>{rem_id}</code> eliminado.")
        except Exception:
            await message.answer("Uso: <code>/del_receiver ID</code>")

    @r.message(Command("mantenimiento"))
    async def cmd_maintenance(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        cfg = await child_db.settings.find_one({"_id": "config"})
        new_state = not (cfg.get("maintenance", False) if cfg else False)
        await child_db.settings.update_one({"_id": "config"}, {"$set": {"maintenance": new_state}}, upsert=True)
        await message.answer(f"🛠️ Mantenimiento: <b>{'Activado 🔴' if new_state else 'Desactivado 🟢'}</b>")

    @r.message(Command("blacklist"))
    async def cmd_blacklist(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            target_id = int(message.text.split()[1])
            await save_user(target_id, {"blacklisted": True})
            await message.answer(f"🚫 Usuario <code>{target_id}</code> bloqueado.")
        except Exception:
            await message.answer("Uso: <code>/blacklist ID</code>")

    @r.message(Command("unblacklist"))
    async def cmd_unblacklist(message: Message):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        try:
            target_id = int(message.text.split()[1])
            await save_user(target_id, {"blacklisted": False})
            await message.answer(f"✅ Usuario <code>{target_id}</code> desbloqueado.")
        except Exception:
            await message.answer("Uso: <code>/unblacklist ID</code>")

    @r.message(Command("broadcast"))
    async def cmd_broadcast(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS: return
        text = message.text.replace("/broadcast", "").strip()
        if not text: return await message.answer("Escribe el mensaje tras el comando.")
        status_msg = await message.answer("⏳ Transmitiendo aviso...")
        count = 0
        async for u in child_db.users.find():
            try:
                await bot.send_message(u["_id"], f"📢 <b>Aviso General:</b>\n\n{text}", parse_mode="HTML")
                count += 1
                await asyncio.sleep(0.05)
            except Exception: pass
        await status_msg.delete()
        await message.answer(f"✅ Difusión completada a <code>{count}</code> usuarios.", parse_mode="HTML")

    @r.message(Command("estadisticas"))
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

    @r.message(CommandStart(), StateFilter("*"))
    async def cmd_start(message: Message, state: FSMContext, bot: Bot):
        user_id = message.from_user.id
        if await is_blacklisted(user_id): return
        if await is_maintenance_mode() and user_id not in SUPER_ADMIN_IDS:
            return await message.answer("🛠 <b>Bot en Mantenimiento.</b> Vuelve más tarde.")

        await state.clear()
        if user_id in waiting_list: waiting_list.remove(user_id)
        if user_id in waiting_vip: waiting_vip.remove(user_id)
        pending_trades.pop(user_id, None)
        
        t_id = active_chats.pop(user_id, None)
        if t_id:
            active_chats.pop(t_id, None)
            pending_trades.pop(t_id, None)
            await set_other_user_state(bot, t_id, BotStates.idle)
            try:
                await bot.send_message(t_id, "❌ <b>El otro usuario ha regresado al menú principal.</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
                await show_main_menu(t_id, bot)
            except Exception: pass
            chat_threads.pop(user_id, None)
            chat_threads.pop(t_id, None)
        await persist_rooms()

        user = await get_user(user_id)
        args = message.text.split(maxsplit=1)
        lang = user.get("lang", "es")

        if len(args) > 1 and args[1].startswith("connect_"):
            target_str = args[1].split("_")[1]
            if target_str.isdigit():
                t_id = int(target_str)
                if t_id != user_id and t_id not in active_chats and t_id not in waiting_list and t_id not in waiting_vip:
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

    @r.callback_query(F.data == "verify_sub")
    async def verify_sub(callback: CallbackQuery, bot: Bot):
        if await is_blacklisted(callback.from_user.id): return
        if await check_force_sub(callback.from_user.id, bot):
            await callback.message.delete()
            await show_main_menu(callback.from_user.id, bot)
        else:
            await callback.answer("⚠️ No se ha detectado tu suscripción.", show_alert=True)

    @r.callback_query(F.data == "change_lang")
    async def change_lang(callback: CallbackQuery, bot: Bot):
        u = await get_user(callback.from_user.id)
        new_lang = "en" if u.get("lang") == "es" else "es"
        await save_user(callback.from_user.id, {"lang": new_lang})
        await callback.answer("Idioma actualizado" if new_lang == "es" else "Language updated")
        await callback.message.delete()
        await show_main_menu(callback.from_user.id, bot)

    @r.callback_query(F.data == "my_profile")
    async def show_profile(callback: CallbackQuery, bot: Bot):
        u_id = callback.from_user.id
        await check_vip_status(u_id, bot)
        user = await get_user(u_id)
        fotos = await child_db.inventory.count_documents({"user_id": u_id, "type": "photo"})
        videos = await child_db.inventory.count_documents({"user_id": u_id, "type": "video"})
        rep = user.get("reputation", 0)
        prog_bar = format_progress_bar(rep, VIP_MIN_REPUTATION)
        
        now = time.time()
        vip_expires = user.get("vip_until", 0)
        is_paid_vip = user.get("paid_vip_active", False) or vip_expires > now
        vip_txt = f"Hasta {datetime.fromtimestamp(vip_expires).strftime('%d/%m %H:%M')}" if is_paid_vip else "Inactivo ❌"
        modo_txt = "🕵️‍♂️ Anónimo" if user.get("mode") == "anon" else "👤 Público"

        kb_list = [
            [InlineKeyboardButton(text="⭐ Membresías VIP (Stars)", callback_data="menu_buy_vip")],
            [InlineKeyboardButton(text="🔄 Cambiar Modo", callback_data="toggle_mode")]
        ]
        
        if VIP_GROUP_ID and (user.get("in_vip") or user.get("referrals", 0) >= VIP_MIN_REFERRALS or rep >= VIP_MIN_REPUTATION or is_paid_vip):
            link_g, _ = await create_invite_link_smart(bot, VIP_GROUP_ID)
            if link_g:
                kb_list.insert(0, [InlineKeyboardButton(text="🌟 Grupo VIP Gratuito", url=link_g)])

        if PAID_VIP_CHANNEL_ID and is_paid_vip:
            link_p, _ = await create_invite_link_smart(bot, PAID_VIP_CHANNEL_ID)
            if link_p:
                kb_list.insert(0, [InlineKeyboardButton(text="💎 Canal VIP de Pago", url=link_p)])

        kb_list.append([InlineKeyboardButton(text="⬅️ Volver", callback_data="back_main")])

        txt = (
            f"👤 <b>Tu Perfil</b>\n\n"
            f"🆔 ID: <code>{u_id}</code>\n"
            f"🌟 Reputación: <code>{rep}/{VIP_MIN_REPUTATION}</code>\n"
            f"<code>[{prog_bar}]</code>\n"
            f"👥 Referidos: <code>{user.get('referrals', 0)}/{VIP_MIN_REFERRALS}</code>\n"
            f"⭐ VIP Stars: <b>{vip_txt}</b>\n"
            f"🎭 Modo: <b>{modo_txt}</b>\n\n"
            f"📦 Caja Fuerte: 📷 {fotos} | 🎥 {videos}"
        )
        await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list), parse_mode="HTML")

    @r.callback_query(F.data == "menu_buy_vip")
    async def menu_buy_vip(callback: CallbackQuery, bot: Bot):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ 1 Día (5 Stars)", url=f"https://t.me/{MASTER_BOT_USERNAME}?start=paystars_{bot.id}_1d")],
            [InlineKeyboardButton(text="⭐ 7 Días (25 Stars)", url=f"https://t.me/{MASTER_BOT_USERNAME}?start=paystars_{bot.id}_7d")],
            [InlineKeyboardButton(text="⭐ 30 Días (80 Stars)", url=f"https://t.me/{MASTER_BOT_USERNAME}?start=paystars_{bot.id}_30d")],
            [InlineKeyboardButton(text="⬅️ Volver", callback_data="my_profile")]
        ])
        txt = (
            "💎 <b>MEMBRESÍAS VIP CON TELEGRAM STARS</b>\n\n"
            "Elige la duración que prefieras. El pago se procesa de forma centralizada e instantánea:\n\n"
            "• <b>24 Horas:</b> 5 Stars (Ideal para intercambios rápidos)\n"
            "• <b>7 Días:</b> 25 Stars (Acceso estándar semanal)\n"
            "• <b>30 Días:</b> 80 Stars (Pase mensual completo con descuento)"
        )
        await callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

    @r.callback_query(F.data == "toggle_mode")
    async def toggle_mode(callback: CallbackQuery, bot: Bot):
        u = await get_user(callback.from_user.id)
        new_mode = "public" if u.get("mode") == "anon" else "anon"
        await save_user(callback.from_user.id, {"mode": new_mode})
        await show_profile(callback, bot)

    @r.callback_query(F.data == "back_main")
    async def back_main(callback: CallbackQuery, state: FSMContext, bot: Bot):
        await state.set_state(BotStates.idle)
        await callback.message.delete()
        await show_main_menu(callback.from_user.id, bot)

    @r.callback_query(F.data == "connect_id")
    async def ask_for_id(callback: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Cancelar", callback_data="back_main")]])
        await callback.message.edit_text("✏️ Escribe el <b>ID numérico</b> del usuario:", reply_markup=kb, parse_mode="HTML")

    @r.message(StateFilter(BotStates.waiting_for_id), ~F.text.startswith("/"))
    async def process_connect_id(message: Message, state: FSMContext, bot: Bot):
        u_id = message.from_user.id
        if not message.text.isdigit():
            return await message.answer("⚠️ Debe ser un ID numérico.")
        t_id = int(message.text)
        if t_id == u_id: return
        if t_id in active_chats or t_id in waiting_list or t_id in waiting_vip:
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

    @r.callback_query(F.data.startswith("accept_id_"))
    async def accept_id_conn(callback: CallbackQuery, state: FSMContext, bot: Bot):
        t_id = int(callback.data.split("_")[2])
        u_id = callback.from_user.id
        if t_id in active_chats or u_id in active_chats:
            return await callback.answer("Uno de los usuarios ya está ocupado.", show_alert=True)
            
        active_chats[u_id], active_chats[t_id] = t_id, u_id
        await state.set_state(BotStates.chatting)
        await set_other_user_state(bot, t_id, BotStates.chatting)
        await persist_rooms()
        
        for uid in (u_id, t_id):
            u_obj = await get_user(uid)
            lng = u_obj.get("lang", "es")
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"),
                           KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]],
                resize_keyboard=True
            )
            await bot.send_message(uid, "✅ <b>¡Conexión establecida!</b> Ya pueden hablar o intercambiar.", reply_markup=kb, parse_mode="HTML")
        await callback.message.delete()

    @r.callback_query(F.data.startswith("reject_id_"))
    async def reject_id_conn(callback: CallbackQuery, bot: Bot):
        req_id = int(callback.data.split("_")[2])
        try:
            await bot.send_message(req_id, "❌ <b>Tu solicitud de chat fue rechazada.</b>", parse_mode="HTML")
        except Exception: pass
        await callback.message.delete()

    @r.callback_query(F.data.in_(["find_chat", "find_chat_menu"]))
    async def find_chat_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
        u_id = callback.from_user.id
        if await is_blacklisted(u_id): return
        if u_id in active_chats or u_id in waiting_list or u_id in waiting_vip:
            return await callback.answer("Ya estás en una sesión o en espera.", show_alert=True)

        user = await get_user(u_id)
        is_vip = user.get("paid_vip_active", False) or user.get("vip_until", 0) > time.time() or user.get("in_vip", False)

        if is_vip:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌟 Solo con VIPs", callback_data="start_search_vip")],
                [InlineKeyboardButton(text="🌐 Búsqueda General", callback_data="start_search_gen")],
                [InlineKeyboardButton(text="⬅️ Cancelar", callback_data="back_main")]
            ])
            await callback.message.edit_text("🔍 <b>Filtro de Búsqueda VIP</b>\n\n¿Con quién te gustaría emparejarte?", reply_markup=kb, parse_mode="HTML")
        else:
            await start_search(callback, state, bot, only_vip=False)

    @r.callback_query(F.data.in_(["start_search_vip", "start_search_gen"]))
    async def handle_search_choice(callback: CallbackQuery, state: FSMContext, bot: Bot):
        only_vip = (callback.data == "start_search_vip")
        await start_search(callback, state, bot, only_vip=only_vip)

    async def start_search(callback: CallbackQuery, state: FSMContext, bot: Bot, only_vip: bool):
        u_id = callback.from_user.id
        user = await get_user(u_id)
        lang = user.get("lang", "es")

        target_pool = waiting_vip if only_vip else waiting_list
        alt_pool = waiting_vip if not only_vip and waiting_vip else None

        matched_id = None
        if target_pool:
            matched_id = target_pool.pop(0)
        elif alt_pool and not only_vip:
            matched_id = alt_pool.pop(0)

        if matched_id and matched_id != u_id:
            active_chats[u_id], active_chats[matched_id] = matched_id, u_id
            await state.set_state(BotStates.chatting)
            await set_other_user_state(bot, matched_id, BotStates.chatting)
            await persist_rooms()

            for uid in (u_id, matched_id):
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
            if only_vip:
                waiting_vip.append(u_id)
            else:
                waiting_list.append(u_id)
            await persist_rooms()
            await state.set_state(BotStates.searching)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancelar Búsqueda" if lang == "es" else "❌ Cancel Queue", callback_data="leave_chat")]])
            txt = (
                "🔍 <b>Buscando compañero de intercambio...</b>\n\n"
                "⏳ <i>Estás en la sala de espera. En cuanto otro usuario busque chat se conectarán al instante.</i>"
                if lang == "es" else
                "🔍 <b>Searching for trade partner...</b>\n\n"
                "⏳ <i>You will be paired automatically once someone joins.</i>"
            )
            await callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

    @r.message(F.text.in_(["❌ Desconectar", "❌ Disconnect"]))
    @r.callback_query(F.data == "leave_chat")
    async def leave_chat(event, state: FSMContext, bot: Bot):
        u_id = event.from_user.id
        if u_id in waiting_list: waiting_list.remove(u_id)
        if u_id in waiting_vip: waiting_vip.remove(u_id)
        pending_trades.pop(u_id, None)
        t_id = active_chats.pop(u_id, None)
        
        if t_id:
            active_chats.pop(t_id, None)
            pending_trades.pop(t_id, None)
            await set_other_user_state(bot, t_id, BotStates.idle)
            try:
                await bot.send_message(t_id, "❌ <b>Tu compañero abandonó la sesión.</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
                await show_main_menu(t_id, bot)
            except Exception: pass
            
        chat_threads.pop(u_id, None)
        if t_id: chat_threads.pop(t_id, None)
        await persist_rooms()
        await state.set_state(BotStates.idle)
        
        if isinstance(event, Message):
            await event.answer("Has salido de la sesión.", reply_markup=ReplyKeyboardRemove())
        else:
            await event.message.delete()
            await bot.send_message(u_id, "Has salido de la sesión.", reply_markup=ReplyKeyboardRemove())
        await show_main_menu(u_id, bot)

    # -------------------------------------------------------------
    # COLA ASÍNCRONA POR LOTES (DEBOUNCE BUFFER) PARA SUBIDA MASIVA
    # -------------------------------------------------------------
    async def process_user_upload_batch(uid: int, bot: Bot):
        """Espera a que el usuario termine de enviar la ráfaga (2.0s) y guarda en bloque."""
        await asyncio.sleep(2.0)
        items = upload_buffers.pop(uid, [])
        upload_tasks.pop(uid, None)

        if not items:
            return

        unique_hashes = list({it["file_unique_id"] for it in items})

        existing_docs = await child_db.inventory.find(
            {"user_id": uid, "file_unique_id": {"$in": unique_hashes}},
            {"file_unique_id": 1}
        ).to_list(length=None)
        existing_hashes = {d["file_unique_id"] for d in existing_docs}

        to_insert = []
        seen_in_batch = set()

        for it in items:
            h = it["file_unique_id"]
            if h not in existing_hashes and h not in seen_in_batch:
                seen_in_batch.add(h)
                to_insert.append({
                    "user_id": uid,
                    "file_id": it["file_id"],
                    "message_id": it["message_id"],
                    "file_unique_id": h,
                    "type": it["type"]
                })

        if to_insert:
            await child_db.inventory.insert_many(to_insert)

        total_inventory = await child_db.inventory.count_documents({"user_id": uid})
        total_received = len(items)
        guardados = len(to_insert)
        duplicados = total_received - guardados

        try:
            if guardados > 0:
                txt = (
                    f"📥 <b>¡Lote procesado con éxito!</b>\n\n"
                    f"• <b>Archivos nuevos guardados:</b> +{guardados}\n"
                    f"• <b>Archivos descartados (duplicados):</b> {duplicados}\n"
                    f"• <b>Total en tu cofre:</b> <code>{total_inventory}</code>\n\n"
                    f"⚠️ <i>Recuerda no borrar los mensajes originales en este chat para no romper tus futuros trades.</i>"
                )
            else:
                txt = (
                    f"⚠️ <b>Lote revisado:</b> Se recibieron <code>{total_received}</code> archivos, "
                    f"pero todos ya estaban registrados en tu cofre (duplicados)."
                )
            await bot.send_message(uid, txt, parse_mode="HTML")
        except Exception:
            pass

    # -------------------------------------------------------------
    # RECEPCIÓN REFORZADA DE ARCHIVOS Y ENCOLADO ATÓMICO
    # -------------------------------------------------------------
    @r.message(F.chat.type == "private", F.photo | F.video | F.document)
    async def handle_media(message: Message, bot: Bot):
        u_id = message.from_user.id
        if await is_blacklisted(u_id): return
        
        media = message.photo[-1] if message.photo else (message.video or message.document)
        file_id = media.file_id
        file_unique_id = media.file_unique_id
        m_type = "photo" if message.photo else ("video" if message.video else "document")

        # 1. Encolado garantizado para administradores con atomicidad absoluta
        try:
            is_new = await child_db.global_files.find_one_and_update(
                {"_id": file_unique_id},
                {"$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
                upsert=True
            )
            # is_new es None únicamente cuando el documento acaba de crearse por primera vez
            if is_new is None:
                await backup_queue.put({
                    "file_id": file_id,
                    "type": m_type,
                    "user_id": u_id,
                    "name": message.from_user.full_name or "Usuario"
                })
        except Exception as e:
            logging.error(f"Error al registrar y encolar respaldo: {e}")

        # 2. Si están en chat 1 a 1 en vivo, transferir directo al compañero
        if u_id in active_chats:
            target = active_chats[u_id]
            try:
                await bot.copy_message(chat_id=target, from_chat_id=u_id, message_id=message.message_id)
                thread_id = await get_or_create_chat_topic(bot, u_id, target)
                if thread_id and LOG_GROUP_ID:
                    await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=f"📎 <code>{u_id}</code> envió un archivo ({m_type}).", parse_mode="HTML")
            except Exception: pass
            return

        # 3. Buffer de subida para el cofre privado del usuario
        upload_buffers.setdefault(u_id, []).append({
            "file_id": file_id,
            "message_id": message.message_id,
            "file_unique_id": file_unique_id,
            "type": m_type
        })

        if u_id in upload_tasks:
            upload_tasks[u_id].cancel()

        upload_tasks[u_id] = asyncio.create_task(process_user_upload_batch(u_id, bot))

    @r.message(StateFilter(BotStates.chatting), F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade"]))
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
                f"• <b>Archivos únicos disponibles:</b> <code>0</code>\n\n"
                f"📥 <i>Sube más videos o fotos al bot para poder proponer un intercambio.</i>"
            )
            return await message.answer(msg_no, parse_mode="HTML")

        await state.set_state(BotStates.waiting_trade_type)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Fotos" if lng == "es" else "📷 Photos", callback_data="settype_photo"),
             InlineKeyboardButton(text="🎥 Videos", callback_data="settype_video")],
            [InlineKeyboardButton(text="🔀 Mixto" if lng == "es" else "🔀 Mixed", callback_data="settype_mixed")]
        ])
        
        msg = (
            f"📊 <b>Estado de tu Inventario:</b>\n"
            f"• Archivos totales en tu cofre: <code>{tot}</code>\n"
            f"• <b>Archivos únicos disponibles:</b> <code>{unq}</code>\n\n"
            f"🎬 <b>¿Qué categoría deseas intercambiar?</b>"
        )
        await message.answer(msg, reply_markup=kb, parse_mode="HTML")

    @r.callback_query(StateFilter(BotStates.waiting_trade_type), F.data.startswith("settype_"))
    async def process_trade_type(callback: CallbackQuery, state: FSMContext):
        u_id = callback.from_user.id
        t_id = active_chats.get(u_id)
        if not t_id:
            return await callback.answer("Chat desconectado.", show_alert=True)

        t_type = callback.data.split("_")[1]
        await state.update_data(trade_type=t_type)
        tot_cat, unq_cat = await get_inventory_stats_for_trade(u_id, t_id, t_type)
        await state.update_data(max_unique=unq_cat)
        
        if unq_cat == 0:
            return await callback.message.edit_text(f"⚠️ No tienes archivos únicos de categoría <b>{t_type}</b> disponibles.", parse_mode="HTML")

        await state.set_state(BotStates.waiting_trade_amount)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="10x10", callback_data="trade_10"),
            InlineKeyboardButton(text="50x50", callback_data="trade_50"),
            InlineKeyboardButton(text="100x100", callback_data="trade_100")
        ]])
        
        msg = (
            f"📁 Categoría: <b>{t_type.capitalize()}</b>\n"
            f"✨ Tienes <b>{unq_cat}</b> archivos únicos disponibles (de {tot_cat} totales).\n\n"
            f"🔢 <b>¿Cuántos archivos deseas intercambiar?</b> Elige una opción o escribe un número:"
        )
        await callback.message.edit_text(msg, reply_markup=kb, parse_mode="HTML")

    async def execute_trade_proposal(u_id, amt, t_type, send_func, state, bot: Bot):
        t_id = active_chats.get(u_id)
        if not t_id: return await state.set_state(BotStates.idle)
        
        user, t_user = await get_user(u_id), await get_user(t_id)
        lang, t_lang = user.get("lang", "es"), t_user.get("lang", "es")
        
        _, unq_available = await get_inventory_stats_for_trade(u_id, t_id, t_type)
        if amt > unq_available:
            err_amt = f"⚠️ <b>Cantidad no disponible:</b> Solicitaste <b>{amt}</b> pero solo tienes <b>{unq_available}</b> disponibles."
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

    @r.message(StateFilter(BotStates.waiting_trade_amount), F.text.regexp(r'^\d+$'))
    async def process_manual_trade_offer(message: Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await execute_trade_proposal(message.from_user.id, int(message.text), data.get("trade_type", "mixed"), message.answer, state, bot)

    @r.callback_query(StateFilter(BotStates.waiting_trade_amount), F.data.startswith("trade_"))
    async def process_button_trade_offer(callback: CallbackQuery, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await callback.message.delete()
        await execute_trade_proposal(callback.from_user.id, int(callback.data.split("_")[1]), data.get("trade_type", "mixed"), callback.message.answer, state, bot)

    async def run_fast_trade_worker(bot: Bot, child_db, sid: int, uid: int, files_s: list, files_r: list, amt: int, t_type: str):
        async def copy_single(sender_id: int, receiver_id: int, file_doc: dict):
            try:
                await bot.copy_message(chat_id=receiver_id, from_chat_id=sender_id, message_id=file_doc["message_id"])
                await child_db.exchange_history.insert_one({
                    "sender_id": sender_id, "receiver_id": receiver_id,
                    "file_unique_id": file_doc["file_unique_id"], "created_at": datetime.now(timezone.utc)
                })
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await bot.copy_message(chat_id=receiver_id, from_chat_id=sender_id, message_id=file_doc["message_id"])
                    await child_db.exchange_history.insert_one({
                        "sender_id": sender_id, "receiver_id": receiver_id,
                        "file_unique_id": file_doc["file_unique_id"], "created_at": datetime.now(timezone.utc)
                    })
                    return True
                except Exception:
                    await child_db.inventory.delete_one({"_id": file_doc["_id"]})
                    return False
            except Exception:
                await child_db.inventory.delete_one({"_id": file_doc["_id"]})
                return False

        async def copy_chunk(sender_id: int, receiver_id: int, chunk: list):
            chunk_ids = [f["message_id"] for f in chunk]
            try:
                await bot.copy_messages(chat_id=receiver_id, from_chat_id=sender_id, message_ids=chunk_ids)
                now_dt = datetime.now(timezone.utc)
                docs = [
                    {"sender_id": sender_id, "receiver_id": receiver_id, "file_unique_id": f["file_unique_id"], "created_at": now_dt}
                    for f in chunk
                ]
                if docs:
                    await child_db.exchange_history.insert_many(docs)
                return len(chunk)
            except Exception:
                sent_count = 0
                for f in chunk:
                    ok = await copy_single(sender_id, receiver_id, f)
                    if ok: sent_count += 1
                    await asyncio.sleep(0.4)
                return sent_count

        total_sent_s = 0
        total_sent_r = 0
        chunk_size = 10
        s_chunks = [files_s[i:i + chunk_size] for i in range(0, min(len(files_s), amt), chunk_size)]
        r_chunks = [files_r[i:i + chunk_size] for i in range(0, min(len(files_r), amt), chunk_size)]
        rounds = max(len(s_chunks), len(r_chunks))
        aborted = False

        try:
            for idx in range(rounds):
                cs = s_chunks[idx] if idx < len(s_chunks) else []
                sent_s = 0
                if cs:
                    sent_s = await copy_chunk(sid, uid, cs)
                    total_sent_s += sent_s
                    await asyncio.sleep(0.6)

                if cs and sent_s == 0:
                    aborted = True
                    break

                cr = r_chunks[idx] if idx < len(r_chunks) else []
                sent_r = 0
                if cr:
                    sent_r = await copy_chunk(uid, sid, cr)
                    total_sent_r += sent_r
                    await asyncio.sleep(0.6)

                if cr and sent_r == 0:
                    aborted = True
                    break

            if aborted or (total_sent_s == 0 and total_sent_r == 0):
                fail_msg = (
                    "⚠️ <b>Intercambio cancelado:</b> Uno de los usuarios eliminó archivos de su cofre en Telegram. "
                    "Para proteger tu inventario, el intercambio fue suspendido."
                )
                try: await bot.send_message(uid, fail_msg, parse_mode="HTML")
                except Exception: pass
                try: await bot.send_message(sid, fail_msg, parse_mode="HTML")
                except Exception: pass
                return

            thread_id = chat_threads.get(uid) or chat_threads.get(sid)
            if thread_id and LOG_GROUP_ID:
                rep_log = f"🔄 <b>Intercambio Finalizado</b>\n• Remitente 1: <code>{sid}</code> (Enviados: {total_sent_s})\n• Remitente 2: <code>{uid}</code> (Enviados: {total_sent_r})\n• Tipo: {t_type}"
                try: await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=rep_log, parse_mode="HTML")
                except Exception: pass

            await child_db.users.update_one({"_id": uid}, {"$inc": {"reputation": 1}})
            await child_db.users.update_one({"_id": sid}, {"$inc": {"reputation": 1}})
            await check_vip_status(uid, bot)
            await check_vip_status(sid, bot)

            await bot.send_message(uid, f"🎉 <b>¡Trade completado!</b> Recibiste {total_sent_s} archivos. (+1 Reputación)", parse_mode="HTML")
            await bot.send_message(sid, f"🎉 <b>¡Trade completado!</b> Recibiste {total_sent_r} archivos. (+1 Reputación)", parse_mode="HTML")

            await send_rating_request(uid, sid, bot)
            await send_rating_request(sid, uid, bot)
        except Exception as e:
            logging.error(f"Error procesando trade worker: {e}")
            await notify_admins_alert(f"Error en trade worker ({sid} <-> {uid}): {e}")

    @r.callback_query(F.data == "accept_trade")
    async def accept_trade(callback: CallbackQuery, bot: Bot):
        u_id = callback.from_user.id
        trade = pending_trades.pop(u_id, None)
        if not trade: return await callback.answer("Propuesta expirada o ya procesada.", show_alert=True)
        s_id, amt, t_type = trade["sender"], trade["amount"], trade.get("type", "mixed")
        
        if active_chats.get(u_id) != s_id or active_chats.get(s_id) != u_id:
            return await callback.message.edit_text("❌ El intercambio se canceló porque la conexión entre ambos finalizó.")

        await callback.message.edit_text("🔍 <i>Comprobando inventarios disponibles...</i>", parse_mode="HTML")
        ok_s, files_s = await get_random_batch(s_id, u_id, t_type, amt)
        ok_r, files_r = await get_random_batch(u_id, s_id, t_type, amt)
        
        if not ok_s or not ok_r:
            err = "⚠️ Uno de los usuarios no tiene suficientes archivos únicos para este intercambio."
            await callback.message.edit_text(err)
            return await bot.send_message(s_id, err)

        await callback.message.edit_text(f"🚀 <i>Transfiriendo {amt}x{amt} de forma rápida y segura...</i>", parse_mode="HTML")
        await bot.send_message(s_id, f"🚀 <i>Transfiriendo {amt}x{amt} de forma rápida y segura...</i>", parse_mode="HTML")

        asyncio.create_task(run_fast_trade_worker(bot, child_db, s_id, u_id, files_s, files_r, amt, t_type))

    @r.callback_query(F.data == "reject_trade")
    async def reject_trade(callback: CallbackQuery, bot: Bot):
        trade = pending_trades.pop(callback.from_user.id, None)
        if trade:
            try: await bot.send_message(trade["sender"], "❌ La propuesta de trade fue rechazada.")
            except Exception: pass
        await callback.message.edit_text("❌ Oferta rechazada.")

    @r.callback_query(F.data.startswith("rate_"))
    async def process_rating(callback: CallbackQuery, bot: Bot):
        action, _, t_id_str = callback.data.split("_")
        t_id = int(t_id_str)
        if action == "good":
            await child_db.users.update_one({"_id": t_id}, {"$inc": {"reputation": 1}})
            await check_vip_status(t_id, bot)
        await callback.message.edit_text("✅ Valoración registrada.")

    @r.message(StateFilter(BotStates.chatting), ~F.text.startswith("/"), ~F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade", "❌ Desconectar", "❌ Disconnect"]))
    async def relay_msg(message: Message, bot: Bot):
        u_id = message.from_user.id
        if await is_blacklisted(u_id): return
        
        if LINK_REGEX.search(message.text or ""):
            return await message.answer("🚫 <b>Mensaje bloqueado:</b> Por seguridad de la red no se permite compartir enlaces o menciones.", parse_mode="HTML")

        target = active_chats.get(u_id)
        if target:
            try:
                await bot.send_message(target, f"💬 {html.quote(message.text)}", parse_mode="HTML")
                thread_id = await get_or_create_chat_topic(bot, u_id, target)
                if thread_id and LOG_GROUP_ID:
                    await bot.send_message(chat_id=LOG_GROUP_ID, message_thread_id=thread_id, text=f"💬 <code>{u_id}</code>: {html.quote(message.text)}", parse_mode="HTML")
            except Exception: pass

    @r.chat_join_request()
    async def process_vip_join(join_req: ChatJoinRequest, bot: Bot):
        if VIP_GROUP_ID and join_req.chat.id == VIP_GROUP_ID:
            user = await get_user(join_req.from_user.id)
            now = time.time()
            is_paid_vip = user.get("paid_vip_active", False) or user.get("vip_until", 0) > now
            if user.get("referrals", 0) >= VIP_MIN_REFERRALS or user.get("reputation", 0) >= VIP_MIN_REPUTATION or is_paid_vip:
                await join_req.approve()
                try: await bot.send_message(join_req.from_user.id, "🎉 ¡Tu solicitud al Grupo VIP ha sido aprobada!")
                except Exception: pass
            else:
                await join_req.decline()
                try: await bot.send_message(join_req.from_user.id, "❌ No cumples los requisitos mínimos para ingresar.")
                except Exception: pass

    return r

# =====================================================================
# 6. ENRUTADOR DEL BOT GESTOR VIP (DEDICADO Y DESACOPLADO)
# =====================================================================
def create_vip_manager_router(config: dict) -> Router:
    vr = Router()
    vr.message.middleware(ThrottlingMiddleware(limit=0.8))
    vr.callback_query.middleware(ThrottlingMiddleware(limit=0.8))

    PAID_VIP_CHANNEL_ID = clean_chat_id(config.get("paid_vip_channel_id"))
    VIP_GROUP_ID = clean_chat_id(config.get("vip_group_id"))
    OWNER_ID = int(config.get("owner_id", 0))

    @vr.message(CommandStart())
    async def vip_bot_start(message: Message, bot: Bot):
        uid = message.from_user.id
        now = time.time()
        sub = await master_db.vip_subscriptions.find_one({"_id": uid}) or {}
        vip_expires = sub.get("vip_until", 0)
        is_active = sub.get("paid_vip_active", False) and vip_expires > now
        
        status_txt = f"🟢 Activo hasta {datetime.fromtimestamp(vip_expires).strftime('%d/%m/%Y %H:%M')}" if is_active else "🔴 Inactivo"

        kb_list = [
            [InlineKeyboardButton(text="⭐ Adquirir Membresía Stars", callback_data="vip_bot_plans")]
        ]

        if is_active:
            if PAID_VIP_CHANNEL_ID:
                link_p, _ = await create_invite_link_smart(bot, PAID_VIP_CHANNEL_ID)
                if link_p:
                    kb_list.insert(0, [InlineKeyboardButton(text="💎 Canal VIP Stars", url=link_p)])
            if VIP_GROUP_ID:
                link_g, _ = await create_invite_link_smart(bot, VIP_GROUP_ID)
                if link_g:
                    kb_list.insert(0, [InlineKeyboardButton(text="🌟 Grupo VIP de la Comunidad", url=link_g)])

        txt = (
            f"👑 <b>Portal Central de Suscripciones VIP</b>\n\n"
            f"Tu estado actual: <b>{status_txt}</b>\n\n"
            f"<i>Las membresías adquiridas aquí son válidas en toda nuestra red de intercambio.</i>"
        )
        await message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list), parse_mode="HTML")

    @vr.callback_query(F.data == "vip_bot_plans")
    async def vip_bot_plans(callback: CallbackQuery, bot: Bot):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ 1 Día (5 Stars)", url=f"https://t.me/{MASTER_BOT_USERNAME}?start=paystars_{bot.id}_1d")],
            [InlineKeyboardButton(text="⭐ 7 Días (25 Stars)", url=f"https://t.me/{MASTER_BOT_USERNAME}?start=paystars_{bot.id}_7d")],
            [InlineKeyboardButton(text="⭐ 30 Días (80 Stars)", url=f"https://t.me/{MASTER_BOT_USERNAME}?start=paystars_{bot.id}_30d")],
            [InlineKeyboardButton(text="⬅️ Volver", callback_data="vip_bot_back")]
        ])
        txt = (
            "💎 <b>SELECCIONA TU PLAN VIP</b>\n\n"
            "Elige la duración de tu acceso a través de Telegram Stars:\n\n"
            "• <b>24 Horas:</b> 5 Stars\n• <b>7 Días:</b> 25 Stars\n• <b>30 Días:</b> 80 Stars\n\n"
            "<i>El pago se procesará a través de nuestro Bot Central de Cobros.</i>"
        )
        await callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

    @vr.callback_query(F.data == "vip_bot_back")
    async def vip_bot_back(callback: CallbackQuery, bot: Bot):
        await callback.message.delete()
        await vip_bot_start(callback.message, bot)

    @vr.message(Command("enviar_vip"))
    async def vip_cmd_enviar(message: Message, bot: Bot):
        if message.from_user.id not in SUPER_ADMIN_IDS and message.from_user.id != OWNER_ID: return
        args = message.text.split()
        if len(args) < 2 or not args[1].isdigit(): return await message.answer("Uso: <code>/enviar_vip ID [dias]</code>", parse_mode="HTML")
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 and args[2].isdigit() else 7
        now = time.time()
        sub = await master_db.vip_subscriptions.find_one({"_id": target_uid}) or {}
        new_vip = max(now, sub.get("vip_until", 0)) + (days * 86400)
        await master_db.vip_subscriptions.update_one(
            {"_id": target_uid},
            {"$set": {"vip_until": new_vip, "paid_vip_active": True, "tier": f"{days}d", "updated_at": datetime.now(timezone.utc)}},
            upsert=True
        )
        buttons = []
        errors_reported = []

        if PAID_VIP_CHANNEL_ID:
            link_p, err_p = await create_invite_link_smart(bot, PAID_VIP_CHANNEL_ID)
            if link_p:
                buttons.append([InlineKeyboardButton(text=f"💎 Canal VIP ({days} Días)", url=link_p)])
            else:
                errors_reported.append(f"Canal VIP (ID: <code>{PAID_VIP_CHANNEL_ID}</code>): {err_p}")

        if VIP_GROUP_ID:
            link_g, err_g = await create_invite_link_smart(bot, VIP_GROUP_ID)
            if link_g:
                buttons.append([InlineKeyboardButton(text="🌟 Grupo VIP", url=link_g)])
            else:
                errors_reported.append(f"Grupo VIP (ID: <code>{VIP_GROUP_ID}</code>): {err_g}")

        kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        txt = f"🎉 <b>¡Membresía activada por {days} días!</b>\nAccede con tus enlaces:"
        await bot.send_message(target_uid, txt, reply_markup=kb, parse_mode="HTML")
        
        admin_msg = f"✅ VIP otorgado a <code>{target_uid}</code> por {days} días."
        if errors_reported:
            admin_msg += "\n\n⚠️ <b>Aviso Enlaces No Generados:</b>\n" + "\n".join([f"• {e}" for e in errors_reported])
        await message.answer(admin_msg, parse_mode="HTML")

    return vr

# =====================================================================
# 7. WATCHDOGS Y WORKERS EN SEGUNDO PLANO (REFORZADO)
# =====================================================================
async def child_message_worker(bot_id: int):
    """Worker con entrega garantizada, reintentos y tolerancia total a fallos de formato."""
    bot = active_bots_tasks[bot_id]["bot"]
    queue = active_bots_tasks[bot_id]["dp"]["backup_queue"]
    child_db = active_bots_tasks[bot_id]["db"]
    cached_receivers = list(SUPER_ADMIN_IDS)
    last_cache_update = 0

    try:
        while True:
            first_item = await queue.get()
            batch = [first_item]
            
            while len(batch) < 10 and not queue.empty():
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            now = time.time()
            if now - last_cache_update > 30:
                try:
                    doc = await child_db.settings.find_one({"_id": "config"})
                    extra = doc.get("extra_receivers", []) if doc else []
                    cached_receivers = list(set(SUPER_ADMIN_IDS + extra))
                    last_cache_update = now
                except Exception as e:
                    logging.warning(f"Error actualizando lista de receptores: {e}")

            recipients = [r for r in cached_receivers if r]
            if not recipients and SUPER_ADMIN_IDS:
                recipients = list(SUPER_ADMIN_IDS)

            if recipients:
                for item in batch:
                    uid = item["user_id"]
                    u_name = html.quote(item.get("name", "Usuario"))
                    m_type = item["type"]
                    f_id = item["file_id"]
                    caption = (
                        f"📦 <b>Respaldo de Archivo</b>\n"
                        f"👤 <b>De:</b> {u_name} (<code>{uid}</code>)\n"
                        f"📁 <b>Tipo:</b> <code>{m_type}</code>"
                    )

                    for rid in recipients:
                        for intento in range(2):
                            try:
                                if m_type == "photo":
                                    await bot.send_photo(chat_id=rid, photo=f_id, caption=caption, parse_mode="HTML")
                                elif m_type == "video":
                                    await bot.send_video(chat_id=rid, video=f_id, caption=caption, parse_mode="HTML")
                                else:
                                    await bot.send_document(chat_id=rid, document=f_id, caption=caption, parse_mode="HTML")
                                break
                            except TelegramRetryAfter as e:
                                await asyncio.sleep(e.retry_after + 0.5)
                            except Exception as e:
                                logging.warning(f"Fallo envío unitario a {rid} (intento {intento+1}): {e}")
                                await asyncio.sleep(0.3)
                        
                        await asyncio.sleep(0.15)

            for _ in batch:
                queue.task_done()
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.critical(f"Error fatal en child_message_worker ({bot_id}): {e}")

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
                        await bot.send_message(uid, "⚠️ Tu membresía VIP Stars ha finalizado.")
                    except Exception: pass
                await child_db.users.update_one({"_id": uid}, {"$set": {"paid_vip_active": False, "vip_until": 0}})
        except Exception as e: logging.error(f"Error en limpiador VIP: {e}")
        await asyncio.sleep(3600)

async def background_central_vip_cleaner(bot: Bot, PAID_VIP_CHANNEL_ID: int):
    while True:
        try:
            now = time.time()
            cursor = master_db.vip_subscriptions.find({"paid_vip_active": True, "vip_until": {"$gt": 0, "$lt": now}})
            async for sub in cursor:
                uid = sub["_id"]
                if PAID_VIP_CHANNEL_ID:
                    try:
                        await bot.ban_chat_member(PAID_VIP_CHANNEL_ID, uid)
                        await bot.unban_chat_member(PAID_VIP_CHANNEL_ID, uid)
                        await bot.send_message(uid, "⚠️ Tu membresía VIP Stars en nuestra red ha finalizado.")
                    except Exception: pass
                await master_db.vip_subscriptions.update_one({"_id": uid}, {"$set": {"paid_vip_active": False, "vip_until": 0}})
        except Exception as e:
            logging.error(f"Error en limpiador VIP central: {e}")
        await asyncio.sleep(3600)

async def isolate_and_cleanup_bot(bot_id: int, revoked: bool = False):
    token = None
    if bot_id in active_bots_tasks:
        tasks = active_bots_tasks.pop(bot_id)
        token = tasks["bot"].token
        for k in ["polling_task", "worker_task", "vip_cleaner_task"]:
            if k in tasks: tasks[k].cancel()
        try:
            await tasks["bot"].session.close()
        except Exception:
            pass

    if bot_id in active_vip_bots_tasks:
        vtasks = active_vip_bots_tasks.pop(bot_id)
        token = token or vtasks["bot"].token
        for k in ["polling_task", "cleaner_task"]:
            if k in vtasks: vtasks[k].cancel()
        try:
            await vtasks["bot"].session.close()
        except Exception:
            pass

    if revoked:
        query = {"$or": [{"bot_token": token} if token else {}, {"bot_token": {"$regex": f"^{bot_id}:"}}]}
        if query["$or"][0] == {}:
            query["$or"].pop(0)
        await master_db.child_bots.update_many(query, {"$set": {"status": "revoked"}})
        await master_db.vip_bots.update_many(query, {"$set": {"status": "revoked"}})
        logging.info(f"🔒 [Bot {bot_id}] Desactivado permanentemente y marcado como 'revoked' en MongoDB.")

async def health_check_monitor(master_bot: Bot):
    while True:
        await asyncio.sleep(60)
        for b_id, d in list(active_bots_tasks.items()):
            try:
                await d["bot"].get_me()
            except (TelegramUnauthorizedError, Exception) as e:
                if isinstance(e, TelegramUnauthorizedError) or "unauthorized" in str(e).lower():
                    logging.warning(f"⚠️ HealthCheck detectó token muerto en bot hijo {b_id}. Limpiando...")
                    await isolate_and_cleanup_bot(b_id, revoked=True)
                    for admin_id in SUPER_ADMIN_IDS:
                        try: await master_bot.send_message(admin_id, f"🚨 <b>Alerta Anti-Ban:</b> Bot hijo con ID <code>{b_id}</code> revocado y desactivado.")
                        except Exception: pass

        for vb_id, vd in list(active_vip_bots_tasks.items()):
            try:
                await vd["bot"].get_me()
            except (TelegramUnauthorizedError, Exception) as e:
                if isinstance(e, TelegramUnauthorizedError) or "unauthorized" in str(e).lower():
                    logging.warning(f"⚠️ HealthCheck detectó token muerto en bot VIP {vb_id}. Limpiando...")
                    await isolate_and_cleanup_bot(vb_id, revoked=True)
                    for admin_id in SUPER_ADMIN_IDS:
                        try: await master_bot.send_message(admin_id, f"🚨 <b>Alerta Anti-Ban:</b> Bot VIP con ID <code>{vb_id}</code> revocado y desactivado.")
                        except Exception: pass

async def child_polling_wrapper(dp: Dispatcher, bot: Bot, bot_id: int):
    try:
        await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, handle_signals=False)
    except (TelegramUnauthorizedError, TelegramForbiddenError):
        logging.error(f"❌ [Bot {bot_id}] Token revocado detectado. Aislándolo...")
        await isolate_and_cleanup_bot(bot_id, revoked=True)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        if "Unauthorized" in str(e) or "unauthorized" in str(e).lower():
            logging.error(f"❌ [Bot {bot_id}] Token revocado durante la ejecución. Desactivándolo...")
            await isolate_and_cleanup_bot(bot_id, revoked=True)
        else:
            logging.critical(f"💥 [Bot {bot_id}] Error no controlado en polling: {e}")
            await notify_admins_alert(f"Error crítico en polling bot {bot_id}: {e}")
            await isolate_and_cleanup_bot(bot_id, revoked=False)
    finally:
        if not bot.session.closed:
            await bot.session.close()

async def start_child_bot(config: dict) -> bool:
    token = config["bot_token"]
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    
    try:
        me = await bot.get_me()
        bot_id = me.id
    except (TelegramUnauthorizedError, Exception) as e:
        logging.error(f"❌ Token inválido o revocado ({token[:10]}...): {e}")
        await bot.session.close()
        await master_db.child_bots.update_one({"bot_token": token}, {"$set": {"status": "revoked"}})
        return False

    if bot_id in active_bots_tasks:
        await bot.session.close()
        return True

    db_ver = config.get("db_version", "v1")
    child_db = master_db_client[f"child_{bot_id}_{db_ver}"]

    try:
        await child_db.exchange_history.create_index([("created_at", 1)], expireAfterSeconds=5184000)
        await child_db.inventory.create_index([("user_id", 1), ("file_unique_id", 1)])
    except Exception:
        pass
    
    dp = Dispatcher(storage=MemoryStorage())
    ctx_vars = {
        "dp": dp,
        "active_chats": {}, "waiting_list": [], "waiting_vip": [], "pending_trades": {},
        "active_viewers": {}, "chat_threads": {},
        "backup_queue": asyncio.Queue(),
        "upload_buffers": {},
        "upload_tasks": {}
    }

    try:
        saved_state = await child_db.session_state.find_one({"_id": "rooms"})
        if saved_state:
            chats_map = {int(k): int(v) for k, v in saved_state.get("active_chats", {}).items()}
            ctx_vars["active_chats"].update(chats_map)
            ctx_vars["waiting_list"].extend([int(x) for x in saved_state.get("waiting_list", [])])
            ctx_vars["waiting_vip"].extend([int(x) for x in saved_state.get("waiting_vip", [])])
            
            for uid in chats_map:
                key = StorageKey(bot_id=bot_id, chat_id=uid, user_id=uid)
                await FSMContext(storage=dp.storage, key=key).set_state(BotStates.chatting)
    except Exception as e:
        logging.error(f"Error restaurando sesiones FSM: {e}")

    dp.include_router(create_child_router(child_config=config, child_db=child_db, ctx_vars=ctx_vars))

    paid_ch = clean_chat_id(config.get("paid_vip_channel_id"))
    active_bots_tasks[bot_id] = {
        "bot": bot, "db": child_db, "dp": ctx_vars,
        "polling_task": asyncio.create_task(child_polling_wrapper(dp, bot, bot_id)),
        "worker_task": asyncio.create_task(child_message_worker(bot_id)),
        "vip_cleaner_task": asyncio.create_task(background_vip_cleaner_runner(bot, child_db, paid_ch))
    }
    return True

async def start_vip_manager_bot(config: dict) -> bool:
    token = config["bot_token"]
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        me = await bot.get_me()
        bot_id = me.id
    except (TelegramUnauthorizedError, Exception) as e:
        logging.error(f"❌ Token VIP inválido o revocado ({token[:10]}...): {e}")
        await bot.session.close()
        await master_db.vip_bots.update_one({"bot_token": token}, {"$set": {"status": "revoked"}})
        return False

    if bot_id in active_vip_bots_tasks:
        await bot.session.close()
        return True

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(create_vip_manager_router(config))
    paid_ch = clean_chat_id(config.get("paid_vip_channel_id"))

    active_vip_bots_tasks[bot_id] = {
        "bot": bot, "dp": dp,
        "polling_task": asyncio.create_task(child_polling_wrapper(dp, bot, bot_id)),
        "cleaner_task": asyncio.create_task(background_central_vip_cleaner(bot, paid_ch))
    }
    logging.info(f"👑 Bot Gestor VIP (@{me.username}) activo y en línea.")
    return True

# =====================================================================
# 8. HANDLERS MASTER BOT (COBROS CENTRALIZADOS Y WIZARD DUAL)
# =====================================================================
@master_dp.message(CommandStart())
async def cmd_start_master(message: Message, state: FSMContext, bot: Bot):
    args = message.text.split(maxsplit=1)
    
    if len(args) > 1 and args[1].startswith("paystars_"):
        parts = args[1].split("_")
        target_bot_id_str = parts[1]
        tier_key = parts[2] if len(parts) > 2 else "7d"
        
        if target_bot_id_str.isdigit() and tier_key in VIP_TIERS:
            target_bot_id = int(target_bot_id_str)
            tier_info = VIP_TIERS[tier_key]
            prices = [LabeledPrice(label=tier_info["label"], amount=tier_info["stars"])]
            payload = f"vip_stars_{target_bot_id}_{message.from_user.id}_{tier_key}"
            try:
                await bot.send_invoice(
                    chat_id=message.from_user.id,
                    title=f"💎 {tier_info['label']}",
                    description=f"Acceso VIP exclusivo por {tier_info['days']} día(s).",
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
        [InlineKeyboardButton(text="🤖 Crear Bot de Intercambio (Hijo)", callback_data="master_crear")],
        [InlineKeyboardButton(text="👑 Crear Bot Gestor VIP", callback_data="master_crear_vip")],
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
        tier_key = parts[4] if len(parts) > 4 else "7d"
        tier_info = VIP_TIERS.get(tier_key, VIP_TIERS["7d"])
        days = tier_info["days"]

        now = time.time()
        sub = await master_db.vip_subscriptions.find_one({"_id": user_id}) or {}
        base = max(now, sub.get("vip_until", 0))
        new_vip = base + (days * 86400)

        await master_db.vip_subscriptions.update_one(
            {"_id": user_id},
            {"$set": {"vip_until": new_vip, "paid_vip_active": True, "tier": tier_key, "updated_at": datetime.now(timezone.utc)}},
            upsert=True
        )

        buttons = []
        child_info = active_bots_tasks.get(target_bot_id)
        if child_info:
            child_db = child_info["db"]
            child_bot = child_info["bot"]
            cfg = await master_db.child_bots.find_one({"bot_token": child_bot.token}) or {}
            paid_ch = clean_chat_id(cfg.get("paid_vip_channel_id"))
            free_vip_id = clean_chat_id(cfg.get("vip_group_id"))

            await child_db.users.update_one(
                {"_id": user_id}, 
                {"$set": {"vip_until": new_vip, "paid_vip_active": True, "in_vip": True, "notified_vip": True}}, 
                upsert=True
            )

            if paid_ch:
                link_p, _ = await create_invite_link_smart(child_bot, paid_ch)
                if link_p:
                    buttons.append([InlineKeyboardButton(text=f"💎 Canal VIP Stars ({days} Días)", url=link_p)])

            if free_vip_id:
                link_g, _ = await create_invite_link_smart(child_bot, free_vip_id)
                if link_g:
                    buttons.append([InlineKeyboardButton(text="🌟 Grupo VIP de la Comunidad", url=link_g)])

        vip_info = active_vip_bots_tasks.get(target_bot_id)
        if vip_info:
            vip_bot = vip_info["bot"]
            vcfg = await master_db.vip_bots.find_one({"bot_token": vip_bot.token}) or {}
            paid_ch_v = clean_chat_id(vcfg.get("paid_vip_channel_id"))
            free_vip_v = clean_chat_id(vcfg.get("vip_group_id"))

            if paid_ch_v:
                link_pv, _ = await create_invite_link_smart(vip_bot, paid_ch_v)
                if link_pv:
                    buttons.append([InlineKeyboardButton(text=f"💎 Canal VIP Stars ({days} Días)", url=link_pv)])

            if free_vip_v:
                link_gv, _ = await create_invite_link_smart(vip_bot, free_vip_v)
                if link_gv:
                    buttons.append([InlineKeyboardButton(text="🌟 Grupo VIP", url=link_gv)])

        txt_pago = (
            f"🎉 <b>¡Pago con Estrellas confirmado! ({tier_info['label']})</b>\n\n"
            f"Tu membresía VIP de <b>{days} días</b> está activa en toda la red.\n"
        )
        if buttons:
            await message.answer(txt_pago, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        else:
            await message.answer("🎉 <b>¡Pago confirmado!</b> Tu membresía de VIP ha sido registrada en el sistema central.")

# ---- Wizard 1: Crear Bot Hijo de Intercambios ----
@master_dp.callback_query(F.data == "master_crear")
async def step1_token(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    await callback.message.edit_text("🤖 <b>Paso 1/7:</b> Envía el <b>Token</b> del bot hijo dado por @BotFather:", parse_mode="HTML")
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
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    }
    await message.answer("⏳ <i>Desplegando bot hijo...</i>", parse_mode="HTML")
    success = await start_child_bot(new_cfg)
    if success:
        await master_db.child_bots.insert_one(new_cfg)
        temp_bot = Bot(token=data["token"])
        try: me = await temp_bot.get_me()
        finally: await temp_bot.session.close()
        
        summary = (
            "🎉 <b>¡BOT HIJO ACTIVO Y EN LÍNEA!</b>\n\n"
            f"🤖 Usuario: <code>@{me.username}</code> (ID: <code>{me.id}</code>)\n"
            f"📢 Canal Sub: <code>{data['sub_id']}</code>\n"
            f"🌟 VIP Gratis: <code>{data['vip_id']}</code>\n"
            f"💎 VIP Stars: <code>{data.get('paid_vip_id', '0')}</code>\n"
            f"📂 Logs: <code>{data.get('log_id', '0')}</code>\n"
            f"🗄️ Versión BD: <code>{db_ver}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Volver al Panel", callback_data="master_panel")]])
        await message.answer(summary, reply_markup=kb, parse_mode="HTML")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Reintentar", callback_data="master_crear")]])
        await message.answer("❌ Error: Token inválido o revocado en Telegram.", reply_markup=kb, parse_mode="HTML")
    await state.clear()

# ---- Wizard 2: Crear Bot Gestor VIP Independiente ----
@master_dp.callback_query(F.data == "master_crear_vip")
async def vip_wizard_step1(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    await callback.message.edit_text("👑 <b>Paso 1/3:</b> Envía el <b>Token</b> del Bot Gestor VIP (obtenido en @BotFather):", parse_mode="HTML")
    await state.set_state(CreateVipManagerBot.waiting_for_token)

@master_dp.message(CreateVipManagerBot.waiting_for_token)
async def vip_wizard_step2(message: Message, state: FSMContext):
    await state.update_data(token=message.text.strip())
    await message.answer("💎 <b>Paso 2/3:</b> Envía el <b>ID del Canal VIP de Pago (Stars)</b> donde este bot será Administrador:", parse_mode="HTML")
    await state.set_state(CreateVipManagerBot.waiting_for_paid_vip_id)

@master_dp.message(CreateVipManagerBot.waiting_for_paid_vip_id)
async def vip_wizard_step3(message: Message, state: FSMContext):
    await state.update_data(paid_vip_id=extract_chat_id(message))
    await message.answer("🌟 <b>Paso 3/3:</b> Envía el <b>ID del Grupo VIP Comunitario</b>:", parse_mode="HTML")
    await state.set_state(CreateVipManagerBot.waiting_for_vip_group_id)

@master_dp.message(CreateVipManagerBot.waiting_for_vip_group_id)
async def vip_wizard_final(message: Message, state: FSMContext):
    vip_group = extract_chat_id(message)
    data = await state.get_data()
    new_vip_cfg = {
        "owner_id": message.from_user.id,
        "bot_token": data["token"],
        "status": "active",
        "paid_vip_channel_id": data["paid_vip_id"],
        "vip_group_id": vip_group,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    }
    await message.answer("⏳ <i>Iniciando Bot Gestor VIP...</i>", parse_mode="HTML")
    success = await start_vip_manager_bot(new_vip_cfg)
    if success:
        await master_db.vip_bots.insert_one(new_vip_cfg)
        temp_bot = Bot(token=data["token"])
        try: me = await temp_bot.get_me()
        finally: await temp_bot.session.close()

        txt = (
            "🎉 <b>¡BOT GESTOR VIP EN LÍNEA!</b>\n\n"
            f"👑 Usuario: <code>@{me.username}</code> (ID: <code>{me.id}</code>)\n"
            f"💎 Canal VIP Pago: <code>{data['paid_vip_id']}</code>\n"
            f"🌟 Grupo VIP: <code>{vip_group}</code>\n\n"
            "<i>Este bot ahora gestiona el acceso a tus canales VIP y redirige los cobros Stars hacia el Master Bot.</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Volver al Panel", callback_data="master_panel")]])
        await message.answer(txt, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer("❌ Error al iniciar el Bot VIP. Verifica que el token no haya sido revocado.")
    await state.clear()

# ---- Panel de Supervisión y Control ----
@master_dp.callback_query(F.data == "master_panel")
async def cb_master_panel(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    
    cursor_child = master_db.child_bots.find({"status": "active"})
    child_list = [b async for b in cursor_child]

    cursor_vip = master_db.vip_bots.find({"status": "active"})
    vip_list = [v async for v in cursor_vip]

    txt = (
        f"📊 <b>ESTADO DE LA RED SAAS</b>\n\n"
        f"🤖 Bots de Intercambio: <code>{len(child_list)}</code>\n"
        f"👑 Bots Gestores VIP: <code>{len(vip_list)}</code>\n\n"
        f"<b>Nodos de Intercambio:</b>\n"
    )
    keyboard = []
    
    for b in child_list:
        temp_b = Bot(token=b["bot_token"])
        try:
            me = await temp_b.get_me()
            txt += f"• @{me.username} (ID: <code>{me.id}</code>)\n"
            keyboard.append([InlineKeyboardButton(text=f"⚙️ Intercambio: @{me.username}", callback_data=f"manage_bot_{me.id}")])
        except Exception:
            txt += "• <i>Bot Inaccesible / Revocado</i>\n"
        finally:
            await temp_b.session.close()

    if vip_list:
        txt += "\n<b>Gestores VIP:</b>\n"
        for vb in vip_list:
            temp_vb = Bot(token=vb["bot_token"])
            try:
                me_v = await temp_vb.get_me()
                txt += f"• 👑 @{me_v.username} (ID: <code>{me_v.id}</code>)\n"
                keyboard.append([InlineKeyboardButton(text=f"👑 VIP: @{me_v.username}", callback_data=f"manage_vip_{me_v.id}")])
            except Exception: pass
            finally: await temp_vb.session.close()

    keyboard.append([InlineKeyboardButton(text="➕ Crear Bot Intercambio", callback_data="master_crear"),
                     InlineKeyboardButton(text="👑 Crear Bot VIP", callback_data="master_crear_vip")])
    await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")

@master_dp.callback_query(F.data.startswith("manage_bot_"))
async def cb_manage_bot(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    bot_id = int(callback.data.split("_")[2])
    bot_ctx = active_bots_tasks.get(bot_id)
    if not bot_ctx: return await callback.answer("⚠️ Bot inactivo o no encontrado.", show_alert=True)
        
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

@master_dp.callback_query(F.data.startswith("manage_vip_"))
async def cb_manage_vip_bot(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    bot_id = int(callback.data.split("_")[2])
    v_ctx = active_vip_bots_tasks.get(bot_id)
    if not v_ctx: return await callback.answer("⚠️ Bot VIP no encontrado.", show_alert=True)
    
    cfg = await master_db.vip_bots.find_one({"bot_token": v_ctx["bot"].token}) or {}
    total_subs = await master_db.vip_subscriptions.count_documents({"paid_vip_active": True})
    me = await v_ctx["bot"].get_me()
    
    txt = (
        f"👑 <b>Gestión de Bot VIP: @{me.username}</b>\n\n"
        f"🆔 ID: <code>{bot_id}</code>\n"
        f"💎 Canal de Pago: <code>{cfg.get('paid_vip_channel_id')}</code>\n"
        f"🌟 Grupo Comunitario: <code>{cfg.get('vip_group_id')}</code>\n"
        f"👥 Suscriptores Activos en Red: <code>{total_subs}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Desconectar Bot VIP", callback_data=f"stop_vip_{bot_id}")],
        [InlineKeyboardButton(text="⬅️ Volver a Lista", callback_data="master_panel")]
    ])
    await callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

@master_dp.callback_query(F.data.startswith("stop_bot_"))
async def cb_stop_bot(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    bot_id = int(callback.data.split("_")[2])
    await isolate_and_cleanup_bot(bot_id, revoked=True)
    await callback.answer("Bot detenido y marcado como revocado.", show_alert=True)
    await cb_master_panel(callback)

@master_dp.callback_query(F.data.startswith("stop_vip_"))
async def cb_stop_vip_bot(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS: return
    bot_id = int(callback.data.split("_")[2])
    await isolate_and_cleanup_bot(bot_id, revoked=True)
    await callback.answer("Bot VIP detenido y marcado como revocado.", show_alert=True)
    await cb_master_panel(callback)

# =====================================================================
# 9. INICIO Y SERVIDOR WEB
# =====================================================================
async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_webapp)
    app.router.add_get("/api/data", api_get_data)
    app.router.add_post("/api/toggle_radar", api_toggle_radar)
    app.router.add_post("/api/bonus", api_claim_bonus)
    app.router.add_post("/api/clear", api_clear_inv)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    return runner

async def main():
    global MASTER_BOT_USERNAME, GLOBAL_MASTER_BOT
    
    if not MASTER_TOKEN or not MASTER_MONGO_URI:
        raise RuntimeError("Configura MASTER_TOKEN y MONGO_URI en tus variables de entorno.")
        
    master_bot = Bot(token=MASTER_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    GLOBAL_MASTER_BOT = master_bot
    me = await master_bot.get_me()
    MASTER_BOT_USERNAME = me.username
    
    await master_bot.delete_webhook(drop_pending_updates=True)
    runner = await web_server()
    
    cursor_child = master_db.child_bots.find({"status": "active"})
    async for cfg in cursor_child:
        await start_child_bot(cfg)

    cursor_vip = master_db.vip_bots.find({"status": "active"})
    async for vcfg in cursor_vip:
        await start_vip_manager_bot(vcfg)
        
    monitor_task = asyncio.create_task(health_check_monitor(master_bot))
    logging.info(f"🚀 SaaS Master (@{MASTER_BOT_USERNAME}) online en puerto {PORT}.")
    
    try:
        await master_dp.start_polling(master_bot)
    finally:
        monitor_task.cancel()
        for bid in list(active_bots_tasks.keys()):
            await isolate_and_cleanup_bot(bid)
        for vbid in list(active_vip_bots_tasks.keys()):
            await isolate_and_cleanup_bot(vbid)
            
        try:
            if not master_bot.session.closed:
                await master_bot.session.close()
        except Exception: pass
            
        master_db_client.close()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())