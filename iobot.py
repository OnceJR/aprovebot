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
    
    if ETIQUETA_REQUERIDA in user_name:
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
@router.message(CommandStart())
async def start_cmd(message: Message):
    if message.chat.type == "private":
        usuarios_registrados.add(message.from_user.id)
        
        # Filtro de acceso total solo para el Super Admin
        if message.from_user.id == SUPER_ADMIN_ID:
            total = len(usuarios_registrados)
            admin_text = (
                "⚙️ **Panel de Configuración Principal**\n\n"
                "Eres el único administrador con acceso a este panel.\n\n"
                f"📊 **Usuarios únicos detectados:** `{total}`\n"
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

@router.message(Command("stats"))
async def bot_stats_cmd(message: Message):
    """Comando rápido para verificar el tráfico (Solo Super Admin)."""
    if message.chat.type == "private" and message.from_user.id == SUPER_ADMIN_ID:
        total = len(usuarios_registrados)
        await message.answer(f"📊 **Tráfico actual:** `{total}` usuarios únicos registrados.", parse_mode="Markdown")

# ================= FILTRO GLOBAL (PATRULLAJE ESTRICTO EN GRUPOS) =================
@router.message()
async def group_messages_processor(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        user_id = message.from_user.id
        user_name = message.from_user.full_name or ""
        
        usuarios_registrados.add(user_id)
        
        # El Super Admin tiene inmunidad total
        if user_id == SUPER_ADMIN_ID:
            return  

        # PATRULLAJE ACTIVO: Si escribe y no tiene la etiqueta, se va.
        if ETIQUETA_REQUERIDA not in user_name:
            try:
                await bot.ban_chat_member(message.chat.id, user_id)
                await message.delete()
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
    await web_server()
    print("🤖 Bot Iniciado y Corriendo...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())