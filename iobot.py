import asyncio
import re
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument
)
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ================= CONFIGURACIÓN =================
TOKEN = "8515941177:AAHF-I0U5EB-zidhrnGbZVQuAdw13ArQpjU"
BACKUP_CHANNEL_ID = -1003986866749  # ID DE TU CANAL PRIVADO UNICO

# NUEVO: Lista de IDs de usuarios designados (Super Admins)
DESIGNATED_USERS = {8983189714, 8764734838}

LINK_REGEX = re.compile(r'(https?://|www\.|t\.me/)', re.IGNORECASE)

active_groups = {}          
authorized_users = {}       
album_cache = {}  # Memoria temporal para agrupar álbumes multimedia

# Cachés para nuevas funciones
promoted_contributors = set()  # Guarda tuplas (chat_id, user_id)
media_counts = {}              # Conteo de archivos por usuario {user_id: {"name": str, "count": int}}

# ================= INICIALIZACIÓN =================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

class BotStates(StatesGroup):
    waiting_for_id = State()

async def is_admin(chat_id: int, user_id: int) -> bool:
    # 1. NUEVO: Verificar si es un usuario designado (ignora si es admin en el grupo o no)
    if user_id in DESIGNATED_USERS:
        return True
        
    # 2. Verificar si fue autorizado por el panel temporal
    if chat_id in authorized_users and user_id in authorized_users[chat_id]:
        return True
        
    # 3. Verificar en Telegram si es administrador o creador del grupo
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except:
        return False

# ================= TECLADOS EN LÍNEA =================
def get_main_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔒 Cerrar Chat", callback_data=f"close_{group_id}"),
            InlineKeyboardButton(text="🔓 Abrir Chat", callback_data=f"open_{group_id}")
        ],
        [
            InlineKeyboardButton(text="👥 Autorizar ID", callback_data=f"addid_{group_id}"),
            InlineKeyboardButton(text="ℹ️ Ayuda", callback_data=f"help_{group_id}")
        ]
    ])

def get_back_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Volver al Panel", callback_data=f"back_{group_id}")]
    ])

# ================= COMANDOS EN GRUPO =================

@router.message(Command("panel"))
async def link_group_panel(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        if await is_admin(message.chat.id, message.from_user.id):
            active_groups[message.from_user.id] = message.chat.id
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⚙️ Abrir Panel", url=f"t.me/{(await bot.me()).username}?start=panel")
            ]])
            await message.reply("Panel de control listo:", reply_markup=kb)
        else:
            await message.reply("❌ No tienes permisos.")

@router.message(Command("info"))
async def group_info_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        if await is_admin(message.chat.id, message.from_user.id):
            try:
                chat = await message.bot.get_chat(message.chat.id)
                member_count = await message.bot.get_chat_member_count(message.chat.id)
                
                info_text = (
                    f"📊 **Información del Grupo**\n\n"
                    f"🏷️ **Nombre:** {chat.title}\n"
                    f"🆔 **ID del Grupo:** `{chat.id}`\n"
                    f"👥 **Miembros Totales:** {member_count}\n"
                    f"📝 **Descripción:** {chat.description or 'Sin descripción'}"
                )
                await message.reply(info_text, parse_mode="Markdown")
                await message.delete()
            except Exception as e:
                await message.reply(f"❌ Error al obtener la información: {e}")

# --- COMANDOS DE MODERACIÓN ---
@router.message(Command("del"))
async def delete_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
                await message.delete()
            except:
                pass

@router.message(Command("ban"))
async def ban_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        if message.reply_to_message:
            user_to_ban = message.reply_to_message.from_user.id
            try:
                await bot.ban_chat_member(message.chat.id, user_to_ban)
                await message.reply_to_message.delete()
                confirm = await message.answer(f"🔨 Usuario baneado por {message.from_user.first_name}.")
                await message.delete()
                await asyncio.sleep(5)
                await confirm.delete()
            except:
                pass

@router.message(Command("unban"))
async def unban_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        user_to_unban = None
        if message.reply_to_message:
            user_to_unban = message.reply_to_message.from_user.id
        else:
            args = message.text.split()
            if len(args) > 1 and args[1].isdigit():
                user_to_unban = int(args[1])
                
        if user_to_unban:
            try:
                await bot.unban_chat_member(message.chat.id, user_to_unban)
                confirm = await message.answer(f"✅ Usuario desbaneado con éxito.")
                await message.delete()
                await asyncio.sleep(5)
                await confirm.delete()
            except:
                pass

