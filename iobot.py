import asyncio, logging, json, os, time
from urllib.parse import quote
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
from aiogram.filters import CommandStart, StateFilter, ChatMemberUpdatedFilter
from aiogram.filters.chat_member_updated import IS_NOT_MEMBER, IS_MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

# ================= CONFIGURACIÓN =================
TOKEN = "8854630623:AAEHltMCc6bWJyTEla5X0jb3Jn9H0wBXK80"
DATA_FILE = "users.json"
GRUPO_ID = -1004407392689 # ⚠️ REEMPLAZA CON EL ID REAL DEL GRUPO VIP

# --- CANAL/GRUPO OBLIGATORIO PARA USAR EL BOT ---
CANAL_OBLIGATORIO_ID = -1004442410627 # ⚠️ REEMPLAZA CON EL ID DEL CANAL
CANAL_OBLIGATORIO_LINK = "https://t.me/+N6EBzYbD16k2NDMx" # ⚠️ REEMPLAZA CON EL LINK

META_REFERIDOS = 3
TIEMPO_MAXIMO_INACTIVIDAD = 8 * 3600 # 8 horas en segundos

# ================= PERMISOS Y EXCEPCIONES =================
# Los que pueden usar el botón de Difusión Global (Admins reales)
SUPER_ADMIN_IDS = {8983189714, 8764734838} 

# Los que son inmunes a las reglas (8 horas, canal obligatorio, referidos)
USUARIOS_EXENTOS = {
    8748956307, 8764734838, 6630522163, 8831263313, 8556221763, 
    5142196200, 7452819858, 8803304819, 8266066936, 8985586526, 
    8847243934, 8864888335
}
# Añadimos a los Super Admins a la lista de exentos automáticamente
USUARIOS_EXENTOS.update(SUPER_ADMIN_IDS)

# ================= BASE DE DATOS LOCAL =================
usuarios_data = {} 

def load_data():
    global usuarios_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f: usuarios_data = json.load(f)
        except:
            usuarios_data = {}

def save_data():
    with open(DATA_FILE, "w") as f: json.dump(usuarios_data, f)

load_data()
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

class AdminPanel(StatesGroup):
    esperando_mensaje_difusion = State()

# ================= VERIFICACIÓN DE SUSCRIPCIÓN OBLIGATORIA =================
async def es_miembro_obligatorio(user_id: int) -> bool:
    """Verifica si el usuario está dentro del canal/grupo obligatorio."""
    # Si es inmune, pasa directo sin verificar
    if user_id in USUARIOS_EXENTOS: return True 
    try:
        member = await bot.get_chat_member(chat_id=CANAL_OBLIGATORIO_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except TelegramAPIError:
        return False

def menu_verificacion(lang="es"):
    texto_unirse = "🔗 Unirse al Canal" if lang == "es" else "🔗 Join Channel"
    texto_verificar = "🔄 Verificar Ingreso" if lang == "es" else "🔄 Verify Join"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texto_unirse, url=CANAL_OBLIGATORIO_LINK)],
        [InlineKeyboardButton(text=texto_verificar, callback_data="verificar_ingreso")]
    ])

# ================= MENÚS Y BOTONES =================
def menu_idioma():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇪🇸 Español", callback_data="lang_es"), 
         InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")]
    ])

async def menu_principal(user_id, bot_username):
    lang = usuarios_data.get(str(user_id), {}).get("lang", "es")
    link = f"https://t.me/{bot_username}?start={user_id}"
    
    if lang == "es":
        texto_compartir = f"🚀 Únete a este increíble grupo exclusivo. Inicia este bot para entrar:\n{link}"
        btn_compartir = InlineKeyboardButton(text="📤 Compartir mi Link", url=f"https://t.me/share/url?url={link}&text={quote(texto_compartir)}")
        buttons = [
            [InlineKeyboardButton(text="🔗 Obtener mi Link", callback_data="get_link")],
            [btn_compartir],
            [InlineKeyboardButton(text="📊 Mis Estadísticas", callback_data="stats"), InlineKeyboardButton(text="📖 ¿Cómo funciona?", callback_data="how_it_works")],
            [InlineKeyboardButton(text="🔓 Obtener Acceso al Grupo", callback_data="check_join")],
            [InlineKeyboardButton(text="🌍 Cambiar Idioma", callback_data="change_lang")]
        ]
    else:
        texto_compartir = f"🚀 Join this amazing exclusive group. Start this bot to enter:\n{link}"
        btn_compartir = InlineKeyboardButton(text="📤 Share my Link", url=f"https://t.me/share/url?url={link}&text={quote(texto_compartir)}")
        buttons = [
            [InlineKeyboardButton(text="🔗 Get Invite Link", callback_data="get_link")],
            [btn_compartir],
            [InlineKeyboardButton(text="📊 My Stats", callback_data="stats"), InlineKeyboardButton(text="📖 How it works?", callback_data="how_it_works")],
            [InlineKeyboardButton(text="🔓 Get Group Access", callback_data="check_join")],
            [InlineKeyboardButton(text="🌍 Change Language", callback_data="change_lang")]
        ]

    # SOLO los Super Admins ven el botón de Difusión
    if user_id in SUPER_ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="📢 Difusión Global (Admin)", callback_data="admin_broadcast")])
        
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def inicializar_usuario(uid):
    if str(uid) not in usuarios_data:
        usuarios_data[str(uid)] = {
            "referidos": 0, "lang": None, "link_entregado": False, 
            "in_group": False, "last_msg": 0.0
        }
        save_data()

