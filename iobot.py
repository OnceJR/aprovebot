import asyncio
import logging
import json
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

# ================= CONFIGURACIÓN =================
# ⚠️ ADVERTENCIA: Cambia este token, el anterior fue expuesto.
TOKEN = "8783353791:AAF0wQHXBeRzBrovC3hisyxOUOUuspUgyTs"
ETIQUETA_REQUERIDA = "ᴼᵀᴹ"
DATA_FILE = "users.json"

SUPER_ADMIN_IDS = {8983189714, 8764734838} 
ADMINS_FIJOS = {8748956307, 8764734838, 6630522163, 8831263313, 8556221763, 5142196200, 7452819858, 8803304819, 8266066936, 8985586526}

# ================= PERSISTENCIA DE DATOS (JSON) =================
def load_data():
    if not os.path.exists(DATA_FILE):
        return set(), set(ADMINS_FIJOS | SUPER_ADMIN_IDS), set()
    
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            registrados = set(data.get("registrados", []))
            exentos = set(data.get("exentos", [])) | ADMINS_FIJOS | SUPER_ADMIN_IDS
            grupos = set(data.get("grupos", []))
            return registrados, exentos, grupos
    except json.JSONDecodeError:
        return set(), set(ADMINS_FIJOS | SUPER_ADMIN_IDS), set()

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump({
            "registrados": list(usuarios_registrados),
            "exentos": list(usuarios_exentos),
            "grupos": list(grupos_conocidos)
        }, f)

usuarios_registrados, usuarios_exentos, grupos_conocidos = load_data()

# ================= INICIALIZACIÓN =================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

class AdminPanel(StatesGroup):
    esperando_id_excepcion = State()
    esperando_mensaje_difusion = State()

# ================= TECLADOS INLINE =================
def obtener_teclado_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Ver Estadísticas", callback_data="admin_stats")],
        [
            InlineKeyboardButton(text="🛡 Añadir Excepción", callback_data="admin_add_exempt"),
            InlineKeyboardButton(text="📢 Difusión Global", callback_data="admin_broadcast")
        ],
        [InlineKeyboardButton(text="❌ Cerrar Panel", callback_data="admin_close")]
    ])

def obtener_teclado_cancelar():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Cancelar Operación", callback_data="admin_cancel")]
    ])

# ================= RASTREO AUTOMÁTICO DE GRUPOS =================
@router.my_chat_member()
async def bot_added_or_removed(event: ChatMemberUpdated):
    """Detecta si el bot es añadido o eliminado de un grupo para gestionar links."""
    if event.chat.type not in ["group", "supergroup"]: return
    
    if event.new_chat_member.status in ["administrator", "member"]:
        grupos_conocidos.add(event.chat.id)
        logging.info(f"Registrado en el grupo: {event.chat.title}")
    elif event.new_chat_member.status in ["kicked", "left"]:
        grupos_conocidos.discard(event.chat.id)
        logging.info(f"Eliminado del grupo: {event.chat.title}")
    save_data()

# ================= COMANDO DE ENLACES DE 1 USO =================
@router.message(Command("link"), F.chat.type == "private")
async def generar_link_un_uso(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    
    if not grupos_conocidos:
        await message.answer("❌ No tengo ningún grupo registrado. Asegúrate de añadirme a un grupo como administrador primero.")
        return

    await message.answer("⏳ Generando enlaces exclusivos...")
    
    for grupo_id in grupos_conocidos:
        try:
            link = await bot.create_chat_invite_link(
                chat_id=grupo_id,
                member_limit=1,
                name="Invitación Única"
            )
            chat_info = await bot.get_chat(grupo_id)
            
            await message.answer(
                f"🏛 **Grupo:** {chat_info.title}\n"
                f"🔗 **Enlace (1 Solo Uso):**\n`{link.invite_link}`\n\n"
                "*(Este enlace se autodestruirá tras 1 uso)*",
                parse_mode="Markdown"
            )
        except TelegramAPIError as e:
            await message.answer(f"❌ No pude crear link para un grupo (ID: `{grupo_id}`). Verifica mis permisos de administrador.")

# ================= FILTRO DE ADMISIÓN (CAPTCHA) =================
@router.chat_join_request()
async def process_join_request(join_request: ChatJoinRequest):
    user_name = join_request.from_user.full_name or ""
    user_id = join_request.from_user.id
    chat_id = join_request.chat.id
    
    if user_id not in usuarios_registrados:
        usuarios_registrados.add(user_id)
        save_data()
    
    if ETIQUETA_REQUERIDA in user_name or user_id in usuarios_exentos:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Soy humano y tengo el tag", callback_data=f"captcha_{chat_id}_{user_id}")]
        ])
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🛡 **VERIFICACIÓN DE SEGURIDAD**\n\nHola {join_request.from_user.first_name}, hemos recibido tu solicitud. Confirma que eres humano y que posees la etiqueta `{ETIQUETA_REQUERIDA}` presionando el botón.",
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except TelegramAPIError:
            try: await join_request.decline()
            except TelegramAPIError: pass
    else:
        try: await join_request.decline()
        except TelegramAPIError: pass

