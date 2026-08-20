import asyncio
import logging
import json
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

# ================= CONFIGURACIÓN =================
TOKEN = "8783353791:AAF0wQHXBeRzBrovC3hisyxOUOUuspUgyTs"
ETIQUETA_REQUERIDA = "ᴼᵀᴹ"
DATA_FILE = "users.json" # Archivo para guardar la memoria persistente

# 👇 AQUÍ AGREGAS TODOS LOS SUPER ADMINS (Separados por comas)
SUPER_ADMIN_IDS = {8983189714, 8764734838} 
ADMINS_FIJOS = {8748956307, 8764734838, 6630522163, 8831263313, 8556221763, 5142196200, 7452819858, 8803304819, 8266066936, 8985586526}

# ================= PERSISTENCIA DE DATOS (JSON) =================
def load_data():
    """Carga los usuarios desde el archivo JSON o crea listas vacías si no existe."""
    if not os.path.exists(DATA_FILE):
        return set(), set(ADMINS_FIJOS | SUPER_ADMIN_IDS)
    
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            return set(data.get("registrados", [])), set(data.get("exentos", [])) | ADMINS_FIJOS | SUPER_ADMIN_IDS
    except json.JSONDecodeError:
        return set(), set(ADMINS_FIJOS | SUPER_ADMIN_IDS)

def save_data():
    """Guarda los usuarios registrados y exentos en el archivo JSON."""
    with open(DATA_FILE, "w") as f:
        json.dump({
            "registrados": list(usuarios_registrados),
            "exentos": list(usuarios_exentos)
        }, f)

# Inicializamos las listas cargando desde el archivo JSON
usuarios_registrados, usuarios_exentos = load_data()

# ================= INICIALIZACIÓN =================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

# ================= ESTADOS PARA EL MENÚ (FSM) =================
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

# ================= FILTRO DE ADMISIÓN (CAPTCHA) =================
@router.chat_join_request()
async def process_join_request(join_request: ChatJoinRequest):
    """Filtra y envía el Captcha al privado del usuario."""
    user_name = join_request.from_user.full_name or ""
    user_id = join_request.from_user.id
    chat_id = join_request.chat.id
    
    # Registramos el tráfico y guardamos
    if user_id not in usuarios_registrados:
        usuarios_registrados.add(user_id)
        save_data()
    
    if ETIQUETA_REQUERIDA in user_name or user_id in usuarios_exentos:
        # En vez de aceptar automático, enviamos Captcha
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Soy humano y tengo el tag", callback_data=f"captcha_{chat_id}_{user_id}")]
        ])
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🛡 **VERIFICACIÓN DE SEGURIDAD**\n\nHola {join_request.from_user.first_name}, hemos recibido tu solicitud. Por favor, confirma que eres humano y que posees la etiqueta `{ETIQUETA_REQUERIDA}` presionando el botón de abajo.",
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except TelegramAPIError:
            # Si el usuario no ha iniciado el bot en privado, no podemos verificarlo. Se rechaza.
            try:
                await join_request.decline()
            except TelegramAPIError:
                pass
    else:
        # No tiene el tag, rechazo automático
        try:
            await join_request.decline()
        except TelegramAPIError:
            pass

@router.callback_query(F.data.startswith("captcha_"))
async def handle_captcha_approval(callback: CallbackQuery):
    """Aprueba la solicitud cuando el usuario presiona el botón."""
    # Extraemos el ID del grupo y del usuario ocultos en el callback data
    data = callback.data.split("_")
    chat_id = int(data[1])
    user_id = int(data[2])
    
    # Evitamos que alguien presione el botón de otro
    if callback.from_user.id != user_id:
        await callback.answer("❌ Este botón no es para ti.", show_alert=True)
        return

    try:
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        await callback.message.edit_text("✅ **¡Verificación completada!**\nTu solicitud ha sido aprobada. ¡Bienvenido al grupo!", parse_mode="Markdown")
    except TelegramAPIError:
        await callback.message.edit_text("❌ **Error.** O ya fuiste aceptado, o tu solicitud expiró. Intenta unirte de nuevo.", parse_mode="Markdown")