# ================= LÓGICA DE REGISTRO Y /START =================
@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message):
    uid = str(message.from_user.id)
    
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        inviter = args[1]
        if inviter != uid and uid not in usuarios_data: 
            inicializar_usuario(uid)
            if inviter in usuarios_data:
                usuarios_data[inviter]["referidos"] += 1
                save_data()
                try: 
                    msg_inv = "🎉 **¡Nuevo Referido!**" if usuarios_data[inviter].get("lang") == "es" else "🎉 **New Referral!**"
                    await bot.send_message(int(inviter), msg_inv, parse_mode="Markdown")
                except: pass

    inicializar_usuario(uid)
    
    if not await es_miembro_obligatorio(message.from_user.id):
        lang = usuarios_data[uid].get("lang", "es") or "es"
        msg = ("⚠️ **Paso Obligatorio**\nPara poder usar este bot, debes unirte a nuestro canal patrocinador primero." 
               if lang == "es" else 
               "⚠️ **Mandatory Step**\nYou must join our sponsor channel first to use this bot.")
        await message.answer(msg, parse_mode="Markdown", reply_markup=menu_verificacion(lang))
        return 

    if usuarios_data[uid].get("lang") is None:
        await message.answer("🌍 ¡Bienvenido! Selecciona tu idioma:\n\n🌍 Welcome! Select your language:", reply_markup=menu_idioma())
    else:
        bot_info = await bot.get_me()
        await message.answer("🚀 **Menú Principal / Main Menu**", reply_markup=await menu_principal(message.from_user.id, bot_info.username))

# ================= CALLBACK: VERIFICAR INGRESO OBLIGATORIO =================
@router.callback_query(F.data == "verificar_ingreso")
async def verificar_ingreso_cmd(call: CallbackQuery):
    uid = str(call.from_user.id)
    lang = usuarios_data.get(uid, {}).get("lang", "es") or "es"
    
    if await es_miembro_obligatorio(call.from_user.id):
        await call.answer("✅ Verificación exitosa." if lang == "es" else "✅ Verification successful.", show_alert=True)
        await call.message.delete()
        
        if usuarios_data[uid].get("lang") is None:
            await call.message.answer("🌍 ¡Bienvenido! Selecciona tu idioma:\n\n🌍 Welcome! Select your language:", reply_markup=menu_idioma())
        else:
            bot_info = await bot.get_me()
            await call.message.answer("🚀 **Menú Principal / Main Menu**", reply_markup=await menu_principal(call.from_user.id, bot_info.username))
    else:
        msg = "❌ Aún no estás en el canal. Únete y vuelve a verificar." if lang == "es" else "❌ You haven't joined the channel yet. Join and verify again."
        await call.answer(msg, show_alert=True)

# ================= GESTIÓN DE IDIOMAS =================
@router.callback_query(F.data.startswith("lang_"))
async def set_lang(call: CallbackQuery):
    lang = call.data.split("_")[1]
    uid = str(call.from_user.id)
    
    usuarios_data[uid]["lang"] = lang
    save_data()
    
    bot_info = await bot.get_me()
    msg = "✅ Idioma configurado.\nSelecciona una opción:" if lang == "es" else "✅ Language set.\nSelect an option:"
    await call.message.edit_text(msg, reply_markup=await menu_principal(call.from_user.id, bot_info.username))

@router.callback_query(F.data == "change_lang")
async def change_lang_cmd(call: CallbackQuery):
    await call.message.edit_text("🌍 Selecciona tu nuevo idioma / Select your new language:", reply_markup=menu_idioma())