@router.callback_query(F.data.startswith("captcha_"))
async def handle_captcha_approval(callback: CallbackQuery):
    data = callback.data.split("_")
    chat_id, user_id = int(data[1]), int(data[2])
    
    if callback.from_user.id != user_id:
        return await callback.answer("❌ Este botón no es para ti.", show_alert=True)

    try:
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        await callback.message.edit_text("✅ **¡Verificación completada!**\nTu solicitud ha sido aprobada. ¡Bienvenido!", parse_mode="Markdown")
    except TelegramAPIError:
        await callback.message.edit_text("❌ **Error.** O ya fuiste aceptado, o tu solicitud expiró.", parse_mode="Markdown")

# ================= CHAT PRIVADO (PANEL ADMIN VS USUARIOS) =================
@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    
    if message.from_user.id not in usuarios_registrados:
        usuarios_registrados.add(message.from_user.id)
        save_data()
    
    if message.from_user.id in SUPER_ADMIN_IDS:
        admin_text = "👑 **PANEL DE CONTROL PRINCIPAL**\n\nSelecciona una opción del menú interactivo.\n*(Usa /link para generar accesos únicos)*"
        await message.answer(admin_text, parse_mode="Markdown", reply_markup=obtener_teclado_admin())
    else:
        welcome_text = (
            "🏛 **SISTEMA OFICIAL DE ADMISIÓN**\n\n"
            f"⚠️ **REQUISITO OBLIGATORIO:**\n"
            f"Para ingresar, es indispensable que agregues la etiqueta `{ETIQUETA_REQUERIDA}` a tu nombre de Telegram.\n\n"
            "📌 **Instrucciones:**\n"
            "1. Copia la etiqueta del mensaje inferior.\n"
            "2. Ve a los Ajustes de Telegram > Editar perfil.\n"
            "3. Péguela en tu nombre.\n"
            "4. Solicita unirte mediante el enlace de invitación.\n\n"
            "⛔️ *Nota:* Si retiras la etiqueta una vez dentro, serás expulsado."
        )
        await message.answer(welcome_text, parse_mode="Markdown")
        await message.answer(f"👇 **Toca la etiqueta para copiarla:**\n\n`{ETIQUETA_REQUERIDA}`", parse_mode="Markdown")

# ================= CALLBACKS DEL MENÚ ADMIN =================
@router.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        return await callback.answer("No tienes permisos.", show_alert=True)

    action = callback.data.replace("admin_", "")

    if action == "close":
        await callback.message.delete()
        await state.clear()
    elif action == "cancel":
        await callback.message.edit_text("✅ **Operación cancelada.**", parse_mode="Markdown", reply_markup=obtener_teclado_admin())
        await state.clear()
    elif action == "stats":
        texto_stats = (
            "📊 **ESTADÍSTICAS DEL BOT**\n\n"
            f"👥 **Usuarios Registrados:** `{len(usuarios_registrados)}`\n"
            f"🛡 **Usuarios Inmunes:** `{len(usuarios_exentos)}`\n"
            f"🏛 **Grupos Vinculados:** `{len(grupos_conocidos)}`"
        )
        await callback.message.edit_text(texto_stats, parse_mode="Markdown", reply_markup=obtener_teclado_admin())
    elif action == "add_exempt":
        await callback.message.edit_text("🛡 **AÑADIR EXCEPCIÓN**\nEnvíame el **ID Numérico** del usuario.", parse_mode="Markdown", reply_markup=obtener_teclado_cancelar())
        await state.set_state(AdminPanel.esperando_id_excepcion)
    elif action == "broadcast":
        await callback.message.edit_text(f"📢 **DIFUSIÓN GLOBAL**\nSe enviará a **{len(usuarios_registrados)}** usuarios. Envíame el mensaje.", parse_mode="Markdown", reply_markup=obtener_teclado_cancelar())
        await state.set_state(AdminPanel.esperando_mensaje_difusion)

