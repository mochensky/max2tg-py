import asyncio
import json
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiofiles
import aiohttp
import aiosqlite
import pytz
from dotenv import load_dotenv

# https://github.com/mochensky/max-user-api
from max_user_api import Client, Config, UserAgentConfig, Message
from max_user_api.models import ControlAttachment, PhotoAttachment, VideoAttachment, FileAttachment
from max_user_api.enums import MessageStatus

os.makedirs("data", exist_ok=True)

logging.getLogger('aiosqlite').setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("data/main.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)
load_dotenv()

MAX_TOKEN = os.getenv("MAX_TOKEN")
MAX_DEVICE_ID = os.getenv("MAX_DEVICE_ID")
MAX_CHAT_ID = int(os.getenv("MAX_CHAT_ID", 0))
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
TG_DEBUG_USER_ID = os.getenv("TG_DEBUG_USER_ID", None)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"

VIDEO_HEADERS = {
    "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.5",
    "Range": "bytes=0-",
    "Connection": "keep-alive",
    "Referer": "https://web.max.ru/",
    "Cookie": "tstc=p",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
    "DNT": "1",
    "Sec-GPC": "1",
    "Accept-Encoding": "identity",
    "Priority": "u=4"
}

IMAGES_DIR = "data/images"
VIDEOS_DIR = "data/videos"
FILES_DIR = "data/files"
DB_FILE = "data/main.db"

user_names = {}

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)


