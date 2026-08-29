import os
import asyncio
import logging
import time
import random
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
db = db_client.intercambio_bot

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

# --- API Y FRONTEND PARA LA MINI APP ---
async def api_get_data(request):
    user_id = int(request.query.get("id", 0))
    if not user_id: return web.json_response({"error": "No ID"})
    
    user = await get_user(user_id)
    fotos = await db.inventory.count_documents({"user_id": user_id, "type": "photo"})
    videos = await db.inventory.count_documents({"user_id": user_id, "type": "video"})
    
    now = time.time()
    last_bonus = user.get("last_bonus", 0)
    cooldown = 6 * 3600 # 6 horas
    time_left = max(0, (last_bonus + cooldown) - now)
    
    return web.json_response({
        "fotos": fotos, "videos": videos,
        "reputation": user.get("reputation", 0),
        "referrals": user.get("referrals", 0),
        "time_left": time_left
    })

async def api_claim_bonus(request):
    data = await request.json()
    user_id = int(data.get("id", 0))
    if not user_id: return web.json_response({"error": "No ID"})
    
    user = await get_user(user_id)
    now = time.time()
    last_bonus = user.get("last_bonus", 0)
    cooldown = 6 * 3600
    
    if now < last_bonus + cooldown:
        return web.json_response({"success": False, "error": "Cooldown active"})
        
    puntos_ganados = random.randint(1, 5)
    nueva_rep = user.get("reputation", 0) + puntos_ganados
    
    await db.users.update_one({"_id": user_id}, {"$set": {"last_bonus": now, "reputation": nueva_rep}})
    await check_vip_status(user_id) # Revisamos si con este bonus alcanzó el VIP
    
    return web.json_response({"success": True, "bonus": puntos_ganados, "new_rep": nueva_rep, "time_left": cooldown})

