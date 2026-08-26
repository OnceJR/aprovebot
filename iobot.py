import os
import asyncio
import logging
import time
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    BotCommand, BotCommandScopeDefault, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient

# --- CONFIGURACIÓN PRINCIPAL ---
TOKEN = os.getenv("BOT_TOKEN", "8758379002:AAHMOIe4-dVfmiW2FzESo-C11q63J0buqIg")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://carlosjrpelegrina_db_user:1DNyN9AFa9bh1tCr@cluster0.haf2f1l.mongodb.net")
BACKUP_CHANNEL_ID = -1004499528343  
FORCE_SUB_CHANNEL_ID = -1004381717458 
FORCE_SUB_CHANNEL_LINK = "https://t.me/+UErsppCsR2Q5MzVh"
VIP_GROUP_ID = -1003581180620 

ADMIN_IDS = [8748956307, 8764734838, 6630522163, 8831263313, 8556221763, 5142196200, 7452819858, 8803304819, 8266066936, 8985586526, 8847243934, 8864888335]

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client.intercambio_bot

active_chats = {}
waiting_list = []
pending_trades = {}
backup_queue = asyncio.Queue()

class BotStates(StatesGroup):
    idle = State()
    searching = State()
    chatting = State()
    waiting_trade_amount = State()
    waiting_for_id = State()

# --- FUNCIONES AUXILIARES ---
async def get_user(user_id):
    user = await db.users.find_one({"_id": user_id})
    if not user:
        user = {"_id": user_id, "lang": "es", "referrals": 0, "reputation": 0, "mode": "anon", "in_vip": False, "last_vip_msg": 0, "notified_vip": False}
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

# --- WEB SERVER & BACKUP ---
async def handle_ping(request): return web.Response(text="Bot is running!")
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()

async def backup_worker():
    while True:
        task = await backup_queue.get()
        try:
            caption = f"👤 Subido por: {task['name']} (`{task['user_id']}`)"
            if task["type"] == "photo": await bot.send_photo(BACKUP_CHANNEL_ID, task["file_id"], caption=caption)
            elif task["type"] == "video": await bot.send_video(BACKUP_CHANNEL_ID, task["file_id"], caption=caption)
            else: await bot.send_document(BACKUP_CHANNEL_ID, task["file_id"], caption=caption)
            await asyncio.sleep(2.5)
        except Exception as e: pass
        finally: backup_queue.task_done()

# --- MONITOR VIP ---
async def vip_monitor():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        async for user in db.users.find({"in_vip": True}):
            if user["_id"] in ADMIN_IDS: continue
            if now - user.get("last_vip_msg", now) > (8 * 3600):
                try:
                    await bot.ban_chat_member(VIP_GROUP_ID, user["_id"])
                    await bot.unban_chat_member(VIP_GROUP_ID, user["_id"])
                    await save_user(user["_id"], {"in_vip": False})
                    lang = user.get("lang", "es")
                    msg = "⚠️ Has sido eliminado del grupo VIP por inactividad (8 horas sin aportar)." if lang == "es" else "⚠️ You have been removed from the VIP group due to inactivity (8 hours without posting)."
                    await bot.send_message(user["_id"], msg)
                except: pass

async def setup_bot_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Menú principal"),
        BotCommand(command="help", description="📖 Guía de uso y ayuda"),
        BotCommand(command="leave", description="❌ Salir del chat actual")
    ], scope=BotCommandScopeDefault())

