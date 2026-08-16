import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

# ================= CONFIGURACIÓN =================
TOKEN = "8985157561:AAEP2XkXV86iSSNqpawYqfqIuY2ApmBu4o8"
ETIQUETA_REQUERIDA = "ᴼᵀᴹ"

# 👇 AQUÍ AGREGAS TODOS LOS SUPER ADMINS (Separados por comas)
SUPER_ADMIN_IDS = {8983189714, 8764734838} 

usuarios_registrados = set() # Memoria para contar usuarios únicos
usuarios_exentos = {8748956307, 8764734838, 6630522163, 8831263313, 8556221763, 5142196200, 7452819858, 8803304819, 8266066936, 8985586526} # Memoria para usuarios inmunes al filtro

# Aseguramos que TODOS los Super Admins nunca sean expulsados
usuarios_exentos.update(SUPER_ADMIN_IDS) 

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

# ================= FILTRO DE ADMISIÓN (SOLICITUDES) =================
@router.chat_join_request()
async def process_join_request(join_request: ChatJoinRequest):
    """Aprueba o rechaza automáticamente las solicitudes de ingreso."""
    user_name = join_request.from_user.full_name or ""
    user_id = join_request.from_user.id
    
    # Se registra el tráfico
    usuarios_registrados.add(user_id)
    
    # Si el usuario tiene la etiqueta O ESTÁ EN LA LISTA DE EXENTOS, se aprueba
    if ETIQUETA_REQUERIDA in user_name or user_id in usuarios_exentos:
        try:
            await join_request.approve()
        except:
            pass
    else:
        try:
            await join_request.decline()
        except:
            pass

# ================= CHAT PRIVADO (PANEL ADMIN VS USUARIOS) =================
@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
    await state.clear() # Limpia cualquier estado pendiente
    usuarios_registrados.add(message.from_user.id)
    
    # Filtro de acceso total para los Super Admins
    if message.from_user.id in SUPER_ADMIN_IDS:
        admin_text = (
            "👑 **PANEL DE CONTROL PRINCIPAL**\n\n"
            "Bienvenido al sistema de administración. Selecciona una opción del menú interactivo:"
        )
        await message.answer(admin_text, parse_mode="Markdown", reply_markup=obtener_teclado_admin())
    else:
        # Vista para los usuarios comunes (Mensaje oficial de admisión)
        welcome_text = (
            "🏛 **SISTEMA OFICIAL DE ADMISIÓN**\n\n"
            "Estimado usuario, sea bienvenido al portal de ingreso. "
            "Para asegurar la calidad y exclusividad de nuestra comunidad, operamos bajo un estricto filtro de seguridad.\n\n"
            f"⚠️ **REQUISITO OBLIGATORIO:**\n"
            f"Para que su solicitud de ingreso al Grupo de Aportes sea aprobada, es indispensable que agregue la etiqueta `{ETIQUETA_REQUERIDA}` a su nombre de Telegram.\n\n"
            "📌 **Instrucciones:**\n"
            "1. Copie la etiqueta del mensaje inferior.\n"
            "2. Vaya a los Ajustes de Telegram > Editar perfil.\n"
            "3. Péguela en su nombre o apellido.\n"
            "4. Solicite unirse mediante el enlace de invitación.\n\n"
            "⛔️ *Nota:* El sistema monitorea constantemente a los usuarios. Si usted retira esta etiqueta de su nombre una vez dentro de la comunidad, será expulsado automáticamente de forma irrevocable."
        )
        await message.answer(welcome_text, parse_mode="Markdown")
        
        # Mensaje separado para copiar fácilmente con un toque
        await message.answer(
            f"👇 **Toque la etiqueta para copiarla:**\n\n`{ETIQUETA_REQUERIDA}`", 
            parse_mode="Markdown"
        )

