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

# Inmunidad administrativa
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
        except Exception as e: logging.error(f"Error Backup: {e}")
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
                    await bot.send_message(user["_id"], "⚠️ Has sido eliminado del grupo VIP por inactividad (8 horas sin aportar).")
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
            await bot.send_message(user["_id"], f"📢 **Aviso:**\n\n{text}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05) 
        except: pass
    await message.answer(f"✅ Difusión completada a `{count}` usuarios.")

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
        return await message.answer("🛑 **Acceso Restringido**\nDebes unirte a nuestro canal principal para usar el bot.", reply_markup=markup, parse_mode="Markdown")

    await show_main_menu(message.answer, user)
    await state.set_state(BotStates.idle)

@router.callback_query(F.data == "verify_sub")
async def verify_sub(callback: CallbackQuery):
    if await check_force_sub(callback.from_user.id):
        await callback.message.delete()
        await show_main_menu(callback.message.answer, await get_user(callback.from_user.id))
    else:
        await callback.answer("⚠️ Aún no te has unido al canal.", show_alert=True)

async def show_main_menu(send_func, user):
    lang = user.get("lang", "es")
    btn_share = "🔗 Compartir mi link" if lang == "es" else "🔗 Share my link"
    bot_info = await bot.get_me()
    my_link = f"https://t.me/{bot_info.username}?start={user['_id']}"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Buscar Chat Aleatorio", callback_data="find_chat"),
         InlineKeyboardButton(text="🆔 Conectar por ID", callback_data="connect_id")],
        [InlineKeyboardButton(text="👤 Mi Perfil", callback_data="my_profile"),
         InlineKeyboardButton(text="⚙️ Idioma / Language", callback_data="change_lang")],
        [InlineKeyboardButton(text=btn_share, url=f"https://t.me/share/url?url={my_link}")]
    ])
    text = "👋 **¡Bienvenido!**\nSube material para guardarlo en tu caja fuerte, o conéctate con alguien para intercambiar." if lang == "es" else "👋 **Welcome!**\nUpload media to save it, or connect to trade."
    await send_func(text, reply_markup=markup, parse_mode="Markdown")

@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 **Guía Completa del Bot de Intercambio**\n\n"
        "📦 **1. Tu Inventario:** Envía archivos a este chat para guardarlos (solo si no estás emparejado).\n"
        "⚠️ **Importante:** No elimines los mensajes que envíes. El bot funciona reenviándolos.\n\n"
        "💬 **2. Chatear:** Conecta con alguien al azar o por su ID. El material enviado aquí va directo a tu compañero.\n"
        "🤝 **3. Lotes:** Usa 'Proponer Intercambio' para mandar ráfagas automáticas sin repetidos.\n"
        "🌟 **4. VIP:** Gana 15 de reputación o invita 3 amigos para entrar al grupo exclusivo."
    )
    await message.answer(help_text, parse_mode="Markdown")

# --- PERFIL E IDIOMA ---
@router.callback_query(F.data == "change_lang")
async def change_lang(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await save_user(callback.from_user.id, {"lang": "en" if user.get("lang") == "es" else "es"})
    await callback.answer("✅ Idioma actualizado")
    await callback.message.delete()
    await show_main_menu(callback.message.answer, await get_user(callback.from_user.id))

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
    text = (f"👤 **Tu Perfil**\n\n🆔 Tu ID de conexión: `{uid}`\n🌟 Reputación: `{user.get('reputation', 0)}` puntos\n"
            f"👥 Referidos: `{user.get('referrals', 0)}/3`\n🎭 Modo actual: **{modo}**\n\n📦 **Inventario:** 📷 {fotos} | 🎥 {videos}")
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
    await show_main_menu(callback.message.answer, await get_user(callback.from_user.id))

# --- EMPAREJAMIENTO ---
@router.callback_query(F.data == "connect_id")
async def ask_for_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_id)
    await callback.message.edit_text("✏️ Escribe el **ID numérico** del usuario:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Cancelar", callback_data="back_main")]]))