async def handle_webapp(request):
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Exchange Panel</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
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
            body { background: var(--bg); color: var(--text); padding: 16px; }
            .header { text-align: center; margin-bottom: 20px; }
            .header h1 { font-size: 22px; margin-bottom: 4px; }
            .header p { font-size: 14px; color: var(--hint); }
            .tabs { display: flex; background: var(--card-bg); border-radius: 12px; padding: 4px; margin-bottom: 16px; }
            .tab { flex: 1; text-align: center; padding: 10px; font-size: 13px; font-weight: 600; color: var(--hint); cursor: pointer; border-radius: 8px; }
            .tab.active { background: var(--accent); color: var(--accent-txt); }
            .section { display: none; flex-direction: column; gap: 12px; }
            .section.active { display: flex; animation: fadeIn 0.3s; }
            @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
            .card { background: var(--card-bg); border-radius: 16px; padding: 16px; display: flex; align-items: center; justify-content: space-between; }
            .card h3 { font-size: 14px; color: var(--hint); margin-bottom: 6px; }
            .value { font-size: 20px; font-weight: 700; }
            .progress-bg { background: rgba(255,255,255,0.1); border-radius: 8px; height: 12px; margin-top: 10px; overflow: hidden; width: 100%; }
            .progress-fill { background: linear-gradient(90deg, #FFD700, #FFA500); height: 100%; width: 0%; transition: 0.5s; }
            .btn-bonus { border: none; padding: 10px 16px; border-radius: 8px; font-weight: bold; color: #fff; cursor: pointer; transition: 0.3s; }
            .btn-main { background: var(--accent); color: var(--accent-txt); border: none; border-radius: 12px; padding: 14px; width: 100%; font-size: 15px; font-weight: bold; cursor: pointer; margin-top: 10px; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>⚡ Panel de Control</h1>
            <p id="greeting">Sincronizando con Telegram...</p>
        </div>

        <div class="tabs">
            <div class="tab active" onclick="switchTab('stats', this)">📊 Mi Nivel</div>
            <div class="tab" onclick="switchTab('inventory', this)">📦 Inventario</div>
        </div>

        <div id="stats" class="section active">
            <!-- Tarjeta de VIP -->
            <div class="card" style="flex-direction: column; align-items: stretch;">
                <div style="display: flex; justify-content: space-between;">
                    <h3>👑 Progreso hacia VIP</h3>
                    <span class="value" id="vip-text" style="font-size:16px;">--/20</span>
                </div>
                <div class="progress-bg"><div class="progress-fill" id="vip-fill"></div></div>
                <p style="font-size:12px; color:var(--hint); margin-top:8px;">Necesitas 20 puntos para entrar al grupo secreto.</p>
            </div>

            <!-- Tarjeta de Bonus -->
            <div class="card">
                <div>
                    <h3>🎁 Bonus Diario</h3>
                    <div class="value" id="bonus-text" style="font-size:16px;">Calculando...</div>
                </div>
                <button id="btn-bonus" class="btn-bonus" onclick="claimBonus()" disabled>Reclamar</button>
            </div>

            <button class="btn-main" onclick="tg.close()">Volver al Chat</button>
        </div>

        <div id="inventory" class="section">
            <div class="card"><div><h3>📷 Fotos</h3><div class="value" id="photo-count">--</div></div></div>
            <div class="card"><div><h3>🎥 Videos</h3><div class="value" id="video-count">--</div></div></div>
            <p style="font-size:12px; color:#ff7875; padding:8px; background:rgba(255,77,79,0.1); border-radius:8px;">⚠️ Nunca borres los mensajes enviados al bot o perderás los archivos.</p>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            let userId = tg.initDataUnsafe?.user?.id || 0;
            if (tg.initDataUnsafe?.user) document.getElementById('greeting').innerText = `Hola, ${tg.initDataUnsafe.user.first_name} 👋`;

            function switchTab(tabId, element) {
                document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                element.classList.add('active');
            }

            let bonusTimer;
            function updateBonusUI(timeLeft) {
                let btn = document.getElementById("btn-bonus");
                let txt = document.getElementById("bonus-text");
                clearInterval(bonusTimer);
                
                if (timeLeft <= 0) {
                    btn.disabled = false;
                    btn.style.background = "var(--accent)";
                    txt.innerText = "¡Disponible!";
                } else {
                    btn.disabled = true;
                    btn.style.background = "var(--hint)";
                    bonusTimer = setInterval(() => {
                        timeLeft--;
                        if (timeLeft <= 0) updateBonusUI(0);
                        else {
                            let h = Math.floor(timeLeft / 3600);
                            let m = Math.floor((timeLeft % 3600) / 60);
                            let s = Math.floor(timeLeft % 60);
                            txt.innerText = `${h}h ${m}m ${s}s`;
                        }
                    }, 1000);
                }
            }

            async function loadData() {
                if (!userId) return;
                let res = await fetch(`/api/data?id=${userId}`);
                let data = await res.json();
                
                document.getElementById("photo-count").innerText = data.fotos;
                document.getElementById("video-count").innerText = data.videos;
                
                let rep = data.reputation;
                document.getElementById("vip-text").innerText = `${rep}/20 Pts`;
                document.getElementById("vip-fill").style.width = Math.min(100, (rep / 20) * 100) + "%";
                
                updateBonusUI(data.time_left);
            }

            async function claimBonus() {
                document.getElementById("btn-bonus").disabled = true;
                let res = await fetch(`/api/bonus`, {
                    method: "POST", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({id: userId})
                });
                let data = await res.json();
                if(data.success) {
                    tg.showAlert(`🎉 ¡Felicidades! Has ganado ${data.bonus} puntos de reputación.`);
                    loadData();
                } else {
                    tg.showAlert("⚠️ Aún debes esperar.");
                }
            }
            loadData();
        </script>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type="text/html")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_webapp)
    app.router.add_get("/api/data", api_get_data)
    app.router.add_post("/api/bonus", api_claim_bonus)
    
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

    if len(args) > 1 and args[1].isdigit():
        inviter_id = int(args[1])
        if inviter_id != user_id and not await db.users.find_one({"referred_by": user_id}):
            await save_user(user_id, {"referred_by": inviter_id})
            await db.users.update_one({"_id": inviter_id}, {"$inc": {"referrals": 1}})
            await check_vip_status(inviter_id) 

    if not await check_force_sub(user_id):
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Unirse al Canal", url=FORCE_SUB_CHANNEL_LINK)],
            [InlineKeyboardButton(text="✅ Verificar Ingreso", callback_data="verify_sub")]
        ])
        return await message.answer("🛑 **Acceso Restringido**\nDebes unirte a nuestro canal para usar el bot.", reply_markup=markup, parse_mode="Markdown")

    await show_main_menu(user_id)
    await state.set_state(BotStates.idle)

@router.callback_query(F.data == "verify_sub")
async def verify_sub(callback: CallbackQuery):
    if await check_force_sub(callback.from_user.id):
        await callback.message.delete()
        await show_main_menu(callback.from_user.id)
    else: await callback.answer("⚠️ Aún no te has unido.", show_alert=True)