# --- COMANDOS ---
@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    text = message.text.replace("/broadcast", "").strip()
    if not text: return await message.answer("⚠️ Escribe el mensaje a difundir.")
    
    await message.answer("⏳ Iniciando difusión...")
    count = 0
    async for user in db.users.find():
        try:
            lang = user.get("lang", "es")
            aviso = "📢 **Aviso:**" if lang == "es" else "📢 **Notice:**"
            await bot.send_message(user["_id"], f"{aviso}\n\n{text}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05) 
        except: pass
    await message.answer(f"✅ Difusión completada a `{count}` usuarios.")

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
        txt_res = "🛑 **Acceso Restringido**\nDebes unirte a nuestro canal principal para usar el bot." if lang == "es" else "🛑 **Access Restricted**\nYou must join our main channel to use the bot."
        
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
        err_msg = "⚠️ Aún no te has unido al canal." if lang == "es" else "⚠️ You haven't joined the channel yet."
        await callback.answer(err_msg, show_alert=True)

# --- FUNCIÓN ARREGLADA ---
async def show_main_menu(user_id):
    user = await get_user(user_id)
    lang = user.get("lang", "es")
    
    btn_rnd = "💬 Buscar Chat Aleatorio" if lang == "es" else "💬 Random Chat"
    btn_id = "🆔 Conectar por ID" if lang == "es" else "🆔 Connect via ID"
    btn_prof = "👤 Mi Perfil" if lang == "es" else "👤 My Profile"
    btn_share = "🔗 Compartir mi link" if lang == "es" else "🔗 Share my link"
    
    bot_info = await bot.get_me()
    my_link = f"https://t.me/{bot_info.username}?start={user['_id']}"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_rnd, callback_data="find_chat"), InlineKeyboardButton(text=btn_id, callback_data="connect_id")],
        [InlineKeyboardButton(text=btn_prof, callback_data="my_profile"), InlineKeyboardButton(text="⚙️ Idioma / Language", callback_data="change_lang")],
        [InlineKeyboardButton(text=btn_share, url=f"https://t.me/share/url?url={my_link}")]
    ])
    
    text = "👋 **¡Bienvenido!**\nSube material para guardarlo en tu caja fuerte, o conéctate con alguien para intercambiar." if lang == "es" else "👋 **Welcome!**\nUpload media to save it in your vault, or connect with someone to trade."
    await bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="Markdown")

@router.message(Command("help"))
async def cmd_help(message: Message):
    user = await get_user(message.from_user.id)
    lang = user.get("lang", "es")
    
    if lang == "es":
        help_text = "🤖 **Guía Completa**\n\n📦 **1. Tu Inventario:** Envía archivos a este chat para guardarlos (solo si no estás emparejado).\n⚠️ **Importante:** No elimines los mensajes que envíes. El bot funciona reenviándolos.\n\n💬 **2. Chatear:** Conecta con alguien al azar o por su ID. El material enviado aquí va directo a tu compañero.\n🤝 **3. Lotes:** Usa 'Proponer Intercambio' para mandar ráfagas sin repetidos.\n🌟 **4. VIP:** Gana 15 de reputación o invita 3 amigos para entrar al grupo exclusivo."
    else:
        help_text = "🤖 **Complete Guide**\n\n📦 **1. Inventory:** Send files here to save them (only if you are not in a chat).\n⚠️ **Important:** Do not delete the messages you send. The bot works by forwarding them.\n\n💬 **2. Chatting:** Connect randomly or via ID. Media sent here goes directly to your partner.\n🤝 **3. Batches:** Use 'Propose Trade' to send bursts of unique files.\n🌟 **4. VIP:** Earn 15 rep points or invite 3 friends for VIP access."
        
    await message.answer(help_text, parse_mode="Markdown")

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
        text = f"👤 **Tu Perfil**\n\n🆔 Tu ID de conexión: `{uid}`\n🌟 Reputación: `{user.get('reputation', 0)}` puntos\n👥 Referidos: `{user.get('referrals', 0)}/3`\n🎭 Modo actual: **{modo}**\n\n📦 **Inventario:** 📷 {fotos} | 🎥 {videos}"
    else:
        modo = "🕵️‍♂️ Anonymous" if user.get("mode") == "anon" else "👤 Public"
        text = f"👤 **Your Profile**\n\n🆔 Connection ID: `{uid}`\n🌟 Reputation: `{user.get('reputation', 0)}` pts\n👥 Referrals: `{user.get('referrals', 0)}/3`\n🎭 Current Mode: **{modo}**\n\n📦 **Inventory:** 📷 {fotos} | 🎥 {videos}"
        
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")

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