@router.message(StateFilter(BotStates.waiting_for_id))
async def process_connect_id(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("⚠️ Debe ser un número.")
    target_id, user_id = int(message.text), message.from_user.id
    
    if target_id == user_id: return
    if target_id in active_chats or target_id in waiting_list: return await message.answer("⚠️ El usuario está ocupado.")
        
    sender_user = await get_user(user_id)
    fotos = await db.inventory.count_documents({"user_id": user_id, "type": "photo"})
    nombre = "Un usuario anónimo" if sender_user.get("mode") == "anon" else message.from_user.full_name
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Aceptar Conexión", callback_data=f"accept_id_{user_id}")],
        [InlineKeyboardButton(text="❌ Rechazar", callback_data=f"reject_id_{user_id}")]
    ])
    await bot.send_message(target_id, f"🔔 **Solicitud de Chat**\n{nombre} quiere conectar contigo.\n📊 Stats: 📷 {fotos} fotos | 🌟 Rep: {sender_user.get('reputation',0)}", reply_markup=markup, parse_mode="Markdown")
    await message.answer("⏳ Solicitud enviada. Esperando respuesta...")
    await state.set_state(BotStates.idle)

@router.callback_query(F.data.startswith("accept_id_"))
async def accept_id_connection(callback: CallbackQuery, state: FSMContext):
    target_id, user_id = int(callback.data.split("_")[2]), callback.from_user.id
    if target_id in active_chats or user_id in active_chats: return await callback.answer("Alguien ya está ocupado.", show_alert=True)
        
    active_chats[user_id], active_chats[target_id] = target_id, user_id
    await state.set_state(BotStates.chatting)
    await dp.fsm.resolve_context(bot, target_id, target_id).set_state(BotStates.chatting)
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio"), KeyboardButton(text="❌ Desconectar")]], resize_keyboard=True)
    await bot.send_message(target_id, "✅ **Conexión Establecida.**", reply_markup=kb, parse_mode="Markdown")
    await callback.message.delete()
    await callback.message.answer("✅ **Conexión Establecida.**", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("reject_id_"))
async def reject_id_connection(callback: CallbackQuery):
    await callback.message.edit_text("❌ Rechazaste la solicitud.")
    await bot.send_message(int(callback.data.split("_")[2]), "❌ Tu solicitud fue rechazada o el usuario no está disponible.")

@router.callback_query(F.data == "find_chat")
async def find_chat(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if waiting_list:
        target_id = waiting_list.pop(0)
        active_chats[user_id], active_chats[target_id] = target_id, user_id
        await state.set_state(BotStates.chatting)
        await dp.fsm.resolve_context(bot, target_id, target_id).set_state(BotStates.chatting)
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🤝 Proponer Intercambio"), KeyboardButton(text="❌ Desconectar")]], resize_keyboard=True)
        await bot.send_message(target_id, "✅ **¡Chat encontrado!**", reply_markup=kb, parse_mode="Markdown")
        await callback.message.delete()
        await callback.message.answer("✅ **¡Chat encontrado!**", reply_markup=kb, parse_mode="Markdown")
    else:
        waiting_list.append(user_id)
        await state.set_state(BotStates.searching)
        await callback.message.edit_text("🔍 **Buscando compañero...**", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancelar", callback_data="leave_chat")]]))

@router.message(F.text == "❌ Desconectar")
@router.message(Command("leave"))
@router.callback_query(F.data == "leave_chat")
async def leave_chat(event, state: FSMContext):
    user_id = event.from_user.id
    if user_id in waiting_list: waiting_list.remove(user_id)
    
    target_id = active_chats.pop(user_id, None)
    if target_id:
        active_chats.pop(target_id, None)
        await dp.fsm.resolve_context(bot, target_id, target_id).set_state(BotStates.idle)
        await bot.send_message(target_id, "❌ **El chat finalizó.**", reply_markup=ReplyKeyboardRemove())
        await show_main_menu(bot.send_message, await get_user(target_id))
        
    await state.set_state(BotStates.idle)
    if isinstance(event, Message):
        await event.answer("Has salido del chat.", reply_markup=ReplyKeyboardRemove())
        await show_main_menu(event.answer, await get_user(user_id))
    else:
        await event.message.delete()
        await show_main_menu(event.message.answer, await get_user(user_id))

# --- VIP Y REPUTACIÓN ---
async def check_vip_status(user_id):
    user = await get_user(user_id)
    if user.get("notified_vip"): return
    if user.get("referrals", 0) >= 3 or user.get("reputation", 0) >= 15:
        invite = await bot.create_chat_invite_link(chat_id=VIP_GROUP_ID, member_limit=1)
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌟 Entrar al VIP", url=invite.invite_link)]])
        await bot.send_message(user_id, "🎉 **¡Te has ganado acceso al VIP!**\n⚠️ Debes aportar cada 8 horas.", reply_markup=markup, parse_mode="Markdown")
        await save_user(user_id, {"notified_vip": True})

@router.message(F.chat.id == VIP_GROUP_ID, F.photo | F.video | F.document)
async def vip_group_activity(message: Message):
    await save_user(message.from_user.id, {"in_vip": True, "last_vip_msg": time.time()})

async def send_rating_request(user_id, target_id):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 Bueno", callback_data=f"rate_good_{target_id}"), InlineKeyboardButton(text="👎 Malo", callback_data=f"rate_bad_{target_id}")]
    ])
    await bot.send_message(user_id, "¿Qué te pareció el contenido y comportamiento de tu compañero?", reply_markup=markup)