# ================= REGLAS E INSTRUCCIONES =================
@router.callback_query(F.data == "how_it_works")
async def show_how(call: CallbackQuery):
    lang = usuarios_data.get(str(call.from_user.id), {}).get("lang", "es")
    if lang == "es":
        text = (
            "📖 **¿CÓMO FUNCIONA EL GRUPO?**\n\n"
            f"1️⃣ Invita a **{META_REFERIDOS} amigos** usando tu enlace personal.\n"
            "2️⃣ Presiona *'Obtener Acceso al Grupo'* para recibir tu entrada.\n\n"
            "⚠️ **REGLA DE ORO (LEER ATENTAMENTE):**\n"
            "Una vez dentro del grupo, **tienes la obligación de aportar contenido o escribir al menos una vez cada 8 horas**. "
            "El bot tiene un cronómetro interno. Si pasas más de 8 horas sin interactuar en el grupo, **serás expulsado automáticamente y perderás todos tus referidos**."
        )
    else:
        text = (
            "📖 **HOW IT WORKS?**\n\n"
            f"1️⃣ Invite **{META_REFERIDOS} friends** using your personal link.\n"
            "2️⃣ Click *'Get Group Access'* to receive your entry pass.\n\n"
            "⚠️ **GOLDEN RULE (READ CAREFULLY):**\n"
            "Once inside the group, **you must contribute content or send a message at least once every 8 hours**. "
            "The bot runs an internal timer. If 8 hours pass without your interaction in the group, **you will be automatically kicked and lose your referrals**."
        )
    bot_info = await bot.get_me()
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=await menu_principal(call.from_user.id, bot_info.username))

# ================= ESTADÍSTICAS Y LINKS =================
@router.callback_query(F.data == "get_link")
async def get_link(call: CallbackQuery):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={call.from_user.id}"
    lang = usuarios_data.get(str(call.from_user.id), {}).get("lang", "es")
    msg = "🔗 **Tu enlace de invitación:**" if lang == "es" else "🔗 **Your invite link:**"
    await call.message.answer(f"{msg}\n`{link}`\n\n*(Puedes usar el botón de 'Compartir' del menú principal para enviarlo rápido)*", parse_mode="Markdown")

@router.callback_query(F.data == "stats")
async def stats(call: CallbackQuery):
    count = usuarios_data.get(str(call.from_user.id), {}).get("referidos", 0)
    lang = usuarios_data.get(str(call.from_user.id), {}).get("lang", "es")
    msg = f"👥 Has invitado a: {count}/{META_REFERIDOS}" if lang == "es" else f"👥 You invited: {count}/{META_REFERIDOS}"
    
    # Mensaje extra si el usuario es Exento/Inmune
    if call.from_user.id in USUARIOS_EXENTOS:
        msg += "\n\n✨ Eres Inmune (Acceso VIP desbloqueado permanentemente)"
        
    await call.answer(msg, show_alert=True)

# ================= ENTREGA DEL ACCESO AL GRUPO VIP =================
@router.callback_query(F.data == "check_join")
async def check_join(call: CallbackQuery):
    uid = str(call.from_user.id)
    user_id_int = call.from_user.id
    user_data = usuarios_data.get(uid, {})
    count = user_data.get("referidos", 0)
    lang = user_data.get("lang", "es")
    
    if not await es_miembro_obligatorio(user_id_int):
        await call.message.answer(
            "⚠️ Te has salido del canal obligatorio. Vuelve a unirte para usar el bot." if lang == "es" else "⚠️ You left the mandatory channel. Join again to use the bot.",
            reply_markup=menu_verificacion(lang)
        )
        return

    # Comprueba si ya se le dio el link Y NO ES EXENTO
    if user_data.get("link_entregado") and user_id_int not in USUARIOS_EXENTOS:
        msg = "❌ Ya se te ha generado un enlace. Si fuiste expulsado, debes conseguir referidos con otra cuenta." if lang == "es" else "❌ A link was already generated for you."
        return await call.answer(msg, show_alert=True)

    # Puede pasar si tiene 3 referidos O ES INMUNE
    if count >= META_REFERIDOS or user_id_int in USUARIOS_EXENTOS:
        try:
            link = await bot.create_chat_invite_link(chat_id=GRUPO_ID, member_limit=1, name=f"Acceso: {uid}")
            
            usuarios_data[uid]["link_entregado"] = True
            save_data()
            
            btn_entrar = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚪 Entrar al Grupo" if lang=="es" else "🚪 Enter Group", url=link.invite_link)]
            ])
            
            if lang == "es":
                texto_exito = (
                    "✅ **¡ACCESO DESBLOQUEADO!**\n\n"
                    "⚠️ **ANTES DE ENTRAR, RECUERDA:**\n"
                    "A partir del momento en que presiones el botón y entres al grupo, comenzará un conteo de 8 horas. "
                    "**Debes aportar contenido o interactuar antes de que el tiempo se acabe**, o el bot te eliminará permanentemente.\n\n"
                    "*(Si eres usuario Exento, ignora esta regla).* \n\n"
                    "Toca el botón abajo para ingresar (es de un solo uso):"
                )
            else:
                texto_exito = (
                    "✅ **ACCESS UNLOCKED!**\n\n"
                    "⚠️ **BEFORE YOU ENTER, REMEMBER:**\n"
                    "The moment you enter the group, an 8-hour countdown begins. "
                    "**You must contribute content or interact before time runs out**, otherwise the bot will kick you permanently.\n\n"
                    "*(If you are an Exempt user, ignore this rule).* \n\n"
                    "Click the button below to join (1-use only):"
                )
                
            await call.message.answer(texto_exito, parse_mode="Markdown", reply_markup=btn_entrar)
            
        except TelegramAPIError as e:
            await call.message.answer("❌ Error. Asegúrate de que el bot sea administrador en el grupo.")
    else:
        msg = f"❌ Te faltan {META_REFERIDOS - count} referidos." if lang == "es" else f"❌ You need {META_REFERIDOS - count} more referrals."
        await call.answer(msg, show_alert=True)