# ================= CALLBACKS DEL MENÚ ADMIN =================
@router.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext):
    # Verificamos que sea uno de los Super Admins
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("No tienes permisos.", show_alert=True)
        return

    action = callback.data.split("_")[1]

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
            "*(La memoria cuenta desde el último reinicio)*"
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
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
        
    try:
        target_id = int(message.text.strip())
        usuarios_exentos.add(target_id)
        await message.answer(
            f"✅ **¡Listo!** El ID `{target_id}` ha sido añadido a las excepciones.", 
            parse_mode="Markdown",
            reply_markup=obtener_teclado_admin()
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ **Error:** Debes enviar un ID numérico válido. Inténtalo de nuevo o cancela.", reply_markup=obtener_teclado_cancelar())

@router.message(StateFilter(AdminPanel.esperando_mensaje_difusion), F.chat.type == "private")
async def recibir_mensaje_difusion(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
        
    await message.answer("⏳ **Iniciando difusión masiva...**")
    await state.clear()
    
    exitos = 0
    fallos = 0
    
    for user_id in usuarios_registrados:
        try:
            await message.copy_to(chat_id=user_id)
            exitos += 1
            await asyncio.sleep(0.05) # Pausa mínima para no saturar la API de Telegram
        except TelegramAPIError:
            fallos += 1
            
    resumen = (
        "📢 **DIFUSIÓN FINALIZADA**\n\n"
        f"✅ Entregados con éxito: `{exitos}`\n"
        f"❌ Fallidos (Bot bloqueado): `{fallos}`"
    )
    await message.answer(resumen, parse_mode="Markdown", reply_markup=obtener_teclado_admin())

# ================= COMANDOS DE GRUPO (APORTADOR) =================
@router.message(Command("aportador"), F.chat.type.in_(["group", "supergroup"]))
async def dar_etiqueta_aportador(message: Message):
    """Otorga título personalizado y añade a excepciones."""
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    
    if not message.reply_to_message:
        await message.reply("⚠️ Responde al mensaje del usuario que será Aportador.")
        return

    target_user = message.reply_to_message.from_user
    
    try:
        # Promover sin poderes reales para poder darle título
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
        usuarios_exentos.add(target_user.id) # Se vuelve inmune al filtro
        await message.reply(f"✅ {target_user.first_name} ahora tiene la etiqueta de Aportador y está exento del filtro.")
    except Exception as e:
        await message.reply(f"❌ Error (Verifica que el bot tenga permisos de 'Añadir Admins'): {e}")

@router.message(Command("quitar_aportador"), F.chat.type.in_(["group", "supergroup"]))
async def quitar_etiqueta_aportador(message: Message):
    """Revoca el título personalizado y elimina de excepciones."""
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    
    if not message.reply_to_message:
        await message.reply("⚠️ Responde al mensaje del usuario para quitarle el rol.")
        return

    target_user = message.reply_to_message.from_user
    
    try:
        # Remover status de administrador
        await bot.promote_chat_member(
            chat_id=message.chat.id, user_id=target_user.id,
            can_manage_chat=False, can_change_info=False,
            can_delete_messages=False, can_invite_users=False,
            can_restrict_members=False, can_pin_messages=False,
            can_promote_members=False
        )
        
        # Le quitamos la excepción si estaba, EXCEPTO si es uno de los Super Admins
        if target_user.id in usuarios_exentos and target_user.id not in SUPER_ADMIN_IDS:
            usuarios_exentos.remove(target_user.id)
            
        await message.reply(f"✅ Se le quitó el rol a {target_user.first_name}. Volverá a ser revisado por el filtro.")
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

# ================= FILTRO GLOBAL (PATRULLAJE ESTRICTO EN GRUPOS) =================
@router.message(F.chat.type.in_(["group", "supergroup"]))
async def group_messages_processor(message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.full_name or ""
    
    usuarios_registrados.add(user_id)
    
    # Si el usuario está exento (Es admin principal o es Aportador), lo ignoramos
    if user_id in usuarios_exentos:
        return  

    # PATRULLAJE ACTIVO: Si escribe y no tiene la etiqueta
    if ETIQUETA_REQUERIDA not in user_name:
        try:
            await message.delete()
            # Kick (Banear y Desbanear) para que puedan intentar volver
            await bot.ban_chat_member(message.chat.id, user_id)
            await bot.unban_chat_member(message.chat.id, user_id)
        except:
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
    # Iniciamos el servidor web falso en segundo plano
    asyncio.create_task(web_server())
    print("🤖 Bot Iniciado y Corriendo...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())