# ================= CHAT PRIVADO (PANEL ADMIN VS USUARIOS) =================
@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    
    if message.from_user.id not in usuarios_registrados:
        usuarios_registrados.add(message.from_user.id)
        save_data()
    
    # Filtro de acceso para Super Admins
    if message.from_user.id in SUPER_ADMIN_IDS:
        admin_text = (
            "👑 **PANEL DE CONTROL PRINCIPAL**\n\n"
            "Bienvenido al sistema de administración. Selecciona una opción del menú interactivo:"
        )
        await message.answer(admin_text, parse_mode="Markdown", reply_markup=obtener_teclado_admin())
    else:
        # Vista para usuarios comunes
        welcome_text = (
            "🏛 **SISTEMA OFICIAL DE ADMISIÓN**\n\n"
            "Estimado usuario, sea bienvenido al portal de ingreso. "
            "Para asegurar la calidad y exclusividad de nuestra comunidad, operamos bajo un estricto filtro de seguridad.\n\n"
            f"⚠️ **REQUISITO OBLIGATORIO:**\n"
            f"Para que su solicitud de ingreso sea aprobada, es indispensable que agregue la etiqueta `{ETIQUETA_REQUERIDA}` a su nombre de Telegram.\n\n"
            "📌 **Instrucciones:**\n"
            "1. Copie la etiqueta del mensaje inferior.\n"
            "2. Vaya a los Ajustes de Telegram > Editar perfil.\n"
            "3. Péguela en su nombre o apellido.\n"
            "4. Solicite unirse mediante el enlace de invitación.\n\n"
            "⛔️ *Nota:* El sistema monitorea constantemente. Si retira la etiqueta una vez dentro, será expulsado automáticamente."
        )
        await message.answer(welcome_text, parse_mode="Markdown")
        await message.answer(f"👇 **Toque la etiqueta para copiarla:**\n\n`{ETIQUETA_REQUERIDA}`", parse_mode="Markdown")

# ================= CALLBACKS DEL MENÚ ADMIN =================
@router.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("No tienes permisos.", show_alert=True)
        return

    action = callback.data.replace("admin_", "")

    if action == "close":
        await callback.message.delete()
        await state.clear()
        
    elif action == "cancel":
        await callback.message.edit_text(
            "✅ **Operación cancelada.**\n¿Qué deseas hacer ahora?", 
            parse_mode="Markdown", 
            reply_markup=obtener_teclado_admin()
        )
        await state.clear()
        
    elif action == "stats":
        total = len(usuarios_registrados)
        exentos = len(usuarios_exentos)
        texto_stats = (
            "📊 **ESTADÍSTICAS DEL BOT**\n\n"
            f"👥 **Usuarios Registrados (Tráfico):** `{total}`\n"
            f"🛡 **Usuarios Inmunes:** `{exentos}`\n\n"
            "*(Datos respaldados en memoria JSON)*"
        )
        await callback.message.edit_text(texto_stats, parse_mode="Markdown", reply_markup=obtener_teclado_admin())
        
    elif action == "add_exempt":
        await callback.message.edit_text(
            "🛡 **AÑADIR EXCEPCIÓN**\n\n"
            "Envíame el **ID Numérico** del usuario que deseas volver inmune al filtro.",
            parse_mode="Markdown",
            reply_markup=obtener_teclado_cancelar()
        )
        await state.set_state(AdminPanel.esperando_id_excepcion)
        
    elif action == "broadcast":
        await callback.message.edit_text(
            "📢 **ENVIAR DIFUSIÓN GLOBAL**\n\n"
            f"Este mensaje se enviará a los **{len(usuarios_registrados)}** usuarios registrados.\n\n"
            "Envíame el mensaje que deseas difundir (texto, foto, video o documento).",
            parse_mode="Markdown",
            reply_markup=obtener_teclado_cancelar()
        )
        await state.set_state(AdminPanel.esperando_mensaje_difusion)

