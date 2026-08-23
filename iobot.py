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
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)
from aiohttp import web

# Configuración básica
TOKEN = "8930108804:AAFakVeuGHUVnbsB9pv0jbRKON4pPkzsJQE"
BACKUP_CHANNEL_ID = -1004465910047
SUPER_ADMIN_ID = 8983189714

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# Base de datos simulada y Colas
db = {
    "users": {},          
    "active_chats": {},   # id_usuario_A : id_usuario_B
    "global_files": set() 
}

waiting_list = [] # Usuarios buscando chat aleatorio
last_notified = {}
backup_queue = asyncio.Queue() # Sistema Anti-Colapso para el canal

class ChatStates(StatesGroup):
    idle = State()
    searching = State()
    chatting = State()
    waiting_trade = State()

# --- UPTIMEROBOT ---
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080)) 
    site = web.TCPSite(runner, "0.0.0.0", port) 
    await site.start()

# --- TRABAJADOR DE RESPALDO (Sube al canal sin colapsar) ---
async def backup_worker():
    while True:
        task = await backup_queue.get()
        file_id = task["file_id"]
        media_type = task["type"]
        user = task["user"]
        
        # Crear la etiqueta con los datos del usuario
        username_text = f" (@{user.username})" if user.username else ""
        caption = f"👤 Subido por: {user.full_name}{username_text}\n🆔 ID: `{user.id}`"

        try:
            if media_type == "photo":
                await bot.send_photo(chat_id=BACKUP_CHANNEL_ID, photo=file_id, caption=caption)
            elif media_type == "video":
                await bot.send_video(chat_id=BACKUP_CHANNEL_ID, video=file_id, caption=caption)
            elif media_type == "document":
                await bot.send_document(chat_id=BACKUP_CHANNEL_ID, document=file_id, caption=caption)
            
            # Pausa de 2.5 segundos (Evita el FloodWait de Telegram)
            await asyncio.sleep(2.5) 
        except Exception as e:
            logging.error(f"Error en backup_worker: {e}")
        finally:
            backup_queue.task_done()

async def setup_bot_commands(bot: Bot):
    user_commands = [
        BotCommand(command="start", description="Menú principal"),
        BotCommand(command="help", description="Ayuda y guía de uso"),
        BotCommand(command="leave", description="Salir del chat actual")
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    admin_commands = user_commands + [
        BotCommand(command="admin", description="👑 Panel Admin"),
        BotCommand(command="broadcast", description="📢 Mensaje global")
    ]
    try:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=SUPER_ADMIN_ID))
    except:
        pass

# --- INICIO Y REFERIDOS ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1) 

    if user_id not in db["users"]:
        db["users"][user_id] = {"lang": "es", "inventory": []}

    await state.set_state(ChatStates.idle)

    # Lógica de enlace de referidos
    if len(args) > 1:
        target_id = int(args[1])
        if target_id in db["users"] and target_id != user_id:
            if target_id not in db["active_chats"]:
                # Conectar a ambos
                db["active_chats"][user_id] = target_id
                db["active_chats"][target_id] = user_id
                
                # Obtener el estado del otro usuario y cambiarlo a chatting
                target_state = dp.fsm.resolve_context(bot, target_id, target_id)
                await target_state.set_state(ChatStates.chatting)
                await state.set_state(ChatStates.chatting)

                await bot.send_message(target_id, f"⚡️ ¡Alguien ha entrado con tu link! Están conectados.\nEscribe algo para saludar. Usa /leave para salir.")
                await message.answer("⚡️ ¡Te has conectado directamente mediante el enlace!\nYa puedes escribirle. Usa /leave para salir.")
                return
            else:
                await message.answer("⚠️ El usuario dueño de este enlace está ocupado chateando con otra persona ahora mismo.")

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Buscar Chat Aleatorio", callback_data="find_chat")],
        [InlineKeyboardButton(text="👤 Mi Perfil / Inventario", callback_data="my_profile")]
    ])
    
    await message.answer("👋 **¡Bienvenido al Bot de Intercambio!**\n\nSube fotos o videos para nutrir tu inventario o busca alguien con quien intercambiar.", reply_markup=markup, parse_mode="Markdown")

