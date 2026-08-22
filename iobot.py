import asyncio, logging, json, os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramAPIError

# ================= CONFIGURACIÓN =================
TOKEN = "8783353791:AAF0wQHXBeRzBrovC3hisyxOUOUuspUgyTs"
DATA_FILE = "users.json"
SUPER_ADMIN_IDS = {8983189714, 8764734838} 

# ================= DATOS PERSISTENTES =================
usuarios_data = {} # {user_id: {"referidos": 0, "lang": "es"}}

def load_data():
    global usuarios_data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f: usuarios_data = json.load(f)

def save_data():
    with open(DATA_FILE, "w") as f: json.dump(usuarios_data, f)

load_data()
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

# ================= MENÚS Y BOTONES =================
def menu_principal(user_id):
    is_admin = user_id in SUPER_ADMIN_IDS
    buttons = [
        [InlineKeyboardButton(text="🔗 Obtener Link de Invitación", callback_data="get_link")],
        [InlineKeyboardButton(text="📊 Mis Estadísticas", callback_data="stats")]
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="📢 Difusión Global (Admin)", callback_data="admin_broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= LÓGICA DE START Y REFERIDOS =================
@router.message(CommandStart())
async def start_cmd(message: Message):
    user_id = str(message.from_user.id)
    
    # Lógica de referido
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        inviter = args[1]
        if inviter != user_id:
            if inviter not in usuarios_data: usuarios_data[inviter] = {"referidos": 0}
            usuarios_data[inviter]["referidos"] = usuarios_data.get(inviter, {"referidos": 0})["referidos"] + 1
            save_data()
            try: await bot.send_message(int(inviter), "🎉 ¡Alguien usó tu link! (+1 referido)")
            except: pass

    if user_id not in usuarios_data:
        usuarios_data[user_id] = {"referidos": 0}
        save_data()
        
    await message.answer(
        "🏛 **SISTEMA DE ADMISIÓN**\n\n"
        "Para ingresar al grupo exclusivo, debes invitar a **3 personas** usando tu link personal.\n\n"
        "Usa los botones para gestionar tu acceso:", 
        reply_markup=menu_principal(message.from_user.id)
    )

# ================= CALLBACKS =================
@router.callback_query(F.data == "get_link")
async def get_link(call: CallbackQuery):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={call.from_user.id}"
    await call.message.answer(f"🔗 Tu link personal:\n`{link}`\n\nInvita a 3 amigos para desbloquear el acceso.", parse_mode="Markdown")

@router.callback_query(F.data == "stats")
async def stats(call: CallbackQuery):
    count = usuarios_data.get(str(call.from_user.id), {}).get("referidos", 0)
    await call.answer(f"Has invitado a {count} personas.", show_alert=True)

# ================= ADMISIÓN AL GRUPO =================
@router.chat_join_request()
async def join_req(request: ChatJoinRequest):
    count = usuarios_data.get(str(request.from_user.id), {}).get("referidos", 0)
    if count >= 3 or request.from_user.id in SUPER_ADMIN_IDS:
        await request.approve()
    else:
        await bot.send_message(request.from_user.id, "❌ Necesitas 3 referidos para entrar. Usa /start para ver tu progreso.")
        await request.decline()

# ================= ADMIN: DIFUSIÓN =================
@router.callback_query(F.data == "admin_broadcast")
async def broadcast_cmd(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Envíame el mensaje que quieres difundir a todos los usuarios:")
    # (Aquí podrías implementar un State para capturar el mensaje)

# ================= SERVIDOR WEB RENDER =================
async def web_server():
    runner = web.AppRunner(web.Application())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()

async def main():
    dp.include_router(router)
    asyncio.create_task(web_server())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())