# --- COMANDO REPETIR Y BORRAR (/s O .s) ---
@router.message(F.text.startswith("/s ") | F.text.startswith(".s "))
async def repeat_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        if await is_admin(message.chat.id, message.from_user.id):
            text_to_send = message.text[3:].strip()
            if text_to_send:
                try:
                    await message.answer(text_to_send)
                    await message.delete()
                except:
                    pass

# --- COMANDOS DE CONTEO (ESTADÍSTICAS) ---
@router.message(Command("aportes"))
async def check_stats_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
        data = media_counts.get(target.id, {"count": 0})
        await message.reply(
            f"📊 **{target.first_name}** ha aportado **{data['count']}** archivos multimedia al grupo.", 
            parse_mode="Markdown"
        )

@router.message(Command("topaportes"))
async def top_stats_cmd(message: Message):
    if not media_counts:
        return await message.reply("📉 Aún no hay aportes registrados en esta sesión.")
    
    sorted_counts = sorted(media_counts.values(), key=lambda x: x["count"], reverse=True)[:10]
    text = "🏆 **Top 10 Aportadores:**\n\n"
    for i, data in enumerate(sorted_counts, 1):
        text += f"{i}. {data['name']} - {data['count']} aportes\n"
    
    await message.reply(text, parse_mode="Markdown")

# ================= CHAT PRIVADO (PANEL) =================
@router.message(CommandStart())
async def start_private_panel(message: Message, state: FSMContext):
    if message.chat.type == "private":
        await state.clear()
        group_id = active_groups.get(message.from_user.id)
        if group_id:
            chat_info = await bot.get_chat(group_id)
            await message.answer(
                f"⚙️ **Panel de Control:** {chat_info.title}\nElige una opción:",
                reply_markup=get_main_keyboard(group_id),
                parse_mode="Markdown"
            )
        else:
            await message.answer("Usa /panel dentro de un grupo primero para vincularlo.")

@router.message(BotStates.waiting_for_id)
async def process_new_id(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id = data.get("group_id")
    panel_msg_id = data.get("panel_msg_id")
    await message.delete() 
    
    try:
        new_id = int(message.text.strip())
        if group_id not in authorized_users:
            authorized_users[group_id] = set()
        authorized_users[group_id].add(new_id)
        
        await bot.edit_message_text(
            f"✅ **ID {new_id} autorizado con éxito.**",
            chat_id=message.chat.id,
            message_id=panel_msg_id,
            reply_markup=get_main_keyboard(group_id),
            parse_mode="Markdown"
        )
    except ValueError:
        pass
    finally:
        await state.clear()

@router.callback_query(F.data.startswith("back_"))
async def back_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    group_id = int(callback.data.split("_")[1])
    await callback.message.edit_text("⚙️ **Panel Principal**", reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")

@router.callback_query(F.data.startswith("help_"))
async def help_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    help_text = (
        "📚 **Guía de Uso del Bot:**\n\n"
        "🔸 **Anti-Links:** Borra automáticamente mensajes con enlaces.\n"
        "🔸 **Respaldo Único:** Copia fotos, videos y archivos al canal.\n"
        "🔸 **Conteo:** Usa `/aportes` o `/topaportes` para ver estadísticas.\n"
        "🔸 **/s o .s [mensaje]:** El bot repite el mensaje y borra el tuyo.\n"
        "🔸 **/info:** Muestra estadísticas del grupo.\n"
        "🔸 **/del, /ban y /unban:** Comandos de moderación.\n"
    )
    await callback.message.edit_text(help_text, reply_markup=get_back_keyboard(group_id), parse_mode="Markdown")

@router.callback_query(F.data.startswith("close_"))
async def close_chat_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        await bot.set_chat_permissions(group_id, ChatPermissions(can_send_messages=False))
        await callback.answer("Chat cerrado.", show_alert=True)
    except:
        pass

@router.callback_query(F.data.startswith("open_"))
async def open_chat_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        await bot.set_chat_permissions(group_id, ChatPermissions(
            can_send_messages=True, can_send_photos=True, can_send_videos=True, can_send_other_messages=True
        ))
        await callback.answer("Chat abierto.", show_alert=True)
    except:
        pass

@router.callback_query(F.data.startswith("addid_"))
async def addid_cb(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_for_id)
    await state.update_data(group_id=group_id, panel_msg_id=callback.message.message_id)
    await callback.message.edit_text("✍️ **Envía un mensaje con el ID numérico del usuario.**", reply_markup=get_back_keyboard(group_id), parse_mode="Markdown")

# ================= FUNCIONES DE ÁLBUM =================
async def process_album(media_group_id: str, chat_title: str):
    """Espera a que lleguen todas las fotos/videos del álbum y las envía juntas al canal."""
    await asyncio.sleep(3)  
    
    if media_group_id not in album_cache:
        return
        
    messages = album_cache.pop(media_group_id)
    media_group = []
    
    for idx, msg in enumerate(messages):
        caption = None
        if idx == 0:
            orig_cap = msg.caption or ""
            sig = f"📌 Enviado desde: {chat_title}"
            caption = f"{orig_cap}\n\n{sig}" if orig_cap else sig
        
        if msg.photo:
            media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=caption))
        elif msg.video:
            media_group.append(InputMediaVideo(media=msg.video.file_id, caption=caption))
        elif msg.document:
            media_group.append(InputMediaDocument(media=msg.document.file_id, caption=caption))
            
    if media_group:
        try:
            await bot.send_media_group(BACKUP_CHANNEL_ID, media=media_group)
        except Exception as e:
            logging.error(f"Error copiando álbum al canal: {e}")