# ================= RASTREADOR DE INGRESO Y ACTIVIDAD (REGLA DE 8 HORAS) =================
@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_detecta_ingreso(event: ChatMemberUpdated):
    if event.chat.id == GRUPO_ID:
        uid = str(event.new_chat_member.user.id)
        if uid in usuarios_data:
            usuarios_data[uid]["in_group"] = True
            usuarios_data[uid]["last_msg"] = time.time()
            save_data()

@router.message(F.chat.id == GRUPO_ID)
async def bot_detecta_mensaje(message: Message):
    uid = str(message.from_user.id)
    if uid in usuarios_data:
        usuarios_data[uid]["last_msg"] = time.time()
        if not usuarios_data[uid].get("in_group"):
            usuarios_data[uid]["in_group"] = True
        save_data()

# ================= EL VERDUGO (CRONÓMETRO DE 8 HORAS) =================
async def verificador_inactividad(bot: Bot):
    while True:
        await asyncio.sleep(900) # Revisa cada 15 minutos
        ahora = time.time()
        
        for uid_str, data in list(usuarios_data.items()):
            uid = int(uid_str)
            
            # SI ES EXENTO, SALTAMOS A ESTE USUARIO (Nunca será expulsado por inactividad)
            if uid in USUARIOS_EXENTOS: continue
            
            if data.get("in_group", False):
                ultimo_msg = data.get("last_msg", ahora)
                tiempo_inactivo = ahora - ultimo_msg
                
                if tiempo_inactivo > TIEMPO_MAXIMO_INACTIVIDAD:
                    try:
                        await bot.ban_chat_member(chat_id=GRUPO_ID, user_id=uid)
                        await bot.unban_chat_member(chat_id=GRUPO_ID, user_id=uid)
                        
                        usuarios_data[uid_str]["in_group"] = False
                        usuarios_data[uid_str]["referidos"] = 0
                        usuarios_data[uid_str]["link_entregado"] = False
                        save_data()
                        
                        lang = data.get("lang", "es")
                        msg = "❌ Has sido expulsado del grupo por inactividad de 8 horas. Tus referidos se han reiniciado a 0. Vuelve a ganarte tu lugar." if lang == "es" else "❌ You were kicked for 8 hours of inactivity. Your referrals are reset to 0."
                        await bot.send_message(uid, msg)
                        
                    except Exception as e:
                        logging.error(f"Error expulsando a {uid}: {e}")

# ================= DIFUSIÓN GLOBAL (ADMINS) =================
@router.callback_query(F.data == "admin_broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in SUPER_ADMIN_IDS: return
    await call.message.answer("📢 Envía el mensaje de difusión (o escribe /cancelar):")
    await state.set_state(AdminPanel.esperando_mensaje_difusion)

@router.message(StateFilter(AdminPanel.esperando_mensaje_difusion))
async def broadcast_execute(message: Message, state: FSMContext):
    if message.text == "/cancelar":
        await state.clear()
        return await message.answer("✅ Cancelado.")
        
    await message.answer("⏳ **Enviando difusión...**")
    await state.clear()
    exitos = 0
    for uid in usuarios_data.keys():
        try:
            await message.copy_to(chat_id=int(uid))
            exitos += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"📢 **Difusión lista:** Entregados a {exitos} usuarios.")

# ================= EJECUCIÓN (RENDER) =================
async def web_server():
    runner = web.AppRunner(web.Application())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()

async def main():
    dp.include_router(router)
    asyncio.create_task(verificador_inactividad(bot))
    asyncio.create_task(web_server())
    logging.info("🤖 Bot Corriendo con Lista de Exentos")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())