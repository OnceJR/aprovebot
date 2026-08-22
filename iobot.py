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

# ================= MENÚS PROFESIONALES =================
def menu_idioma():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇪🇸 Español", callback_data="lang_es"), 
         InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")]
    ])

def menu_principal(user_id):
    lang = usuarios_data.get(str(user_id), {}).get("lang", "es")
    text_link = "🔗 Get Invite Link" if lang == "en" else "🔗 Obtener mi Link"
    text_stats = "📊 Stats" if lang == "en" else "📊 Mis Estadísticas"
    text_how = "📖 How it works" if lang == "en" else "📖 ¿Cómo funciona?"
    
    buttons = [
        [InlineKeyboardButton(text=text_link, callback_data="get_link")],
        [InlineKeyboardButton(text=text_stats, callback_data="stats")],
        [InlineKeyboardButton(text=text_how, callback_data="how_it_works")]
    ]
    if user_id in SUPER_ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="📢 Broadcast (Admin)", callback_data="admin_broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= LÓGICA DE IDIOMA Y START =================
@router.message(CommandStart())
async def start_cmd(message: Message):
    user_id = str(message.from_user.id)
    
    # Lógica de referido
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        inviter = args[1]
        if inviter != user_id:
            if inviter not in usuarios_data: usuarios_data[inviter] = {"referidos": 0, "lang": "es"}
            usuarios_data[inviter]["referidos"] = usuarios_data.get(inviter, {"referidos": 0})["referidos"] + 1
            save_data()
            try: await bot.send_message(int(inviter), "🎉 +1 Referido!")
            except: pass

    if user_id not in usuarios_data:
        await message.answer("¡Bienvenido! Selecciona tu idioma:\nWelcome! Select your language:", reply_markup=menu_idioma())
    else:
        await message.answer("Menú Principal:", reply_markup=menu_principal(int(user_id)))

@router.callback_query(F.data.startswith("lang_"))
async def set_lang(call: CallbackQuery):
    lang = call.data.split("_")[1]
    if call.from_user.id not in usuarios_data: usuarios_data[str(call.from_user.id)] = {"referidos": 0}
    usuarios_data[str(call.from_user.id)]["lang"] = lang
    save_data()
    await call.message.edit_text("Configurado. Menu:", reply_markup=menu_principal(call.from_user.id))

# ================= BOTÓN: ¿CÓMO FUNCIONA? =================
@router.callback_query(F.data == "how_it_works")
async def show_how(call: CallbackQuery):
    lang = usuarios_data.get(str(call.from_user.id), {}).get("lang", "es")
    if lang == "es":
        text = ("📖 **¿Cómo ingresar al grupo?**\n\n"
                "1. Comparte tu link personal con tus amigos.\n"
                "2. Consigue 3 personas que inicien el bot.\n"
                "3. ¡El acceso al grupo se desbloquea automáticamente!\n\n"
                "¡Es fácil, rápido y seguro!")
    else:
        text = ("📖 **How to join?**\n\n"
                "1. Share your personal link with your friends.\n"
                "2. Get 3 people to start this bot.\n"
                "3. Group access unlocks automatically!\n\n"
                "Fast, easy and safe!")
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=menu_principal(call.from_user.id))

# ================= CALLBACKS DE ACCIÓN =================
@router.callback_query(F.data == "get_link")
async def get_link(call: CallbackQuery):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={call.from_user.id}"
    await call.message.answer(f"🔗 Tu link:\n`{link}`", parse_mode="Markdown")

@router.callback_query(F.data == "stats")
async def stats(call: CallbackQuery):
    count = usuarios_data.get(str(call.from_user.id), {}).get("referidos", 0)
    msg = f"👥 Invitaciones: {count}/3"
    await call.answer(msg, show_alert=True)

# ================= ADMISIÓN (CHAT JOIN REQUEST) =================
@router.chat_join_request()
async def join_req(request: ChatJoinRequest):
    count = usuarios_data.get(str(request.from_user.id), {}).get("referidos", 0)
    if count >= 3 or request.from_user.id in SUPER_ADMIN_IDS:
        await request.approve()
    else:
        await bot.send_message(request.from_user.id, "❌ Necesitas 3 referidos para entrar.")
        await request.decline()

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