# ================= FILTRO GLOBAL =================
@router.message()
async def group_messages_processor(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        # 1. Filtro de enlaces (Anti-Spam)
        content = message.text or message.caption
        if content and LINK_REGEX.search(content):
            if not await is_admin(message.chat.id, message.from_user.id):
                try:
                    await message.delete()
                    return
                except:
                    pass
        
        # 2. Filtro de Respaldo Multimedia (SOLO fotos, videos y documentos)
        if message.photo or message.video or message.document:
            user_id = message.from_user.id
            chat_id = message.chat.id
            
            # --- LÓGICA DE CONTEO Y ETIQUETA ---
            if user_id not in media_counts:
                media_counts[user_id] = {"name": message.from_user.first_name, "count": 0}
            media_counts[user_id]["count"] += 1
            
            # Promover a "Aportador" sutilmente si no es admin (los designados cuentan como admin y se saltan esto)
            if not await is_admin(chat_id, user_id):
                if (chat_id, user_id) not in promoted_contributors:
                    try:
                        await bot.promote_chat_member(
                            chat_id, user_id, 
                            can_manage_chat=True,
                            can_change_info=False,
                            can_delete_messages=False,
                            can_invite_users=False,
                            can_restrict_members=False,
                            can_pin_messages=False,
                            can_manage_video_chats=False,
                            can_promote_members=False
                        )
                        await bot.set_chat_administrator_custom_title(chat_id, user_id, "Aportador")
                        promoted_contributors.add((chat_id, user_id))
                    except Exception as e:
                        logging.error(f"No se pudo promover a Aportador al usuario {user_id}: {e}")

            # --- LÓGICA DE RESPALDO MULTIMEDIA ---
            if message.media_group_id:
                group_id = message.media_group_id
                if group_id not in album_cache:
                    album_cache[group_id] = []
                    asyncio.create_task(process_album(group_id, message.chat.title))
                
                album_cache[group_id].append(message)
            
            else:
                try:
                    original_caption = message.caption or ""
                    group_signature = f"📌 Enviado desde: {message.chat.title}"
                    new_caption = f"{original_caption}\n\n{group_signature}" if original_caption else group_signature

                    await message.copy_to(BACKUP_CHANNEL_ID, caption=new_caption)
                except Exception as e:
                    logging.error(f"Error copiando archivo suelto: {e}")

# ================= SERVIDOR WEB FALSO PARA RENDER =================
async def handle(request):
    return web.Response(text="Bot is running smoothly!")

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
    # Ejecutamos el server aiohttp en background
    asyncio.create_task(web_server())
    print("🤖 Bot Web Service iniciado y corriendo en puerto 10000...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())