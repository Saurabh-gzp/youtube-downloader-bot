# YouTube Downloader Telegram Bot

MTProto-based (Pyrogram) YouTube downloader — sends files directly, so videos larger than the 50MB Bot API limit work too.

## How it works
1. Send any YouTube link to the bot
2. Pick quality (360p / 720p / 1080p / Audio MP3)
3. Bot downloads with `yt-dlp` and uploads via MTProto

## Deploy (Antideploy)
Env vars required:
- `API_ID` — from my.telegram.org
- `API_HASH` — from my.telegram.org
- `BOT_TOKEN` — from @BotFather
- `OWNER_ID` — (optional) Telegram user id of owner
- `PORT` — set by the platform (default 8000)

A tiny HTTP server on `$PORT` answers the platform health check; the bot itself runs over MTProto (no webhook needed).

## Run locally
```bash
pip install -r requirements.txt
export API_ID=... API_HASH=... BOT_TOKEN=...
python bot.py
```

**Note:** `ffmpeg` must be installed for merging video+audio (`apt install ffmpeg`).