@router.callback_query(F.data.startswith("rate_"))
async def process_rating(callback: CallbackQuery):
    action, _, target_id = callback.data.split("_")
    if action == "good":
        await db.users.update_one({"_id": int(target_id)}, {"$inc": {"reputation": 1}})
        await check_vip_status(int(target_id))
    await callback.message.edit_text("✅ Gracias por tu valoración.")

# --- INVENTARIO Y REENVÍO MULTIMEDIA DIRECTO ---
@router.message(F.chat.type == "private", F.photo | F.video | F.document)
async def handle_media(message: Message):
    user_id = message.from_user.id
    media = message.photo[-1] if message.photo else (message.video if message.video else message.document)
    file_id, file_unique_id = media.file_id, media.file_unique_id
    media_type = "photo" if message.photo else ("video" if message.video else "document")

    # 1. Respaldo (Solo sube al canal si es un archivo no repetido a nivel global)
    if not await db.global_files.find_one({"_id": file_unique_id}):
        await db.global_files.insert_one({"_id": file_unique_id})
        await backup_queue.put({"file_id": file_id, "type": media_type, "user_id": user_id, "name": message.from_user.full_name})

    # 2. Comportamiento según el estado del chat
    if user_id in active_chats:
        # Si está chateando, lo reenvía directo y finaliza (no se guarda en inventario)
        target = active_chats[user_id]
        try: await message.forward(target)
        except: pass
        return

    # 3. Si NO está chateando, se guarda en el inventario personal
    if not await db.inventory.find_one({"user_id": user_id, "file_unique_id": file_unique_id}):
        await db.inventory.insert_one({"user_id": user_id, "file_id": file_id, "message_id": message.message_id, "file_unique_id": file_unique_id, "type": media_type})
        total = await db.inventory.count_documents({"user_id": user_id})
        await message.answer(f"📥 **Archivo guardado en tu inventario.** (Total: {total})\n\n⚠️ **Importante:** El bot funciona reenviando tus archivos originales. Por favor, **no elimines los mensajes que subas a este chat**. Si los borras, se eliminarán automáticamente de tu inventario.", parse_mode="Markdown")