# --- EMPAREJAMIENTO ---
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
        
    target_id, user_id = int(message.text), message.from_user.id
    if target_id == user_id: return
    
    if target_id in active_chats or target_id in waiting_list: 
        return await message.answer("⚠️ El usuario está ocupado." if lang == "es" else "⚠️ The user is busy.")
        
    fotos = await db.inventory.count_documents({"user_id": user_id, "type": "photo"})
    nombre = ("Un usuario anónimo" if lang == "es" else "An anonymous user") if user.get("mode") == "anon" else message.from_user.full_name
    
    target_user = await get_user(target_id)
    t_lang = target_user.get("lang", "es")
    
    btn_acc = "✅ Aceptar Conexión" if t_lang == "es" else "✅ Accept Connection"
    btn_rej = "❌ Rechazar" if t_lang == "es" else "❌ Reject"
    txt_notif = f"🔔 **Solicitud de Chat**\n{nombre} quiere conectar contigo.\n📊 Stats: 📷 {fotos} | 🌟 Rep: {user.get('reputation',0)}" if t_lang == "es" else f"🔔 **Chat Request**\n{nombre} wants to connect.\n📊 Stats: 📷 {fotos} | 🌟 Rep: {user.get('reputation',0)}"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_acc, callback_data=f"accept_id_{user_id}")],
        [InlineKeyboardButton(text=btn_rej, callback_data=f"reject_id_{user_id}")]
    ])
    
    await bot.send_message(target_id, txt_notif, reply_markup=markup, parse_mode="Markdown")
    await message.answer("⏳ Solicitud enviada. Esperando respuesta..." if lang == "es" else "⏳ Request sent. Waiting for response...")
    await state.set_state(BotStates.idle)

@router.callback_query(F.data.startswith("accept_id_"))
async def accept_id_connection(callback: CallbackQuery, state: FSMContext):
    target_id, user_id = int(callback.data.split("_")[2]), callback.from_user.id
    user = await get_user(user_id)
    t_user = await get_user(target_id)
    
    if target_id in active_chats or user_id in active_chats: 
        return await callback.answer("Alguien ya está ocupado." if user.get("lang", "es") == "es" else "Someone is busy.", show_alert=True)
        
    active_chats[user_id], active_chats[target_id] = target_id, user_id
    await state.set_state(BotStates.chatting)
    await dp.fsm.resolve_context(bot, target_id, target_id).set_state(BotStates.chatting)
    
    for uid, u_obj in [(user_id, user), (target_id, t_user)]:
        lng = u_obj.get("lang", "es")
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"), KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]], resize_keyboard=True)
        msg = "✅ **Conexión Establecida.**" if lng == "es" else "✅ **Connection Established.**"
        await bot.send_message(uid, msg, reply_markup=kb, parse_mode="Markdown")
        
    await callback.message.delete()

@router.callback_query(F.data.startswith("reject_id_"))
async def reject_id_connection(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[2])
    user = await get_user(callback.from_user.id)
    t_user = await get_user(target_id)
    
    msg1 = "❌ Rechazaste la solicitud." if user.get("lang", "es") == "es" else "❌ You rejected the request."
    msg2 = "❌ Tu solicitud fue rechazada o el usuario no está disponible." if t_user.get("lang", "es") == "es" else "❌ Your request was rejected or the user is unavailable."
    
    await callback.message.edit_text(msg1)
    await bot.send_message(target_id, msg2)

@router.callback_query(F.data == "find_chat")
async def find_chat(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    lang = user.get("lang", "es")
    
    if waiting_list:
        target_id = waiting_list.pop(0)
        t_user = await get_user(target_id)
        
        active_chats[user_id], active_chats[target_id] = target_id, user_id
        await state.set_state(BotStates.chatting)
        await dp.fsm.resolve_context(bot, target_id, target_id).set_state(BotStates.chatting)
        
        for uid, u_obj in [(user_id, user), (target_id, t_user)]:
            lng = u_obj.get("lang", "es")
            kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio" if lng == "es" else "🤝 Propose Trade"), KeyboardButton(text="❌ Desconectar" if lng == "es" else "❌ Disconnect")]], resize_keyboard=True)
            msg = "✅ **¡Chat encontrado!**" if lng == "es" else "✅ **Chat found!**"
            await bot.send_message(uid, msg, reply_markup=kb, parse_mode="Markdown")
            
        await callback.message.delete()
    else:
        waiting_list.append(user_id)
        await state.set_state(BotStates.searching)
        txt = "🔍 **Buscando compañero...**" if lang == "es" else "🔍 **Searching for partner...**"
        btn = "❌ Cancelar" if lang == "es" else "❌ Cancel"
        await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn, callback_data="leave_chat")]]), parse_mode="Markdown")

