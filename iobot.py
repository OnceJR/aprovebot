import asyncio
import logging
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# Configuración básica
TOKEN = "8930108804:AAFakVeuGHUVnbsB9pv0jbRKON4pPkzsJQE"
BACKUP_CHANNEL_ID = -1004465910047  # ID de tu canal privado de respaldo
SUPER_ADMIN_ID = 8983189714

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# Base de datos simulada (En producción, reemplaza esto por PostgreSQL o SQLite)
db = {
    "users": {},          # user_id: {"lang": "es", "inventory": []}
    "history": {},        # (user_id, target_id): set([file_unique_ids])
    "active_chats": {},   # user_id: target_id
    "pending_trade": {}   # user_id: {"amount": 10, "type": "photo"}
}

class ChatStates(StatesGroup):
    chtting = State()
    waiting_trade_amount = State()

# --- UPTIMEROBOT KEEPALIVE SERVER ---
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.AppSite(runner, "0.0.0.0", 8080)
    await site.start()

# --- COMANDO /START Y ENLACES DE INVITACIÓN ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1) # Permite capturar links tipo /start ref_12345

    if user_id not in db["users"]:
        db["users"][user_id] = {"lang": "es", "inventory": []}

    # Si entró por un enlace de invitación de otro usuario
    if len(args) > 1:
        inviter_id = args[1]
        await message.answer(f"¡Has entrado a través del link de un amigo! (Referencia: {inviter_id})")

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇪🇸 Español", callback_data="lang_es"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_de")] # (o en)
    ])
    
    await message.answer("¡Bienvenido! Selecciona tu idioma / Welcome! Choose your language:", reply_markup=markup)

@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery):
    user_id = callback.from_user.id
    lang = callback.data.split("_")[1]
    db["users"][user_id]["lang"] = lang

    text = "¡Idioma configurado en Español! Envía fotos, videos o archivos para guardarlos en tu inventario." if lang == "es" \
           else "Language set to English! Send photos, videos or files to save them to your inventory."
    
    # Generar Link de Referidos del usuario
    bot_info = await bot.get_me()
    my_link = f"https://t.me/{bot_info.username}?start={user_id}"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Buscar Chat / Find Chat", callback_data="find_chat")],
        [InlineKeyboardButton(text="🔗 Compartir mi Link", url=f"https://t.me/share/url?url={my_link}&text=¡Entra a este bot para intercambiar contenido!")]
    ])
    
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()

# --- COMANDO /HELP ---
@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 **Guía de Uso del Bot:**\n\n"
        "1. **Carga:** Envía cualquier foto, video o archivo al chat. Se guardarán en tu inventario personal.\n"
        "2. **Intercambio:** Usa 'Buscar Chat' para emparejarte con otro usuario de forma anónima.\n"
        "3. **Acuerdo:** En el chat, acuerden una cantidad (ej. 10x10) y propongan el intercambio.\n"
        "4. **Idioma:** Usa /start para reconfigurar tu idioma en cualquier momento.\n"
        "5. **Tu Link:** Comparte tu enlace de invitación para que otros seconecten contigo."
    )
    await message.answer(help_text, parse_mode="Markdown")

# --- PANEL DE SÚPER ADMIN ---
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != SUPER_ADMIN_ID:
        await message.answer("No tienes permisos para usar este comando.")
        return

    total_users = len(db["users"])
    total_files = sum(len(u["inventory"]) for u in db["users"].values())
    active_chats_count = len(db["active_chats"]) // 2

    admin_text = (
        "👑 **Panel de Súper Administrador**\n\n"
        f"👥 Usuarios totales: `{total_users}`\n"
        f"📁 Archivos en inventarios: `{total_files}`\n"
        f"💬 Chats activos ahora: `{active_chats_count}`\n\n"
        "Usa `/broadcast [mensaje]` para enviar un comunicado a todos."
    )
    await message.answer(admin_text, parse_mode="Markdown")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    text_to_broadcast = message.text.replace("/broadcast", "").strip()
    if not text_to_broadcast:
        await message.answer("Escribe el mensaje que deseas difundir. Ejemplo: `/broadcast Hola a todos`")
        return

    count = 0
    for user_id in db["users"]:
        try:
            await bot.send_message(user_id, f"📢 **Aviso importante:**\n\n{text_to_broadcast}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05) # Evitar flood limits de Telegram
        except Exception:
            pass

    await message.answer(f"✅ Difusión completada con éxito a `{count}` usuarios.")

# --- MANEJO DE ARCHIVOS E INVENTARIO ---
@router.message(F.photo | F.video | F.document)
async def handle_media_upload(message: Message):
    user_id = message.from_user.id
    if user_id not in db["users"]:
        db["users"][user_id] = {"lang": "es", "inventory": []}

    # Extraer identificadores de Telegram
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

    # Evitar duplicados absolutos en el inventario del usuario
    user_inventory = db["users"][user_id]["inventory"]
    if any(item["file_unique_id"] == file_unique_id for item in user_inventory):
        await message.answer("⚠️ Este archivo ya está en tu inventario.")
        return

    # Guardar en inventario personal
    user_inventory.append({"file_id": file_id, "file_unique_id": file_unique_id, "type": media_type})

    # Respaldo oculto en el canal (Evitando duplicados globales mediante file_unique_id)
    try:
        # Nota: Aquí implementarías la validación si el file_unique_id ya pasó por el canal
        await bot.copy_message(chat_id=BACKUP_CHANNEL_ID, from_chat_id=user_id, message_id=message.message_id)
    except Exception as e:
        logging.error(f"Error al respaldar en canal: {e}")

    await message.answer(f"✅ Archivo guardado correctamente en tu inventario.\n📊 Total en inventario: {len(user_inventory)}")

# --- CONFIGURACIÓN PRINCIPAL DE ARRANCKE ---
async def main():
    dp.include_router(router)
    # Iniciar servidor web para UptimeRobot en paralelo con el bot de Telegram
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())