async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_message_id TEXT UNIQUE,
                tg_message_id INTEGER,
                max_sender_id INTEGER,
                timestamp INTEGER
            )
        ''')
        await db.commit()


async def handle_control_message(message: Message, client) -> Optional[str]:
    if not message.attaches:
        return None

    control = None
    for attach in message.attaches:
        if isinstance(attach, ControlAttachment):
            control = attach
            break
    if not control or control.event not in {"add", "joinByLink", "remove", "leave", "new"}:
        return None

    utc_dt = datetime.fromtimestamp(message.time / 1000, tz=timezone.utc)
    try:
        moscow_dt = utc_dt.astimezone(ZoneInfo("Europe/Moscow"))
    except:
        moscow_dt = utc_dt.astimezone(pytz.timezone("Europe/Moscow"))
    time_str = moscow_dt.strftime("%d.%m.%Y %H:%M:%S")

    async def get_name(uid: int) -> str:
        key = str(uid)
        if key in user_names:
            return user_names[key]
        try:
            contacts = await client.get_contacts([uid])
            if contacts:
                c = contacts[0]
                full = f"{c.first_name} {c.last_name}".strip()
                user_names[key] = full or f"ID{uid}"
                return user_names[key]
        except:
            pass
        user_names[key] = f"ID{uid}"
        return f"ID{uid}"

    if control.event == "joinByLink":
        user_id = control.userId or message.sender_id
        name = await get_name(user_id)
        return f"• {time_str}\n\n{name} присоединился(-ась) к чату"

    elif control.event == "add":
        if not control.userIds:
            return None
        actor_name = await get_name(message.sender_id)
        added_names = []
        for uid in control.userIds:
            added_names.append(await get_name(uid))

        if len(added_names) == 1:
            text = f"{actor_name} добавил(-а) {added_names[0]}"
        else:
            text = f"{actor_name} добавил(-а) {', '.join(added_names[:-1])} и {added_names[-1]}"
        return f"• {time_str}\n\n{text}"

    elif control.event == "remove":
        if not control.userIds:
            return None
        actor_name = await get_name(message.sender_id)
        removed_names = [await get_name(uid) for uid in control.userIds]
        if len(removed_names) == 1:
            text = f"{actor_name} исключил(-а) {removed_names[0]}"
        else:
            text = f"{actor_name} исключил(-а) {', '.join(removed_names[:-1])} и {removed_names[-1]}"
        return f"• {time_str}\n\n{text}"

    elif control.event == "leave":
        name = await get_name(message.sender_id)
        return f"• {time_str}\n\n{name} покинул(-а) чат"

    elif control.event == "new":
        actor_name = await get_name(message.sender_id)
        return f"• {time_str}\n\n{actor_name} создал(-а) новый чат"

    return None


async def sync_chat_history(client):
    logger.info("Starting chat history synchronization with Telegram...")

    messages = await client.get_messages(MAX_CHAT_ID)
    messages.sort(key=lambda m: m.time or 0)

    current_max_messages = {}
    for msg in messages:
        if msg.id:
            current_max_messages[msg.id] = msg

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT max_message_id, tg_message_id, max_sender_id, timestamp FROM messages") as cursor:
            rows = await cursor.fetchall()

    known_max_ids = set()
    for row in rows:
        max_id, tg_id, sender_id, ts = row
        known_max_ids.add(max_id)

        if max_id not in current_max_messages:
            # logger.info(f"Message {max_id} not found in MAX anymore → deleting from Telegram")
            # await delete_telegram_message(tg_id)
            # await delete_message_by_max_id(max_id)
            continue

        current_msg = current_max_messages[max_id]

        if current_msg.status.value == MessageStatus.REMOVED:
            # logger.info(f"Message {max_id} is deleted in MAX → deleting from Telegram")
            # await delete_telegram_message(tg_id)
            # await delete_message_by_max_id(max_id)
            pass
        elif current_msg.status.value == MessageStatus.EDITED:
            logger.info(f"Message {max_id} was edited → updating in Telegram")
            await handle_edited_message(current_msg)

    for msg in messages:
        if msg.status.value != MessageStatus.REMOVED and msg.id not in known_max_ids:
            logger.info(f"New message during downtime: {msg.id} → sending to Telegram")
            await process_message(client, msg)

    logger.info("Chat history synchronization completed.")


async def add_message(max_message_id: int, tg_message_id: int, max_sender_id: int, timestamp: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            'INSERT OR REPLACE INTO messages (max_message_id, tg_message_id, max_sender_id, timestamp) VALUES (?, ?, ?, ?)',
            (max_message_id, tg_message_id, max_sender_id, timestamp)
        )
        await db.commit()


async def get_message_by_max_id(max_message_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            'SELECT * FROM messages WHERE max_message_id = ?',
            (max_message_id,)
        )
        row = await cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'max_message_id': row[1],
                'tg_message_id': row[2],
                'max_sender_id': row[3],
                'timestamp': row[4]
            }
        return None


async def delete_message_by_max_id(max_message_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            'DELETE FROM messages WHERE max_message_id = ?',
            (max_message_id,)
        )
        await db.commit()


async def send_debug_message(text: str):
    if not TG_DEBUG_USER_ID or not TG_BOT_TOKEN:
        return
    try:
        base_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_DEBUG_USER_ID, "text": text}
        async with aiohttp.ClientSession() as session:
            async with session.post(base_url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to send debug message: HTTP {resp.status} {await resp.text()}")
    except Exception as e:
        logger.error(f"send_debug_message failed: {e}")


async def download_photo(attach):
    try:
        url = f"{attach.base_url}&sig={attach.photo_token}"
        file_path = os.path.join(IMAGES_DIR, f"{attach.photo_id}.webp")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    async with aiofiles.open(file_path, "wb") as f:
                        await f.write(await response.read())
                    logger.info(f"Image downloaded: {file_path}")
                    return file_path
                else:
                    err = f"Failed to download image: HTTP {response.status}"
                    logger.error(err)
                    await send_debug_message(f"[download_photo] {err} url={url}")
                    return None
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Failed to download image {attach.photo_id}: {e}")
        await send_debug_message(f"[download_photo] photo_id={attach.photo_id} error={e}\n{tb}")
        return None


async def download_video(url: str, video_id: int):
    try:
        file_path = os.path.join(VIDEOS_DIR, f"{video_id}.mp4")
        parsed_url = urlparse(url)
        host = parsed_url.netloc
        headers = {
            "Host": host,
            "User-Agent": USER_AGENT,
            **VIDEO_HEADERS,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    async with aiofiles.open(file_path, "wb") as f:
                        await f.write(await response.read())
                    logger.info(f"Video downloaded: {file_path}")
                    return file_path
                else:
                    err = f"Failed to download video: HTTP {response.status}"
                    logger.error(err)
                    await send_debug_message(f"[download_video] {err} video_id={video_id} url={url}")
                    return None
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Failed to download video {video_id}: {e}")
        await send_debug_message(f"[download_video] video_id={video_id} error={e}\n{tb}")
        return None


async def download_file(url: str, file_id: int, file_name: str):
    try:
        safe_name = "".join(c for c in file_name if c.isalnum() or c in ('.', '_', '-')).rstrip(
            '.') or f"file-{file_id}"
        file_path = os.path.join(FILES_DIR, f"{file_id}-{safe_name}")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    async with aiofiles.open(file_path, "wb") as f:
                        await f.write(await response.read())
                    logger.info(f"File downloaded: {file_path}")
                    return file_path
                else:
                    err = f"Failed to download file: HTTP {response.status}"
                    logger.error(err)
                    await send_debug_message(f"[download_file] {err} file_id={file_id} url={url}")
                    return None
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Failed to download file {file_id}: {e}")
        await send_debug_message(f"[download_file] file_id={file_id} error={e}\n{tb}")
        return None


def build_output(message, sender_name: str, is_edited: bool = False):
    time_ms = message.time or 0
    utc_dt = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc)
    pytz_moscow = pytz.timezone("Europe/Moscow")
    try:
        moscow_dt = utc_dt.astimezone(ZoneInfo("Europe/Moscow"))
    except:
        moscow_dt = utc_dt.astimezone(pytz_moscow)

    time_str = moscow_dt.strftime("%d.%m.%Y %H:%M:%S")

    text = message.formatted_html_text or ""
    special_message = text.startswith(("Добавил", "Присоединился", "Создал"))

    if special_message:
        return f"• {time_str}\n\n{text}"
    else:
        if is_edited and message.update_time:
            utc_dt1 = datetime.fromtimestamp(message.update_time / 1000, tz=timezone.utc)
            try:
                moscow_dt2 = utc_dt1.astimezone(ZoneInfo("Europe/Moscow"))
            except:
                moscow_dt2 = utc_dt1.astimezone(pytz.timezone("Europe/Moscow"))
            time_str1 = moscow_dt2.strftime("%d.%m.%Y %H:%M:%S")
            output = f"• {sender_name}\n• {time_str}\n• [Ред. в {time_str1}]"
        else:
            output = f"• {sender_name}\n• {time_str}"

        if message.forwarded_message:
            if message.forwarded_message.channel:
                forwarded_sender_name = message.forwarded_message.channel.name
            else:
                forwarded_sender_name = user_names.get(
                    str(message.forwarded_message.sender_id),
                    str(message.forwarded_message.sender_id)
                )
            output += (
                f"\n• [Пересланное сообщение от {forwarded_sender_name}]\n\n"
                f"{message.forwarded_message.formatted_html_text}"
            )
        else:
            output += f"\n\n{text}"
        return output


async def send_to_telegram(message, output: str, image_paths: list = None, video_paths: list = None,
                           file_paths: list = None, answer_message_id: int = None):
    base_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/"

    all_files = (image_paths or []) + (video_paths or []) + (file_paths or [])
    has_text = bool(output.strip())

    reply_params = None
    if answer_message_id:
        reply_params = {
            "message_id": answer_message_id,
            "allow_sending_without_reply": True
        }

    if not all_files and not has_text:
        logger.warning(f"No text or media for message {message.id}, skipping")
        return None

    if not all_files:
        send_url = base_url + "sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": output, "parse_mode": "HTML"}
        if answer_message_id:
            payload["reply_parameters"] = json.dumps({
                "message_id": answer_message_id,
                "allow_sending_without_reply": True
            })
        try:
            logger.debug(f"Sending message to Telegram:\n{output}")
            async with aiohttp.ClientSession() as session:
                async with session.post(send_url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        message_id = data['result']['message_id']
                        logger.info("Message successfully sent to Telegram")
                        return message_id
                    else:
                        logger.error(f"Telegram API error: {await resp.text()}")
                        return None
        except aiohttp.ClientError as e:
            logger.error(f"Telegram API error: {e}")
            await send_debug_message(f"[send_to_telegram] error={e}")
            return None
    else:
        media = []
        files_dict = {}
        valid_files = []

        for idx, f_path in enumerate(all_files):
            if not os.path.exists(f_path):
                continue
            base_name = os.path.basename(f_path)
            ext = os.path.splitext(base_name)[1].lower()
            if ext in [".webp", ".jpg", ".png"]:
                m_type = "photo"
            elif ext == ".mp4":
                m_type = "video"
            else:
                m_type = "document"

            attach_name = f"file{idx}"
            media.append({"type": m_type, "media": f"attach://{attach_name}"})
            try:
                files_dict[attach_name] = open(f_path, "rb")
                valid_files.append(f_path)
            except Exception as e:
                logger.error(f"Failed to open file {f_path}: {e}")

        if media:
            media[0]["caption"] = output
            media[0]["parse_mode"] = "HTML"
            send_url = base_url + "sendMediaGroup"
            form_data = aiohttp.FormData()
            form_data.add_field('chat_id', TG_CHAT_ID)
            form_data.add_field('media', json.dumps(media))
            if reply_params:
                form_data.add_field('reply_parameters', json.dumps(reply_params))

            for name, file_obj in files_dict.items():
                form_data.add_field(
                    name,
                    file_obj,
                    filename=os.path.basename(file_obj.name),
                    content_type="application/octet-stream"
                )

            try:
                logger.debug(f"Sending message to Telegram:\n{output}")
                async with aiohttp.ClientSession() as session:
                    async with session.post(send_url, data=form_data) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            message_id = data['result'][0]['message_id']
                            logger.info("Media group successfully sent to Telegram")
                            return message_id
                        else:
                            logger.error(f"Telegram API error: {await resp.text()}")
                            return None
            except aiohttp.ClientError as e:
                await send_debug_message(f"[send_to_telegram] error={e}")
                logger.warning(f"MediaGroup failed: {e}")
                return None
            finally:
                for f in files_dict.values():
                    try:
                        f.close()
                    except:
                        pass
        else:
            send_url = base_url + "sendMessage"
            payload = {"chat_id": TG_CHAT_ID, "text": output, "parse_mode": "HTML"}
            if answer_message_id:
                payload["reply_parameters"] = json.dumps({
                    "message_id": answer_message_id,
                    "allow_sending_without_reply": True
                })
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(send_url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            message_id = data['result']['message_id']
                            return message_id
                        else:
                            logger.error(f"Telegram API error: {await resp.text()}")
                            return None
            except aiohttp.ClientError as e:
                await send_debug_message(f"[send_to_telegram] error={e}")
                logger.error(f"Telegram API error: {e}")
                return None


async def edit_telegram_message(tg_message_id: int, new_text: str, is_media_group: bool = False):
    base_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/"

    clean_text = new_text.strip()
    if not clean_text:
        clean_text = " "

    if is_media_group:
        url = base_url + "editMessageCaption"
        payload = {
            "chat_id": TG_CHAT_ID,
            "message_id": tg_message_id,
            "caption": clean_text,
            "parse_mode": "HTML"
        }
    else:
        url = base_url + "editMessageText"
        payload = {
            "chat_id": TG_CHAT_ID,
            "message_id": tg_message_id,
            "text": clean_text,
            "parse_mode": "HTML"
        }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    logger.info(f"Message {tg_message_id} {'caption' if is_media_group else 'text'} edited successfully")
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"Failed to edit message {tg_message_id}: {error_text}")
                    return False
    except Exception as e:
        logger.error(f"Exception while editing message {tg_message_id}: {e}")
        return False


async def delete_telegram_message(tg_message_id: int):
    base_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "message_id": tg_message_id
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(base_url, json=payload) as resp:
                if resp.status == 200:
                    logger.info(f"Message {tg_message_id} deleted in Telegram")
                    return True
                else:
                    logger.error(f"Failed to delete message {tg_message_id} in Telegram: {await resp.text()}")
                    return False
    except Exception as e:
        logger.error(f"Error deleting Telegram message: {e}")
        return False


async def process_message(client, message: Message):
    if message.chat_id != MAX_CHAT_ID:
        return

    existing = await get_message_by_max_id(message.id)
    if existing:
        return

    sender_id = message.sender_id
    sender_str = str(sender_id)

    if sender_str not in user_names:
        try:
            contacts = await client.get_contacts([sender_id])
            if contacts:
                contact = contacts[0]
                user_names[sender_str] = f"{contact.first_name} {contact.last_name}".strip()
                logger.info(f"Added user name: {sender_str} -> {user_names[sender_str]}")
        except Exception as e:
            logger.error(f"Failed to fetch contact {sender_id}: {e}")
            user_names[sender_str] = sender_str

    if message.forwarded_message:
        if not message.forwarded_message.channel and message.forwarded_message.sender_id is not None:
            fwd_sender_str = str(message.forwarded_message.sender_id)
            if fwd_sender_str not in user_names:
                try:
                    contacts = await client.get_contacts([message.forwarded_message.sender_id])
                    if contacts:
                        contact = contacts[0]
                        user_names[fwd_sender_str] = f"{contact.first_name} {contact.last_name}".strip()
                        logger.info(f"Added forwarded user name: {fwd_sender_str} -> {user_names[fwd_sender_str]}")
                except Exception as e:
                    logger.error(f"Failed to fetch forwarded contact {message.forwarded_message.sender_id}: {e}")
                    user_names[fwd_sender_str] = fwd_sender_str

    image_paths = []
    video_paths = []
    file_paths = []

    for attach in message.attaches or []:
        if isinstance(attach, PhotoAttachment):
            path = await download_photo(attach)
            if path:
                image_paths.append(path)
        elif isinstance(attach, VideoAttachment):
            try:
                url = await client.get_video_link(attach, message)
                path = await download_video(url, attach.video_id)
                if path:
                    video_paths.append(path)
            except Exception as e:
                logger.error(f"Failed to get/download video: {e}")
        elif isinstance(attach, FileAttachment):
            try:
                url = await client.get_file_link(attach, message)
                path = await download_file(url, attach.file_id, attach.file_name)
                if path:
                    file_paths.append(path)
            except Exception as e:
                logger.error(f"Failed to get/download file: {e}")

    if message.forwarded_message:
        for attach in message.forwarded_message.attaches or []:
            if isinstance(attach, PhotoAttachment):
                path = await download_photo(attach)
                if path:
                    image_paths.append(path)
            elif isinstance(attach, VideoAttachment):
                try:
                    url = await client.get_video_link(attach, message)
                    path = await download_video(url, attach.video_id)
                    if path:
                        video_paths.append(path)
                except Exception as e:
                    logger.error(f"Failed to download forwarded video: {e}")
            elif isinstance(attach, FileAttachment):
                try:
                    url = await client.get_file_link(attach, message)
                    path = await download_file(url, attach.file_id, attach.file_name)
                    if path:
                        file_paths.append(path)
                except Exception as e:
                    logger.error(f"Failed to download forwarded file: {e}")

    sender_name = user_names.get(str(message.sender_id))

    if message.status.EDITED:
        output = build_output(message, sender_name, True)
    else:
        output = build_output(message, sender_name)

    if message.link:
        if message.link.get("type") == "REPLY" and message.link.get("message"):
            message_id = message.link.get("message").get("id")
            telegram_answer_message_id = (await get_message_by_max_id(message_id)).get("tg_message_id")
        else:
            telegram_answer_message_id = None
    else:
        telegram_answer_message_id = None

    control_output = await handle_control_message(message, client)
    if control_output:
        tg_message_id = await send_to_telegram(message, control_output)
        if tg_message_id:
            await add_message(message.id, tg_message_id, message.sender_id, message.time)
        return

    if message.status.value != 2:
        tg_message_id = await send_to_telegram(message, output, image_paths, video_paths, file_paths, telegram_answer_message_id)
        if tg_message_id:
            await add_message(message.id, tg_message_id, message.sender_id, message.time)
            logger.info(f"Message {message.id} saved to database with TG ID {tg_message_id}")


async def handle_edited_message(message: Message):
    if message.chat_id != MAX_CHAT_ID:
        return

    existing_message = await get_message_by_max_id(message.id)
    if not existing_message:
        logger.warning(f"Edited message {message.id} not found in database")
        return

    sender_name = user_names.get(str(message.sender_id), str(message.sender_id))
    output = build_output(message, sender_name, True)

    had_attachments = bool(message.attaches or message.forwarded_message.attaches)

    success = await edit_telegram_message(
        tg_message_id=existing_message['tg_message_id'],
        new_text=output,
        is_media_group=had_attachments
    )

    if success:
        logger.info(f"Message {message.id} edited in Telegram")
    else:
        logger.error(f"Failed to edit message {message.id} in Telegram")


async def handle_deleted_message(message: Message):
    if message.chat_id != MAX_CHAT_ID:
        return

    existing_message = await get_message_by_max_id(message.id)
    if not existing_message:
        logger.warning(f"Deleted message {message.id} not found in database")
        return

    success = await delete_telegram_message(existing_message['tg_message_id'])
    if success:
        await delete_message_by_max_id(message.id)
        logger.info(f"Message {message.id} deleted from database and Telegram")
    else:
        logger.error(f"Failed to delete message {message.id} in Telegram")


async def main():
    if not all([MAX_TOKEN, MAX_DEVICE_ID, TG_BOT_TOKEN]):
        logger.error("Missing required environment variables")
        return

    await init_db()

    max_config = Config(
        token=MAX_TOKEN,
        device_id=MAX_DEVICE_ID,
        debug=False,
        reconnect_delay=5,
        auto_reconnect=True,
        immediate_reconnect=True,
        user_agent=UserAgentConfig(
            user_agent=USER_AGENT,
            locale="ru",
            device_locale="ru",
            os_version="Windows",
            device_name="Firefox",
            app_version="25.9.16",
            screen="1080x1920 1.0x",
            timezone="Europe/Moscow"
        )
    )

    client = Client(max_config)

    @client.on_from_websocket
    async def hande_on_from_websocket(data: str):
        logger.info(f"↓ {data}")

    @client.on_to_websocket
    async def hande_on_to_websocket(data: str):
        logger.info(f"↑ {data}")

    @client.on_message
    async def handle_new_message(message: Message):
        await process_message(client, message)

    @client.on_edited
    async def handle_edited(message: Message):
        await handle_edited_message(message)

    @client.on_deleted
    async def handle_deleted(message: Message):
        # await handle_deleted_message(message)
        pass

    @client.on_disconnected
    async def disconnected(reason):
        logger.error(f"disconnected: {reason}")
        await send_debug_message(f"Disconnected: {reason}")

    @client.on_after_reconnect
    async def after_reconnect():
        logger.info("Reconnected!")
        await send_debug_message("Reconnected!")
        await client.subscribe_to_chat(MAX_CHAT_ID)

    try:
        await client.start()
        logger.info(f"Connected at {client.connection_time}")
        logger.info(f"Connected as {client.me.first_name} (ID: {client.me.id})")

        target_chat = next((c for c in client.chats if c.id == MAX_CHAT_ID), None)
        if not target_chat:
            logger.error(f"Chat {MAX_CHAT_ID} not found")
            return

        participant_ids = list(target_chat.participants.keys())
        if participant_ids:
            contacts = await client.get_contacts(participant_ids)
            for contact in contacts:
                user_id_str = str(contact.id)
                user_names[user_id_str] = f"{contact.first_name} {contact.last_name}".strip()

        await client.subscribe_to_chat(MAX_CHAT_ID)

        await sync_chat_history(client)

        await asyncio.Event().wait()
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Error in main: {e}")
        await send_debug_message(f"[main] error={e}\n{tb}")
    finally:
        await client.close()
        logger.info(f"Disconnected at {client.disconnection_time}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