# ================= CAPTURA DE ESTADOS (FSM) =================
@router.message(StateFilter(AdminPanel.esperando_id_excepcion), F.chat.type == "private")
async def recibir_id_excepcion(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    try:
        target_id = int(message.text.strip())
        usuarios_exentos.add(target_id)
        save_data()
        await message.answer(f"✅ ID `{target_id}` añadido a excepciones.", parse_mode="Markdown", reply_markup=obtener_teclado_admin())
        await state.clear()
    except ValueError:
        await message.answer("❌ Error: Debes enviar un ID numérico válido.", reply_markup=obtener_teclado_cancelar())

@router.message(StateFilter(AdminPanel.esperando_mensaje_difusion), F.chat.type == "private")
async def recibir_mensaje_difusion(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    
    await message.answer("⏳ **Iniciando difusión masiva...**")
    await state.clear()
    
    exitos, fallos = 0, 0
    for user_id in usuarios_registrados:
        try:
            await message.copy_to(chat_id=user_id)
            exitos += 1
            await asyncio.sleep(0.05)
        except TelegramAPIError:
            fallos += 1
            
    await message.answer(f"📢 **DIFUSIÓN FINALIZADA**\n✅ Éxitos: `{exitos}`\n❌ Fallos: `{fallos}`", parse_mode="Markdown", reply_markup=obtener_teclado_admin())

# ================= COMANDOS DE GRUPO (APORTADOR) =================
@router.message(Command("aportador"), F.chat.type.in_(["group", "supergroup"]))
async def dar_etiqueta_aportador(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    if not message.reply_to_message: return await message.reply("⚠️ Responde al mensaje del usuario.")

    target_user = message.reply_to_message.from_user
    try:
        await bot.promote_chat_member(message.chat.id, target_user.id, can_manage_chat=True)
        await bot.set_chat_administrator_custom_title(message.chat.id, target_user.id, "Aportador 💎")
        usuarios_exentos.add(target_user.id)
        save_data()
        await message.reply(f"✅ {target_user.first_name} ahora es Aportador.")
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

@router.message(Command("quitar_aportador"), F.chat.type.in_(["group", "supergroup"]))
async def quitar_etiqueta_aportador(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    if not message.reply_to_message: return await message.reply("⚠️ Responde al mensaje del usuario.")

    target_user = message.reply_to_message.from_user
    try:
        await bot.promote_chat_member(message.chat.id, target_user.id, can_manage_chat=False)
        if target_user.id in usuarios_exentos and target_user.id not in SUPER_ADMIN_IDS:
            usuarios_exentos.remove(target_user.id)
            save_data()
        await message.reply(f"✅ Se le quitó el rol a {target_user.first_name}.")
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

# ================= FILTRO GLOBAL (PATRULLAJE) =================
@router.message(F.chat.type.in_(["group", "supergroup"]))
async def group_messages_processor(message: Message):
    user_id = message.from_user.id
    
    # Registro silencioso y optimizado (solo guarda si es alguien totalmente nuevo)
    if user_id not in usuarios_registrados:
        usuarios_registrados.add(user_id)
        save_data()

    if user_id in usuarios_exentos: return  

    if ETIQUETA_REQUERIDA not in (message.from_user.full_name or ""):
        try:
            await message.delete()
            await bot.ban_chat_member(message.chat.id, user_id)
            await bot.unban_chat_member(message.chat.id, user_id)
        except Exception:
            pass

# ================= SERVIDOR WEB (RENDER) Y EJECUCIÓN =================
async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda req: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()

async def main():
    dp.include_router(router)
    asyncio.create_task(web_server())
    print("🤖 Bot Iniciado y Corriendo con persistencia JSON...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())