async def show_main_menu(user_id):
    user = await get_user(user_id)
    bot_info = await bot.get_me()
    my_link = f"https://t.me/{bot_info.username}?start={user['_id']}"
    
    # URL DE LA MINI APP (Asegúrate de cambiar esto por tu ngrok o Render link)
    webapp_url = os.environ.get("RENDER_EXTERNAL_URL", "https://tu-servicio.onrender.com")
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Abrir Panel de Control", web_app=WebAppInfo(url=webapp_url))],
        [InlineKeyboardButton(text="💬 Buscar Chat", callback_data="find_chat"), InlineKeyboardButton(text="🆔 Conectar ID", callback_data="connect_id")],
        [InlineKeyboardButton(text="👤 Mi Perfil", callback_data="my_profile"), InlineKeyboardButton(text="🔗 Compartir Link", url=f"https://t.me/share/url?url={my_link}")]
    ])
    
    txt = "👋 **¡Bienvenido a la red de intercambio!**\n\n⚠️ **REQUISITO CLAVE:** Sube material propio a este chat para poder hacer intercambios. ¡Sin videos o fotos en tu inventario, no podrás recibir nada!\n\nUtiliza la nueva **Mini App** para reclamar tu bonus diario y ver tu progreso VIP. 🚀"
    await bot.send_message(chat_id=user_id, text=txt, reply_markup=markup, parse_mode="Markdown")

@router.message(Command("help"))
async def cmd_help(message: Message):
    txt = "🤖 **Guía Completa**\n\n📦 **1. Carga inventario:** Sube fotos/videos aquí para llenarlo.\n💬 **2. Inicia Chat:** Conecta al azar o por ID.\n🤝 **3. Lotes:** Usa el botón 'Proponer' en el chat.\n🌟 **4. VIP:** Gana 20 puntos de reputación (con intercambios o bonus diarios) para entrar al grupo VIP."
    await message.answer(txt, parse_mode="Markdown")

@router.callback_query(F.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    uid = user["_id"]
    fotos = await db.inventory.count_documents({"user_id": uid, "type": "photo"})
    videos = await db.inventory.count_documents({"user_id": uid, "type": "video"})
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Cambiar Modo", callback_data="toggle_mode")],
        [InlineKeyboardButton(text="⬅️ Volver", callback_data="back_main")]
    ])
    modo = "🕵️‍♂️ Anónimo" if user.get("mode") == "anon" else "👤 Público"
    txt = f"👤 **Tu Perfil**\n\n🆔 ID: `{uid}`\n🌟 Reputación: `{user.get('reputation', 0)}/20`\n👥 Referidos: `{user.get('referrals', 0)}/3`\n🎭 Modo: **{modo}**\n\n📦 Inventario: 📷 {fotos} | 🎥 {videos}"
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
    await state.set_state(BotStates.waiting_for_id)
    await callback.message.edit_text("✏️ Escribe el **ID numérico** del usuario:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Cancelar", callback_data="back_main")]]), parse_mode="Markdown")

