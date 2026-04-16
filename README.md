# MAX2TG

> [!CAUTION]
> THIS IS DEPRECATED. USE https://github.com/mochensky/max2tg-go INSTEAD

> [!CAUTION]
> This is intentionally bad code (for now).  
> 
> Lots of copy-paste, no structure, poor error handling, no tests, everything in one file ☠️.  
> It works, but it's not how real software should be written.
> 
> Big cleanup, restructuring soon.  
> Updated README + better architecture expected later.
> 
> Only run this if you're know what are you doing and how to work with Python.  
> Consider it a live prototype / messy experiment - not a great tool (yet).

This script mirrors messages from a [MAX messenger](https://max.ru) to a [Telegram](https://telegram.org) group/channel.

It supports:
- Text messages
- Photos / images
- Videos
- Files
- Replies (as Telegram replies)
- Edits (updates message in Telegram)
- Forwarded messages
- Service messages (join, leave, add/remove user, etc.)
- Moscow time formatting
- Persistent message mapping (SQLite)

## Features

- Real-time message forwarding
- Downloads and sends media (photos, videos, documents)
- Handles message edits
- Preserves reply structure
- Logs everything (console + `data/main.log`) (terrible logging implementation 💀)
- Debug messages to separate Telegram user (optional)
- History synchronization on startup (missed messages)

## Requirements

- Python 3.10+
- `max-user-api` library (https://github.com/mochensky/max-user-api)

## Installation

1. Clone the repository
```bash
git clone https://github.com/mochensky/MAX2TG.git
cd MAX2TG
```
2. Create virtual environment (recommended)
```
python -m venv venv
venv\Scripts\activate
```
3. Install dependencies
```
pip install -r requirements.txt
```

`requirements.txt`:

```txt
aiofiles
aiohttp
aiosqlite
pytz
dotenv
python-dotenv
# max-user-api should be installed from git or local path
```

## Configuration

1. Copy example file:

```bash
cp example.env .env
```

2. Open `.env` and fill in the values:

```env
MAX_TOKEN=your_max_token_here
MAX_DEVICE_ID=your_device_id_here
MAX_CHAT_ID=-00001234567890

TG_BOT_TOKEN=12345678:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TG_CHAT_ID=-1001987654321
TG_DEBUG_USER_ID=123456789
```

**How to get values:**

- `MAX_TOKEN` + `MAX_DEVICE_ID` → obtained by intercepting requests from https://web.max.ru (DevTools → Network (sort by ws)) after login OR from local storage (DevTools → Storage) (Recommended)
- `MAX_CHAT_ID` → numeric ID of the target chat (can be found in URL while chat opened (recommended) or via API)
- `TG_BOT_TOKEN` → create bot via [@BotFather](https://t.me/botfather)
- `TG_CHAT_ID` → ID of group/channel

## Usage

```bash
python main.py
```

The bot will:
1. Connect to [MAX messenger](https://max.ru) via WebSocket
2. Synchronize missed messages (if any)
3. Start listening for new messages / edits
4. Forward everything to the specified Telegram chat

## Folder structure after run

```
data/
├── main.log           ← log
├── main.db            ← SQLite db
├── images/            ← downloaded photos
├── videos/            ← downloaded videos
└── files/             ← downloaded files
```

## Notes

- Media is downloaded to disk before sending to Telegram
- Large videos/files may take time to download
- Deleted messages handling is commented out (uncomment if needed)
- Time is always shown in Europe/Moscow timezone