# ================= CAPTURA DE ESTADOS (FSM) =================
@router.message(StateFilter(AdminPanel.esperando_id_excepcion), F.chat.type == "private")
async def recibir_id_excepcion(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
        
    try:
        target_id = int(message.text.strip())
        usuarios_exentos.add(target_id)
        save_data() # Guardamos en JSON
        
        await message.answer(
            f"✅ **¡Listo!** El ID `{target_id}` ha sido añadido a las excepciones.", 
            parse_mode="Markdown",
            reply_markup=obtener_teclado_admin()
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ **Error:** Debes enviar un ID numérico válido.", reply_markup=obtener_teclado_cancelar())

@router.message(StateFilter(AdminPanel.esperando_mensaje_difusion), F.chat.type == "private")
async def recibir_mensaje_difusion(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
        
    await message.answer("⏳ **Iniciando difusión masiva...** (Esto puede tardar según el volumen)")
    await state.clear()
    
    exitos = 0
    fallos = 0
    
    # Iteramos sobre la lista persistente cargada desde el JSON
    for user_id in usuarios_registrados:
        try:
            await message.copy_to(chat_id=user_id)
            exitos += 1
            await asyncio.sleep(0.05) # Límite de seguridad de Telegram
        except TelegramAPIError:
            fallos += 1
            
    resumen = (
        "📢 **DIFUSIÓN FINALIZADA**\n\n"
        f"✅ Entregados con éxito: `{exitos}`\n"
        f"❌ Fallidos (Bot bloqueado/Chat eliminado): `{fallos}`"
    )
    await message.answer(resumen, parse_mode="Markdown", reply_markup=obtener_teclado_admin())

# ================= COMANDOS DE GRUPO (APORTADOR) =================
@router.message(Command("aportador"), F.chat.type.in_(["group", "supergroup"]))
async def dar_etiqueta_aportador(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    
    if not message.reply_to_message:
        await message.reply("⚠️ Responde al mensaje del usuario que será Aportador.")
        return

    target_user = message.reply_to_message.from_user
    try:
        await bot.promote_chat_member(
            chat_id=message.chat.id, user_id=target_user.id,
            can_manage_chat=True, can_change_info=False,
            can_delete_messages=False, can_invite_users=False,
            can_restrict_members=False, can_pin_messages=False,
            can_promote_members=False
        )
        await bot.set_chat_administrator_custom_title(
            chat_id=message.chat.id, user_id=target_user.id,
            custom_title="Aportador 💎"
        )
        usuarios_exentos.add(target_user.id)
        save_data() # Guardamos en JSON
        
        await message.reply(f"✅ {target_user.first_name} ahora tiene la etiqueta de Aportador y está exento del filtro.")
    except Exception as e:
        await message.reply(f"❌ Error (Verifica permisos de 'Añadir Admins'): {e}")

@router.message(Command("quitar_aportador"), F.chat.type.in_(["group", "supergroup"]))
async def quitar_etiqueta_aportador(message: Message):
    if message.from_user.id not in SUPER_ADMIN_IDS: return
    
    if not message.reply_to_message:
        await message.reply("⚠️ Responde al mensaje del usuario para quitarle el rol.")
        return

    target_user = message.reply_to_message.from_user
    try:
        await bot.promote_chat_member(
            chat_id=message.chat.id, user_id=target_user.id,
            can_manage_chat=False, can_change_info=False,
            can_delete_messages=False, can_invite_users=False,
            can_restrict_members=False, can_pin_messages=False,
            can_promote_members=False
        )
        
        if target_user.id in usuarios_exentos and target_user.id not in SUPER_ADMIN_IDS:
            usuarios_exentos.remove(target_user.id)
            save_data() # Guardamos en JSON
            
        await message.reply(f"✅ Se le quitó el rol a {target_user.first_name}. Volverá a ser revisado por el filtro.")
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

# ================= FILTRO GLOBAL (PATRULLAJE) =================
@router.message(F.chat.type.in_(["group", "supergroup"]))
async def group_messages_processor(message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.full_name or ""
    
    # Registrar usuarios nuevos (se usa un condicional previo para evitar escrituras masivas en JSON por cada mensaje)
    if user_id not in usuarios_registrados:
        usuarios_registrados.add(user_id)
        save_data()

    if user_id in usuarios_exentos:
        return  

    if ETIQUETA_REQUERIDA not in user_name:
        try:
            await message.delete()
            await bot.ban_chat_member(message.chat.id, user_id)
            await bot.unban_chat_member(message.chat.id, user_id)
        except Exception:
            pass

# ================= SERVIDOR WEB FALSO PARA RENDER =================
async def handle(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()

# ================= EJECUCIÓN PRINCIPAL =================
async def main():
    dp.include_router(router)
    asyncio.create_task(web_server())
    print("🤖 Bot Iniciado y Corriendo con persistencia JSON...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())