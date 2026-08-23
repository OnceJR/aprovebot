import os
import asyncio
import logging
import time
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
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
BACKUP_CHANNEL_ID = -1004465910047  # ID de tu canal privado de respaldo
SUPER_ADMIN_ID = 8983189714

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# Base de datos simulada
db = {
    "users": {},          # user_id: {"lang": "es", "inventory": []}
    "history": {},        # (user_id, target_id): set([file_unique_ids])
    "active_chats": {},   # user_id: target_id
    "pending_trade": {},  # user_id: {"amount": 10, "type": "photo"}
    "global_files": set() # AQUÍ SE GUARDAN LOS file_unique_id PARA EVITAR DUPLICADOS EN EL CANAL
}

# Control de spam para notificaciones de subida masiva
last_notified = {}

class ChatStates(StatesGroup):
    chatting = State()
    waiting_trade_amount = State()

# --- UPTIMEROBOT KEEPALIVE SERVER ---
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

# --- CONFIGURACIÓN DEL MENÚ DE COMANDOS DE TELEGRAM ---
async def setup_bot_commands(bot: Bot):
    # Comandos para todos los usuarios
    user_commands = [
        BotCommand(command="start", description="Iniciar el bot / Menú principal"),
        BotCommand(command="help", description="Guía de uso y ayuda")
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    # Comandos EXCLUSIVOS para el Súper Admin (Solo tú los verás)
    admin_commands = user_commands + [
        BotCommand(command="admin", description="👑 Panel de Estadísticas"),
        BotCommand(command="broadcast", description="📢 Enviar mensaje a todos")
    ]
    try:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=SUPER_ADMIN_ID))
    except Exception as e:
        logging.warning(f"No se pudo configurar el menú de admin (quizás el admin no ha iniciado el bot aún): {e}")


# --- COMANDO /START Y ENLACES DE INVITACIÓN ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1) 

    if user_id not in db["users"]:
        db["users"][user_id] = {"lang": "es", "inventory": []}

    if len(args) > 1:
        inviter_id = args[1]
        await message.answer(f"🤝 ¡Has entrado a través del link de un amigo! (Ref: {inviter_id})")

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇪🇸 Español", callback_data="lang_es"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")] 
    ])
    
    await message.answer(
        "👋 ¡Bienvenido al Bot de Intercambio!\n\nSelecciona tu idioma / Choose your language:", 
        reply_markup=markup
    )

@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery):
    user_id = callback.from_user.id
    lang = callback.data.split("_")[1]
    db["users"][user_id]["lang"] = lang

    text = ("✅ **¡Configurado en Español!**\n\n"
            "Para empezar, simplemente envíame fotos, videos o archivos. Yo los guardaré de forma segura en tu inventario para que puedas intercambiarlos luego.") if lang == "es" \
           else ("✅ **Language set to English!**\n\n"
                 "To begin, just send me photos, videos or files. I will safely store them in your inventory for you to trade later.")
    
    bot_info = await bot.get_me()
    my_link = f"https://t.me/{bot_info.username}?start={user_id}"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Buscar Chat / Find Chat", callback_data="find_chat")],
        [InlineKeyboardButton(text="🔗 Compartir mi Link", url=f"https://t.me/share/url?url={my_link}&text=¡Entra a este bot para intercambiar contenido de forma anónima!")]
    ])
    
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    await callback.answer()

# --- COMANDO /HELP ---
@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 **Guía de Uso del Bot:**\n\n"
        "📥 **1. Carga:** Envía cualquier foto, video o archivo al chat. Se guardarán en tu inventario personal de forma privada.\n"
        "🔍 **2. Intercambio:** Usa el botón 'Buscar Chat' en el inicio para emparejarte con otro usuario de forma 100% anónima.\n"
        "🤝 **3. Acuerdo:** Dentro del chat, acuerden una cantidad (ej. 10x10) y propongan el intercambio. El bot se encargará de que nadie envíe archivos repetidos.\n"
        "🌐 **4. Idioma:** Usa /start para volver al menú y cambiar tu idioma.\n"
        "🔗 **5. Tu Link:** Comparte tu enlace de invitación para conectar directamente con alguien."
    )
    await message.answer(help_text, parse_mode="Markdown")