# --- INTERCAMBIO Y ESCROW ---
async def get_random_batch(db, sender_id: int, receiver_id: int, category: str, amount: int):
    already_sent_ids = [doc["file_unique_id"] async for doc in db.exchange_history.find({"sender_id": sender_id, "receiver_id": receiver_id}, {"file_unique_id": 1, "_id": 0})]
    
    # Buscamos un colchón adicional (+15) por si el usuario borró mensajes
    pipeline = [
        {"$match": {"user_id": sender_id, "type": category, "file_unique_id": {"$nin": already_sent_ids}}},
        {"$sample": {"size": amount + 15}}
    ]
    selected_files = [doc async for doc in db.inventory.aggregate(pipeline)]
    return len(selected_files) >= amount, selected_files

@router.message(StateFilter(BotStates.chatting), F.text == "🤝 Proponer Intercambio")
async def btn_propose(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_trade_amount)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10x10", callback_data="trade_10"), InlineKeyboardButton(text="50x50", callback_data="trade_50"), InlineKeyboardButton(text="100x100", callback_data="trade_100")]
    ])
    await message.answer("🔢 **¿Cuántos archivos deseas intercambiar?**\n\nSelecciona una opción o **escribe un número** en el chat:", reply_markup=markup, parse_mode="Markdown")

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
    
    pending_trades[target_id] = {"sender": user_id, "amount": amt}
    await state.set_state(BotStates.chatting)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Aceptar", callback_data="accept_trade"), InlineKeyboardButton(text="❌ Rechazar", callback_data="reject_trade")]
    ])
    await send_func(f"⏳ Has propuesto un intercambio de **{amt}x{amt}**. Esperando respuesta...", parse_mode="Markdown")
    await bot.send_message(target_id, f"🤝 **¡Nueva Propuesta!**\nTu compañero propone intercambiar **{amt}x{amt}** archivos.\n\n¿Aceptas?", reply_markup=markup, parse_mode="Markdown")

@router.callback_query(F.data == "accept_trade")
async def accept_trade(callback: CallbackQuery):
    user_id = callback.from_user.id
    trade = pending_trades.pop(user_id, None)
    if not trade: return
    
    sender_id, amt = trade["sender"], trade["amount"]
    await callback.message.edit_text("✅ Comprobando inventarios...")
    
    success_sender, files_sender = await get_random_batch(db, sender_id, user_id, "photo", amt)
    success_receiver, files_receiver = await get_random_batch(db, user_id, sender_id, "photo", amt)
    
    if not success_sender or not success_receiver:
        await callback.message.edit_text("⚠️ Intercambio cancelado. Uno de los dos no tiene suficientes archivos NUEVOS en su inventario.")
        return await bot.send_message(sender_id, "⚠️ Intercambio cancelado. Uno de los dos no tiene suficientes archivos NUEVOS.")

    await callback.message.edit_text("✅ Aceptado. Procesando envío masivo...")
    await bot.send_message(sender_id, "✅ Aceptado. Procesando envío masivo...")
    
    # Procesar envíos con Auto-Limpieza en caso de archivos borrados
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
        
    success_text = (f"🎉 **¡Intercambio finalizado con éxito!**\n\n"
                    f"💡 **Sugerencia de seguridad:** Para no perder este contenido, te recomendamos seleccionarlo y reenviarlo a tus **Mensajes Guardados** de Telegram.")
    
    await bot.send_message(user_id, success_text, parse_mode="Markdown")
    await bot.send_message(sender_id, success_text, parse_mode="Markdown")
    
    await send_rating_request(user_id, sender_id)
    await send_rating_request(sender_id, user_id)

@router.callback_query(F.data == "reject_trade")
async def reject_trade(callback: CallbackQuery):
    user_id = callback.from_user.id
    trade = pending_trades.pop(user_id, None)
    
    if not trade: 
        return await callback.message.answer("⚠️ No hay intercambios pendientes.", show_alert=True)
    
    sender_id = trade["sender"]
    await callback.message.edit_text("❌ Has rechazado la propuesta de intercambio.")
    await bot.send_message(sender_id, "❌ Tu compañero ha rechazado la propuesta de intercambio.")

@router.message(StateFilter(BotStates.chatting), ~F.text.in_(["🤝 Proponer Intercambio", "❌ Desconectar"]))
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