@router.message(F.text.in_(["❌ Desconectar", "❌ Disconnect"]))
@router.message(Command("leave"))
@router.callback_query(F.data == "leave_chat")
async def leave_chat(event, state: FSMContext):
    user_id = event.from_user.id
    user = await get_user(user_id)
    lang = user.get("lang", "es")
    
    if user_id in waiting_list: waiting_list.remove(user_id)
    
    target_id = active_chats.pop(user_id, None)
    if target_id:
        active_chats.pop(target_id, None)
        t_user = await get_user(target_id)
        t_lang = t_user.get("lang", "es")
        
        await dp.fsm.resolve_context(bot, target_id, target_id).set_state(BotStates.idle)
        t_msg = "❌ **El chat finalizó.**" if t_lang == "es" else "❌ **Chat ended.**"
        await bot.send_message(target_id, t_msg, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        await show_main_menu(target_id) # CORREGIDO AQUÍ
        
    await state.set_state(BotStates.idle)
    msg = "Has salido del chat." if lang == "es" else "You left the chat."
    
    if isinstance(event, Message):
        await event.answer(msg, reply_markup=ReplyKeyboardRemove())
    else:
        await event.message.delete()
        await bot.send_message(user_id, msg, reply_markup=ReplyKeyboardRemove())
        
    await show_main_menu(user_id) # CORREGIDO AQUÍ

# --- VIP Y REPUTACIÓN ---
async def check_vip_status(user_id):
    user = await get_user(user_id)
    if user.get("notified_vip"): return
    if user.get("referrals", 0) >= 3 or user.get("reputation", 0) >= 15:
        invite = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
        lang = user.get("lang", "es")
        btn = "🌟 Entrar al VIP" if lang == "es" else "🌟 Join VIP"
        msg = "🎉 **¡Te has ganado acceso al VIP!**\n⚠️ Debes aportar cada 8 horas." if lang == "es" else "🎉 **You've earned VIP access!**\n⚠️ You must post every 8 hours."
        
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn, url=invite.invite_link)]])
        await bot.send_message(user_id, msg, reply_markup=markup, parse_mode="Markdown")
        await save_user(user_id, {"notified_vip": True})

@router.message(F.chat.id == VIP_GROUP_ID, F.photo | F.video | F.document)
async def vip_group_activity(message: Message):
    await save_user(message.from_user.id, {"in_vip": True, "last_vip_msg": time.time()})

async def send_rating_request(user_id, target_id):
    user = await get_user(user_id)
    lang = user.get("lang", "es")
    btn_g = "👍 Bueno" if lang == "es" else "👍 Good"
    btn_b = "👎 Malo" if lang == "es" else "👎 Bad"
    msg = "¿Qué te pareció el contenido y comportamiento de tu compañero?" if lang == "es" else "How was your partner's behavior and content?"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_g, callback_data=f"rate_good_{target_id}"), InlineKeyboardButton(text=btn_b, callback_data=f"rate_bad_{target_id}")]
    ])
    await bot.send_message(user_id, msg, reply_markup=markup)

@router.callback_query(F.data.startswith("rate_"))
async def process_rating(callback: CallbackQuery):
    action, _, target_id = callback.data.split("_")
    user = await get_user(callback.from_user.id)
    if action == "good":
        await db.users.update_one({"_id": int(target_id)}, {"$inc": {"reputation": 1}})
        await check_vip_status(int(target_id))
    msg = "✅ Gracias por tu valoración." if user.get("lang", "es") == "es" else "✅ Thanks for your rating."
    await callback.message.edit_text(msg)

