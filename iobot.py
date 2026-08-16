import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ChatJoinRequest
from aiogram.filters import Command, CommandStart

# ================= CONFIGURACIÓN =================
TOKEN = "8985157561:AAEP2XkXV86iSSNqpawYqfqIuY2ApmBu4o8"
ETIQUETA_REQUERIDA = "ᴼᵀᴹ"
SUPER_ADMIN_ID = 8983189714  # <-- Tu ID para acceso exclusivo

usuarios_registrados = set() # Memoria para contar usuarios únicos
usuarios_exentos = {8748956307, 8764734838, 6630522163, 8831263313, 8556221763, 5142196200, 7452819858, 8803304819, 8266066936, 8985586526} # Memoria para usuarios inmunes al filtro

# ================= INICIALIZACIÓN =================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

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
async def start_cmd(message: Message):
    usuarios_registrados.add(message.from_user.id)
    
    # Filtro de acceso total solo para el Super Admin
    if message.from_user.id == SUPER_ADMIN_ID:
        total = len(usuarios_registrados)
        exentos_total = len(usuarios_exentos)
        admin_text = (
            "⚙️ **Panel de Configuración Principal**\n\n"
            "Eres el único administrador con acceso a este panel.\n\n"
            f"📊 **Usuarios únicos detectados:** `{total}`\n"
            f"🛡 **Usuarios con excepciones:** `{exentos_total}`\n"
            f"*(Desde el último reinicio del servidor)*\n\n"
            "El bot está configurado para patrullar de forma estricta "
            "y expulsar a cualquier miembro que no tenga la etiqueta "
            f"`{ETIQUETA_REQUERIDA}` en su nombre."
        )
        await message.answer(admin_text, parse_mode="Markdown")
    else:
        # Vista para los usuarios comunes (Mensaje oficial de admisión)
        welcome_text = (
            "🏛 **SISTEMA OFICIAL DE ADMISIÓN**\n\n"
            "Estimado usuario, sea bienvenido al portal de ingreso. "
            "Para asegurar la calidad y exclusividad de nuestra comunidad, operamos bajo un estricto filtro de seguridad.\n\n"
            f"⚠️ **REQUISITO OBLIGATORIO:**\n"
            f"Para que su solicitud de ingreso al Grupo de Aportes sea aprobada, es indispensable que agregue la etiqueta `{ETIQUETA_REQUERIDA}` a su nombre de Telegram.\n\n"
            "📌 **Instrucciones:**\n"
            "1. Vaya a los Ajustes de Telegram > Editar perfil.\n"
            f"2. Añada `{ETIQUETA_REQUERIDA}` a su nombre o apellido.\n"
            "3. Solicite unirse mediante el enlace de invitación.\n\n"
            "⛔️ *Nota:* El sistema monitorea constantemente a los usuarios. Si usted retira esta etiqueta de su nombre una vez dentro de la comunidad, será expulsado automáticamente de forma irrevocable."
        )
        await message.answer(welcome_text, parse_mode="Markdown")

@router.message(Command("stats"), F.chat.type == "private")
async def bot_stats_cmd(message: Message):
    """Comando rápido para verificar el tráfico (Solo Super Admin)."""
    if message.from_user.id == SUPER_ADMIN_ID:
        total = len(usuarios_registrados)
        await message.answer(f"📊 **Tráfico actual:** `{total}` usuarios únicos registrados.", parse_mode="Markdown")

@router.message(Command("exempt"), F.chat.type == "private")
async def add_exempt_cmd(message: Message):
    """Comando para añadir excepciones mediante el ID."""
    if message.from_user.id == SUPER_ADMIN_ID:
        try:
            target_id = int(message.text.split()[1])
            usuarios_exentos.add(target_id)
            await message.answer(f"✅ Excepción añadida para el ID: `{target_id}`. No será expulsado.", parse_mode="Markdown")
        except (IndexError, ValueError):
            await message.answer("⚠️ Uso incorrecto. Formato: `/exempt 123456789`")

# ================= COMANDOS DE GRUPO (APORTADOR) =================
@router.message(Command("aportador"), F.chat.type.in_(["group", "supergroup"]))
async def dar_etiqueta_aportador(message: Message):
    """Otorga título personalizado y añade a excepciones."""
    if message.from_user.id != SUPER_ADMIN_ID:
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
    if message.from_user.id != SUPER_ADMIN_ID:
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
        
        if target_user.id in usuarios_exentos and target_user.id != SUPER_ADMIN_ID:
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