@router.message(StateFilter(BotStates.waiting_for_id))
async def process_connect_id(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("⚠️ Debe ser un número.")
    t_id, u_id = int(message.text), message.from_user.id
    if t_id == u_id: return
    if t_id in active_chats or t_id in waiting_list: return await message.answer("⚠️ Ocupado.")
    
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Aceptar", callback_data=f"accept_id_{u_id}")], [InlineKeyboardButton(text="❌ Rechazar", callback_data=f"reject_id_{u_id}")]])
    await bot.send_message(t_id, f"🔔 **Solicitud de Chat de ID:** `{u_id}`", reply_markup=markup, parse_mode="Markdown")
    await message.answer("⏳ Solicitud enviada.")
    await state.set_state(BotStates.idle)

@router.callback_query(F.data.startswith("accept_id_"))
async def accept_id_connection(callback: CallbackQuery, state: FSMContext):
    t_id, u_id = int(callback.data.split("_")[2]), callback.from_user.id
    if t_id in active_chats or u_id in active_chats: return await callback.answer("Ocupado.", show_alert=True)
    active_chats[u_id], active_chats[t_id] = t_id, u_id
    
    await state.set_state(BotStates.chatting)
    await dp.fsm.resolve_context(bot, t_id, t_id).set_state(BotStates.chatting)
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio"), KeyboardButton(text="❌ Desconectar")]], resize_keyboard=True)
    await bot.send_message(u_id, "✅ **Conexión Establecida.**", reply_markup=kb, parse_mode="Markdown")
    await bot.send_message(t_id, "✅ **Conexión Establecida.**", reply_markup=kb, parse_mode="Markdown")
    await callback.message.delete()

@router.callback_query(F.data.startswith("reject_id_"))
async def reject_id_connection(callback: CallbackQuery):
    await callback.message.edit_text("❌ Rechazaste la solicitud.")
    await bot.send_message(int(callback.data.split("_")[2]), "❌ Solicitud rechazada.")

@router.callback_query(F.data == "find_chat")
async def find_chat(callback: CallbackQuery, state: FSMContext):
    u_id = callback.from_user.id
    if waiting_list:
        t_id = waiting_list.pop(0)
        active_chats[u_id], active_chats[t_id] = t_id, u_id
        await state.set_state(BotStates.chatting)
        await dp.fsm.resolve_context(bot, t_id, t_id).set_state(BotStates.chatting)
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio"), KeyboardButton(text="❌ Desconectar")]], resize_keyboard=True)
        await bot.send_message(u_id, "✅ **¡Chat encontrado!**", reply_markup=kb, parse_mode="Markdown")
        await bot.send_message(t_id, "✅ **¡Chat encontrado!**", reply_markup=kb, parse_mode="Markdown")
        await callback.message.delete()
    else:
        waiting_list.append(u_id)
        await state.set_state(BotStates.searching)
        await callback.message.edit_text("🔍 **Buscando...**", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancelar", callback_data="leave_chat")]]), parse_mode="Markdown")

@router.message(F.text.in_(["❌ Desconectar", "❌ Disconnect"]))
@router.message(Command("leave"))
@router.callback_query(F.data == "leave_chat")
async def leave_chat(event, state: FSMContext):
    u_id = event.from_user.id
    if u_id in waiting_list: waiting_list.remove(u_id)
    t_id = active_chats.pop(u_id, None)
    if t_id:
        active_chats.pop(t_id, None)
        await dp.fsm.resolve_context(bot, t_id, t_id).set_state(BotStates.idle)
        await bot.send_message(t_id, "❌ **El chat finalizó.**", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        await show_main_menu(t_id)
    await state.set_state(BotStates.idle)
    
    if isinstance(event, Message): await event.answer("Has salido.", reply_markup=ReplyKeyboardRemove())
    else:
        await event.message.delete()
        await bot.send_message(u_id, "Has salido.", reply_markup=ReplyKeyboardRemove())
    await show_main_menu(u_id)

# --- VIP Y REPUTACIÓN ---
async def check_vip_status(user_id):
    user = await get_user(user_id)
    if user.get("notified_vip"): return
    # Ahora revisamos si tiene 20 de reputación o 3 referidos
    if user.get("referrals", 0) >= 3 or user.get("reputation", 0) >= 20:
        invite = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌟 Entrar al VIP", url=invite.invite_link)]])
        await bot.send_message(user_id, "🎉 **¡Te has ganado acceso al VIP!**", reply_markup=markup, parse_mode="Markdown")
        await save_user(user_id, {"notified_vip": True, "in_vip": True})

@router.message(F.chat.id == VIP_GROUP_ID, F.photo | F.video | F.document)
async def vip_group_activity(message: Message):
    await save_user(message.from_user.id, {"in_vip": True})

async def send_rating_request(user_id, target_id):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 Buen usuario", callback_data=f"rate_good_{target_id}"), InlineKeyboardButton(text="👎 Malo", callback_data=f"rate_bad_{target_id}")]
    ])
    await bot.send_message(user_id, "¿Deseas darle un punto extra a tu compañero?", reply_markup=markup)

@router.callback_query(F.data.startswith("rate_"))
async def process_rating(callback: CallbackQuery):
    action, _, t_id = callback.data.split("_")
    if action == "good":
        await db.users.update_one({"_id": int(t_id)}, {"$inc": {"reputation": 1}})
        await check_vip_status(int(t_id))
    await callback.message.edit_text("✅ Valoración enviada.")

# --- INVENTARIO Y REENVÍO MULTIMEDIA DIRECTO ---
@router.message(F.chat.type == "private", F.photo | F.video | F.document)
async def handle_media(message: Message):
    u_id = message.from_user.id
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
            await message.answer(f"📥 **Archivo(s) guardado(s).** (Total: {total})\n\n⚠️ **Importante:** No elimines los mensajes que subas aquí.", parse_mode="Markdown")

# --- INTERCAMBIO AUTOMÁTICO EN LOTE ---
async def get_random_batch(db, sender_id: int, receiver_id: int, category: str, amount: int):
    already_sent = [doc["file_unique_id"] async for doc in db.exchange_history.find({"sender_id": sender_id, "receiver_id": receiver_id}, {"file_unique_id": 1})]
    match_query = {"user_id": sender_id, "file_unique_id": {"$nin": already_sent}}
    if category != "mixed": match_query["type"] = category
    pipeline = [{"$match": match_query}, {"$sample": {"size": amount + 15}}]
    selected = [doc async for doc in db.inventory.aggregate(pipeline)]
    return len(selected) >= amount, selected

@router.message(StateFilter(BotStates.chatting), F.text == "🤝 Proponer Intercambio")
async def btn_propose(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_trade_type)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Fotos", callback_data="settype_photo"), InlineKeyboardButton(text="🎥 Videos", callback_data="settype_video")],
        [InlineKeyboardButton(text="🔀 Mixto", callback_data="settype_mixed")]
    ])
    await message.answer("🎬 **¿Qué deseas intercambiar?**", reply_markup=markup, parse_mode="Markdown")