# --- INVENTARIO Y REENVÍO MULTIMEDIA DIRECTO ---
@router.message(F.chat.type == "private", F.photo | F.video | F.document)
async def handle_media(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    lang = user.get("lang", "es")
    
    media = message.photo[-1] if message.photo else (message.video if message.video else message.document)
    file_id, file_unique_id = media.file_id, media.file_unique_id
    media_type = "photo" if message.photo else ("video" if message.video else "document")

    if not await db.global_files.find_one({"_id": file_unique_id}):
        await db.global_files.insert_one({"_id": file_unique_id})
        await backup_queue.put({"file_id": file_id, "type": media_type, "user_id": user_id, "name": message.from_user.full_name})

    if user_id in active_chats:
        target = active_chats[user_id]
        try: await message.forward(target)
        except: pass
        return

    if not await db.inventory.find_one({"user_id": user_id, "file_unique_id": file_unique_id}):
        await db.inventory.insert_one({"user_id": user_id, "file_id": file_id, "message_id": message.message_id, "file_unique_id": file_unique_id, "type": media_type})
        total = await db.inventory.count_documents({"user_id": user_id})
        msg = f"📥 **Archivo guardado en tu inventario.** (Total: {total})\n\n⚠️ **Importante:** El bot funciona reenviando tus archivos originales. Por favor, **no elimines los mensajes que subas a este chat**. Si los borras, se eliminarán automáticamente de tu inventario." if lang == "es" else f"📥 **File saved in your inventory.** (Total: {total})\n\n⚠️ **Important:** The bot works by forwarding your original files. Please, **do not delete the messages you send here**. If you delete them, they will be removed from your inventory."
        await message.answer(msg, parse_mode="Markdown")

# --- INTERCAMBIO Y ESCROW ---
async def get_random_batch(db, sender_id: int, receiver_id: int, category: str, amount: int):
    already_sent_ids = [doc["file_unique_id"] async for doc in db.exchange_history.find({"sender_id": sender_id, "receiver_id": receiver_id}, {"file_unique_id": 1, "_id": 0})]
    pipeline = [{"$match": {"user_id": sender_id, "type": category, "file_unique_id": {"$nin": already_sent_ids}}}, {"$sample": {"size": amount + 15}}]
    selected_files = [doc async for doc in db.inventory.aggregate(pipeline)]
    return len(selected_files) >= amount, selected_files

@router.message(StateFilter(BotStates.chatting), F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade"]))
async def btn_propose(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user.get("lang", "es")
    
    await state.set_state(BotStates.waiting_trade_amount)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10x10", callback_data="trade_10"), InlineKeyboardButton(text="50x50", callback_data="trade_50"), InlineKeyboardButton(text="100x100", callback_data="trade_100")]
    ])
    msg = "🔢 **¿Cuántos archivos deseas intercambiar?**\n\nSelecciona una opción o **escribe un número** en el chat:" if lang == "es" else "🔢 **How many files do you want to trade?**\n\nSelect an option or **type a number** in the chat:"
    await message.answer(msg, reply_markup=markup, parse_mode="Markdown")

@router.message(StateFilter(BotStates.waiting_trade_amount), F.text.regexp(r'^\d+$'))
async def process_manual_trade_offer(message: Message, state: FSMContext):
    await execute_trade_proposal(message.from_user.id, int(message.text), message.answer, state)

@router.callback_query(StateFilter(BotStates.waiting_trade_amount), F.data.startswith("trade_"))
async def process_button_trade_offer(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await execute_trade_proposal(callback.from_user.id, int(callback.data.split("_")[1]), callback.message.answer, state)

async def execute_trade_proposal(user_id, amt, send_func, state):
    target_id = active_chats.get(user_id)
    if not target_id: return await state.set_state(BotStates.idle)
    
    user = await get_user(user_id)
    t_user = await get_user(target_id)
    
    pending_trades[target_id] = {"sender": user_id, "amount": amt}
    await state.set_state(BotStates.chatting)
    
    btn_acc = "✅ Aceptar" if t_user.get("lang", "es") == "es" else "✅ Accept"
    btn_rej = "❌ Rechazar" if t_user.get("lang", "es") == "es" else "❌ Reject"
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_acc, callback_data="accept_trade"), InlineKeyboardButton(text=btn_rej, callback_data="reject_trade")]])
    
    msg_s = f"⏳ Has propuesto un intercambio de **{amt}x{amt}**. Esperando respuesta..." if user.get("lang", "es") == "es" else f"⏳ You proposed a **{amt}x{amt}** trade. Waiting for response..."
    msg_t = f"🤝 **¡Nueva Propuesta!**\nTu compañero propone intercambiar **{amt}x{amt}** archivos.\n\n¿Aceptas?" if t_user.get("lang", "es") == "es" else f"🤝 **New Trade Offer!**\nYour partner wants to trade **{amt}x{amt}** files.\n\nDo you accept?"
    
    await send_func(msg_s, parse_mode="Markdown")
    await bot.send_message(target_id, msg_t, reply_markup=markup, parse_mode="Markdown")