# --- PERFIL ---
@router.callback_query(F.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    inventory = db["users"][user_id]["inventory"]
    
    fotos = sum(1 for item in inventory if item["type"] == "photo")
    videos = sum(1 for item in inventory if item["type"] == "video")
    archivos = sum(1 for item in inventory if item["type"] == "document")
    
    bot_info = await bot.get_me()
    my_link = f"https://t.me/{bot_info.username}?start={user_id}"

    text = (f"👤 **Tu Perfil**\n\n"
            f"📷 Fotos: `{fotos}`\n"
            f"🎥 Videos: `{videos}`\n"
            f"📁 Archivos: `{archivos}`\n\n"
            f"🔗 **Tu enlace para invitar y chatear directo:**\n`{my_link}`")
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Volver", callback_data="back_main")]]))
    await callback.answer()

@router.callback_query(F.data == "back_main")
async def back_to_main(callback: CallbackQuery):
    await cmd_start(callback.message, dp.fsm.resolve_context(bot, callback.from_user.id, callback.from_user.id))
    await callback.message.delete()

# --- LÓGICA DE EMPAREJAMIENTO (MATCHMAKING) ---
@router.callback_query(F.data == "find_chat")
async def find_chat(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    if user_id in waiting_list:
        await callback.answer("⏳ Ya estás buscando un chat...", show_alert=True)
        return

    if waiting_list:
        # Hay alguien esperando, los conectamos
        target_id = waiting_list.pop(0)
        
        db["active_chats"][user_id] = target_id
        db["active_chats"][target_id] = user_id
        
        await state.set_state(ChatStates.chatting)
        target_state = dp.fsm.resolve_context(bot, target_id, target_id)
        await target_state.set_state(ChatStates.chatting)

        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Desconectar", callback_data="leave_chat")]])
        
        await bot.send_message(target_id, "✅ **¡Chat encontrado!** Estás conectado de forma anónima. Escribe algo:", reply_markup=markup, parse_mode="Markdown")
        await callback.message.edit_text("✅ **¡Chat encontrado!** Estás conectado de forma anónima. Escribe algo:", reply_markup=markup, parse_mode="Markdown")
    else:
        # No hay nadie, lo ponemos en la lista de espera
        waiting_list.append(user_id)
        await state.set_state(ChatStates.searching)
        await callback.message.edit_text("🔍 **Buscando compañero...**\nPor favor espera, te avisaré en cuanto alguien se conecte.", parse_mode="Markdown")
    
    await callback.answer()

# --- SALIR DEL CHAT ---
@router.message(Command("leave"))
@router.callback_query(F.data == "leave_chat")
async def leave_chat(event, state: FSMContext):
    user_id = event.from_user.id
    
    if user_id in waiting_list:
        waiting_list.remove(user_id)
        await state.set_state(ChatStates.idle)
        msg = "❌ Búsqueda cancelada."
    elif user_id in db["active_chats"]:
        target_id = db["active_chats"].pop(user_id)
        db["active_chats"].pop(target_id, None)
        
        await state.set_state(ChatStates.idle)
        target_state = dp.fsm.resolve_context(bot, target_id, target_id)
        await target_state.set_state(ChatStates.idle)
        
        await bot.send_message(target_id, "❌ **Tu compañero ha abandonado el chat.** Usa /start para volver al menú.", parse_mode="Markdown")
        msg = "❌ **Has salido del chat.** Usa /start para volver al menú."
    else:
        msg = "No estás en ningún chat."

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(msg, parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(msg, parse_mode="Markdown")

# --- REPETIDOR DE CHAT (INTERCAMBIO DE MENSAJES NORMALES) ---
@router.message(StateFilter(ChatStates.chatting), F.text | F.sticker | F.animation | F.voice)
async def relay_message(message: Message):
    user_id = message.from_user.id
    target_id = db["active_chats"].get(user_id)
    
    if target_id:
        try:
            await message.send_copy(chat_id=target_id)
        except Exception:
            await message.answer("⚠️ No se pudo enviar el mensaje a tu compañero.")

# --- MANEJO DE ARCHIVOS Y COLA DE CANAL ---
@router.message(F.photo | F.video | F.document)
async def handle_media_upload(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in db["users"]:
        db["users"][user_id] = {"lang": "es", "inventory": []}

    if message.photo:
        file_id = message.photo[-1].file_id
        file_unique_id = message.photo[-1].file_unique_id
        media_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_unique_id = message.video.file_unique_id
        media_type = "video"
    else:
        file_id = message.document.file_id
        file_unique_id = message.document.file_unique_id
        media_type = "document"

    user_inventory = db["users"][user_id]["inventory"]

    if any(item["file_unique_id"] == file_unique_id for item in user_inventory):
        return # Ignorar duplicados del propio usuario

    # Guardar en inventario personal
    user_inventory.append({"file_id": file_id, "file_unique_id": file_unique_id, "type": media_type})

    # ENVIAR A LA COLA DEL CANAL (Solo si es nuevo globalmente)
    if file_unique_id not in db["global_files"]:
        db["global_files"].add(file_unique_id)
        await backup_queue.put({
            "file_id": file_id,
            "type": media_type,
            "user": message.from_user
        })

    # Notificación Anti-Spam
    current_time = time.time()
    if current_time - last_notified.get(user_id, 0) > 3.0:
        await message.answer(f"📥 Recibiendo archivos y asegurándolos... Tienes un total de {len(user_inventory)} archivos listos.")
        last_notified[user_id] = current_time

    # Si está chateando con alguien, enviar copia al compañero!
    target_id = db["active_chats"].get(user_id)
    if target_id:
        await message.send_copy(chat_id=target_id)

# --- PANEL ADMIN (Ocultos los comandos en el código anterior por brevedad, mantenlos igual) ---
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id == SUPER_ADMIN_ID:
        total_users = len(db["users"])
        total_files = sum(len(u["inventory"]) for u in db["users"].values())
        await message.answer(f"👑 **Admin Panel**\n👥 Usuarios: {total_users}\n📁 Archivos: {total_files}", parse_mode="Markdown")

# --- ARRANQUE ---
async def main():
    dp.include_router(router)
    await setup_bot_commands(bot)
    
    # Iniciar el servidor web, el trabajador de backups en segundo plano y el bot
    await start_web_server()
    asyncio.create_task(backup_worker())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())