@router.callback_query(StateFilter(BotStates.waiting_trade_type), F.data.startswith("settype_"))
async def process_trade_type(callback: CallbackQuery, state: FSMContext):
    await state.update_data(trade_type=callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_trade_amount)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10x10", callback_data="trade_10"), InlineKeyboardButton(text="50x50", callback_data="trade_50"), InlineKeyboardButton(text="100x100", callback_data="trade_100")]
    ])
    await callback.message.edit_text("🔢 **¿Cuántos archivos?**\n\nSelecciona o escribe el número:", reply_markup=markup, parse_mode="Markdown")

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
    
    pending_trades[t_id] = {"sender": u_id, "amount": amt, "type": t_type}
    await state.set_state(BotStates.chatting)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Aceptar", callback_data="accept_trade"), InlineKeyboardButton(text="❌ Rechazar", callback_data="reject_trade")]])
    await send_func(f"⏳ Has propuesto un trade de **{amt}x{amt}** ({t_type}). Esperando...", parse_mode="Markdown")
    await bot.send_message(t_id, f"🤝 **¡Nueva Propuesta!**\nTrade de **{amt}x{amt}** ({t_type}).\n\n¿Aceptas?", reply_markup=markup, parse_mode="Markdown")

@router.callback_query(F.data == "accept_trade")
async def accept_trade(callback: CallbackQuery):
    u_id = callback.from_user.id
    trade = pending_trades.pop(u_id, None)
    if not trade: return
    s_id, amt, t_type = trade["sender"], trade["amount"], trade.get("type", "mixed")
    
    await callback.message.edit_text("✅ Comprobando inventarios...")
    ok_s, files_s = await get_random_batch(db, s_id, u_id, t_type, amt)
    ok_r, files_r = await get_random_batch(db, u_id, s_id, t_type, amt)
    
    if not ok_s or not ok_r:
        await callback.message.edit_text("⚠️ Intercambio cancelado. Uno de los dos no tiene suficientes archivos.")
        return await bot.send_message(s_id, "⚠️ Intercambio cancelado. Uno de los dos no tiene suficientes archivos.")

    await callback.message.edit_text("✅ Procesando envío...")
    await bot.send_message(s_id, "✅ Procesando envío...")
    
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
        
    # --- AQUÍ ESTÁ EL ARREGLO DE REPUTACIÓN AUTOMÁTICA ---
    await db.users.update_one({"_id": u_id}, {"$inc": {"reputation": 1}})
    await db.users.update_one({"_id": s_id}, {"$inc": {"reputation": 1}})
    await check_vip_status(u_id)
    await check_vip_status(s_id)
    
    msg_ok = "🎉 **¡Intercambio finalizado!**\n⭐ *Se sumó +1 punto de reputación a tu perfil.*\n\nGuarda el contenido en Mensajes Guardados."
    await bot.send_message(u_id, msg_ok, parse_mode="Markdown")
    await bot.send_message(s_id, msg_ok, parse_mode="Markdown")
    
    await send_rating_request(u_id, s_id)
    await send_rating_request(s_id, u_id)

@router.callback_query(F.data == "reject_trade")
async def reject_trade(callback: CallbackQuery):
    trade = pending_trades.pop(callback.from_user.id, None)
    if trade: await bot.send_message(trade["sender"], "❌ Propuesta rechazada.")
    await callback.message.edit_text("❌ Rechazado.")

@router.message(StateFilter(BotStates.chatting), ~F.text.in_(["🤝 Proponer Intercambio", "❌ Desconectar"]))
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