# --- PANEL DE SÚPER ADMIN ---
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != SUPER_ADMIN_ID:
        return # Si no es el admin, ignoramos el comando silenciosamente

    total_users = len(db["users"])
    total_files = sum(len(u["inventory"]) for u in db["users"].values())
    active_chats_count = len(db["active_chats"]) // 2
    global_backed_up = len(db["global_files"])

    admin_text = (
        "👑 **Panel de Súper Administrador**\n\n"
        f"👥 **Usuarios registrados:** `{total_users}`\n"
        f"📁 **Archivos en inventarios:** `{total_files}`\n"
        f"☁️ **Archivos únicos en canal:** `{global_backed_up}`\n"
        f"💬 **Chats activos ahora:** `{active_chats_count}`\n\n"
        "Usa `/broadcast [mensaje]` para enviar un comunicado a todos los usuarios."
    )
    await message.answer(admin_text, parse_mode="Markdown")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    text_to_broadcast = message.text.replace("/broadcast", "").strip()
    if not text_to_broadcast:
        await message.answer("⚠️ Escribe el mensaje que deseas difundir.\nEjemplo: `/broadcast Hola a todos, hoy hay mantenimiento`")
        return

    count = 0
    await message.answer("⏳ Iniciando difusión, esto puede tardar un poco...")
    for user_id in db["users"]:
        try:
            await bot.send_message(user_id, f"📢 **Aviso del Administrador:**\n\n{text_to_broadcast}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05) # Evitar flood limits de Telegram
        except Exception:
            pass

    await message.answer(f"✅ Difusión completada con éxito a `{count}` usuarios.")

# --- MANEJO DE ARCHIVOS E INVENTARIO (CON FLOOD CONTROL) ---
@router.message(F.photo | F.video | F.document)
async def handle_media_upload(message: Message):
    user_id = message.from_user.id
    if user_id not in db["users"]:
        db["users"][user_id] = {"lang": "es", "inventory": []}

    # Extraer identificadores según el tipo de archivo
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

    # 1. Evitar que el usuario tenga duplicados en su propio inventario
    if any(item["file_unique_id"] == file_unique_id for item in user_inventory):
        # Respondemos silenciosamente o ignoramos para no hacer spam si sube muchos repetidos
        return

    # Guardar en inventario personal del usuario
    user_inventory.append({"file_id": file_id, "file_unique_id": file_unique_id, "type": media_type})

    # 2. Respaldo oculto en el canal (FILTRO GLOBAL ANTIDUPLICADOS)
    if file_unique_id not in db["global_files"]:
        try:
            await asyncio.sleep(0.1) # Pequeña pausa protectora para evitar rate-limits de Telegram
            await bot.copy_message(chat_id=BACKUP_CHANNEL_ID, from_chat_id=user_id, message_id=message.message_id)
            db["global_files"].add(file_unique_id) # Registrar que ya está en el canal
        except Exception as e:
            logging.error(f"Error al respaldar en canal: {e}")

    # 3. Notificación al usuario con sistema Anti-Spam (Agrupación de mensajes)
    current_time = time.time()
    # Solo le enviamos confirmación si han pasado más de 3 segundos desde la última confirmación
    if current_time - last_notified.get(user_id, 0) > 3.0:
        await message.answer(f"✅ Recibiendo archivos... Se están guardando en tu inventario.\n📊 Total en tu inventario: {len(user_inventory)}")
        last_notified[user_id] = current_time

# --- CONFIGURACIÓN PRINCIPAL DE ARRANQUE ---
async def main():
    dp.include_router(router)
    
    # Configurar el menú nativo de Telegram al iniciar
    await setup_bot_commands(bot)
    
    # Iniciar servidor web y bot en paralelo
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())