@router.callback_query(F.data == "accept_trade")
async def accept_trade(callback: CallbackQuery):
    user_id = callback.from_user.id
    trade = pending_trades.pop(user_id, None)
    if not trade: return
    
    sender_id, amt = trade["sender"], trade["amount"]
    user = await get_user(user_id)
    s_user = await get_user(sender_id)
    lang, s_lang = user.get("lang", "es"), s_user.get("lang", "es")
    
    await callback.message.edit_text("✅ Comprobando inventarios..." if lang == "es" else "✅ Checking inventories...")
    
    success_sender, files_sender = await get_random_batch(db, sender_id, user_id, "photo", amt)
    success_receiver, files_receiver = await get_random_batch(db, user_id, sender_id, "photo", amt)
    
    if not success_sender or not success_receiver:
        await callback.message.edit_text("⚠️ Intercambio cancelado. Uno de los dos no tiene suficientes archivos NUEVOS." if lang == "es" else "⚠️ Trade canceled. One of you doesn't have enough NEW files.")
        return await bot.send_message(sender_id, "⚠️ Intercambio cancelado. Uno de los dos no tiene suficientes archivos NUEVOS." if s_lang == "es" else "⚠️ Trade canceled. One of you doesn't have enough NEW files.")

    await callback.message.edit_text("✅ Aceptado. Procesando envío..." if lang == "es" else "✅ Accepted. Processing batch...")
    await bot.send_message(sender_id, "✅ Aceptado. Procesando envío..." if s_lang == "es" else "✅ Accepted. Processing batch...")
    
    sent_1 = 0
    for f in files_sender:
        if sent_1 >= amt: break
        try:
            await bot.forward_message(chat_id=user_id, from_chat_id=sender_id, message_id=f["message_id"])
            await db.exchange_history.insert_one({"sender_id": sender_id, "receiver_id": user_id, "file_unique_id": f["file_unique_id"]})
            sent_1 += 1
        except Exception: await db.inventory.delete_one({"file_unique_id": f["file_unique_id"]})
        await asyncio.sleep(0.05)
        
    sent_2 = 0
    for f in files_receiver:
        if sent_2 >= amt: break
        try:
            await bot.forward_message(chat_id=sender_id, from_chat_id=user_id, message_id=f["message_id"])
            await db.exchange_history.insert_one({"sender_id": user_id, "receiver_id": sender_id, "file_unique_id": f["file_unique_id"]})
            sent_2 += 1
        except Exception: await db.inventory.delete_one({"file_unique_id": f["file_unique_id"]})
        await asyncio.sleep(0.05)
        
    msg_ok = f"🎉 **¡Intercambio finalizado!**\n\n💡 **Sugerencia de seguridad:** Para no perder este contenido, guárdalo en tus **Mensajes Guardados**." if lang == "es" else f"🎉 **Trade completed!**\n\n💡 **Security tip:** Save this content in your **Saved Messages** so you don't lose it."
    s_msg_ok = f"🎉 **¡Intercambio finalizado!**\n\n💡 **Sugerencia de seguridad:** Para no perder este contenido, guárdalo en tus **Mensajes Guardados**." if s_lang == "es" else f"🎉 **Trade completed!**\n\n💡 **Security tip:** Save this content in your **Saved Messages** so you don't lose it."
    
    await bot.send_message(user_id, msg_ok, parse_mode="Markdown")
    await bot.send_message(sender_id, s_msg_ok, parse_mode="Markdown")
    
    await send_rating_request(user_id, sender_id)
    await send_rating_request(sender_id, user_id)

@router.callback_query(F.data == "reject_trade")
async def reject_trade(callback: CallbackQuery):
    user_id = callback.from_user.id
    trade = pending_trades.pop(user_id, None)
    user = await get_user(user_id)
    
    if not trade: 
        return await callback.message.answer("⚠️ No hay intercambios pendientes." if user.get("lang", "es") == "es" else "⚠️ No pending trades.", show_alert=True)
    
    sender_id = trade["sender"]
    s_user = await get_user(sender_id)
    
    msg_r = "❌ Has rechazado la propuesta de intercambio." if user.get("lang", "es") == "es" else "❌ You rejected the trade offer."
    s_msg_r = "❌ Tu compañero ha rechazado la propuesta de intercambio." if s_user.get("lang", "es") == "es" else "❌ Your partner rejected the trade offer."
    
    await callback.message.edit_text(msg_r)
    await bot.send_message(sender_id, s_msg_r)

@router.message(StateFilter(BotStates.chatting), ~F.text.in_(["🤝 Proponer Intercambio", "🤝 Propose Trade", "❌ Desconectar", "❌ Disconnect"]))
async def relay_msg(message: Message):
    target = active_chats.get(message.from_user.id)
    if target:
        try: await message.forward(target)
        except: pass

async def main():
    dp.include_router(router)
    await setup_bot_commands(bot)
    await start_web_server()
    asyncio.create_task(backup_worker())
    asyncio.create_task(vip_monitor())
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())