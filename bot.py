"""
YouTube Video Downloader Telegram Bot
Uses Pyrogram (MTProto) with API_ID/API_HASH so large files (>50MB) can be sent directly.
Deploy: Antideploy (webhook not needed - MTProto keeps a persistent connection).
A tiny HTTP server runs on $PORT for the platform health check.
"""
import os
import re
import asyncio
import logging
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ytdl-bot")

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise SystemExit("Missing API_ID / API_HASH / BOT_TOKEN env vars")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/ytdl")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.|m\.|music\.)?(youtube\.com/(watch|shorts|live)|youtu\.be)/[^\s]+"
)

# url -> {title, formats}
pending = {}

# ---------- tiny health-check server for the hosting platform ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - ytdl bot alive")

    def log_message(self, *a):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", "8000"))
    try:
        HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
    except Exception as e:
        log.warning("health server failed: %s", e)

# ---------- yt-dlp helpers ----------
def extract_info(url: str) -> dict:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "extract_flat": False, "socket_timeout": 30}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def download_video(url: str, format_id: str, status_cb=None) -> tuple[str, str]:
    """Returns (filepath, title). format_id like 'bv*[height<=720]+ba/b' or 'bestaudio'."""
    import yt_dlp

    def hook(d):
        if status_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                try:
                    status_cb(done / total)
                except Exception:
                    pass

    opts = {
        "format": format_id,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "socket_timeout": 60,
        "retries": 3,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        # merged file may have .mp4 extension instead
        if not os.path.exists(path):
            base = os.path.splitext(path)[0] + ".mp4"
            if os.path.exists(base):
                path = base
        return path, info.get("title", "video")

QUALITY_OPTIONS = [
    ("🎬 360p", "bv*[height<=360]+ba/b[height<=360]/b"),
    ("🎬 720p", "bv*[height<=720]+ba/b[height<=720]/b"),
    ("🎬 1080p", "bv*[height<=1080]+ba/b[height<=1080]/b"),
    ("🎵 Audio MP3", "bestaudio/b"),
]

# ---------- bot ----------
app = Client("ytdl_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             workers=16)

def is_owner(_, __, m: Message):
    return OWNER_ID == 0 or (m.from_user and m.from_user.id == OWNER_ID)

@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    await m.reply_text(
        "👋 <b>YouTube Downloader</b>\n\n"
        "Mujhe koi YouTube link bhejo — quality select karo, main video/audio download karke bhej dunga.\n\n"
        "MTProto se bhejta hun, to 50MB se badi files bhi aa jayengi. 🚀",
    )

@app.on_message(filters.command("help"))
async def help_cmd(_, m: Message):
    await m.reply_text(
        "📖 <b>Kaise use karein:</b>\n\n"
        "1. YouTube video ka link bhejo\n"
        "2. Quality ka button dabao (360p / 720p / 1080p / Audio)\n"
        "3. Thoda wait karo — download hoke video aa jayegi\n\n"
        "Commands: /start /help",
    )

@app.on_message(filters.regex(YOUTUBE_RE))
async def url_handler(_, m: Message):
    match = YOUTUBE_RE.search(m.text or "")
    if not match:
        return
    url = match.group(0)
    if not url.startswith("http"):
        url = "https://" + url

    status = await m.reply_text("🔍 Video info nikal raha hun...")

    def _info():
        try:
            return extract_info(url)
        except Exception as e:
            return {"_error": str(e)}

    info = await asyncio.to_thread(_info)
    if "_error" in info:
        await status.edit_text(f"❌ Error: <code>{info['_error'][:200]}</code>")
        return

    title = info.get("title", "Unknown")
    duration = info.get("duration") or 0
    mins, secs = divmod(int(duration), 60)

    key = f"{m.chat.id}:{m.id}"
    pending[key] = {"url": url, "title": title}

    buttons = [
        [InlineKeyboardButton(label, callback_data=f"q|{key}|{i}")]
        for i, (label, _) in enumerate(QUALITY_OPTIONS)
    ]
    await status.edit_text(
        f"🎥 <b>{title}</b>\n⏱️ Duration: {mins}:{secs:02d}\n\nQuality chuno:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

@app.on_callback_query(filters.regex(r"^q\|"))
async def quality_cb(_, q: CallbackQuery):
    try:
        _, key, idx = q.data.split("|")
        idx = int(idx)
    except ValueError:
        await q.answer("Invalid option", show_alert=True)
        return

    data = pending.get(key)
    if not data:
        await q.answer("Session expire ho gaya, link dobara bhejo.", show_alert=True)
        return

    label, fmt = QUALITY_OPTIONS[idx]
    url, title = data["url"], data["title"]
    await q.message.edit_text(f"⬇️ Downloading <b>{title}</b>\n🎚️ {label} ...")

    loop = asyncio.get_event_loop()
    last_update = 0.0

    def progress_cb(frac: float):
        nonlocal last_update
        import time
        now = time.time()
        if now - last_update < 5:  # update every 5s to avoid flood
            return
        last_update = now
        pct = int(frac * 100)
        asyncio.run_coroutine_threadsafe(
            q.message.edit_text(f"⬇️ Downloading <b>{title}</b>\n🎚️ {label}\n📊 {pct}%"),
            loop,
        )

    try:
        path, real_title = await asyncio.to_thread(download_video, url, fmt, progress_cb)
    except Exception as e:
        log.exception("download failed")
        await q.message.edit_text(f"❌ Download failed: <code>{str(e)[:200]}</code>")
        pending.pop(key, None)
        return

    size_mb = os.path.getsize(path) / (1024 * 1024)
    await q.message.edit_text(
        f"⬆️ Uploading <b>{real_title}</b>\n📦 {size_mb:.1f} MB ..."
    )

    caption = f"🎥 <b>{real_title}</b>\n🎚️ {label}"
    try:
        if "Audio" in label:
            await app.send_audio(q.message.chat.id, path, caption=caption)
        else:
            await app.send_video(q.message.chat.id, path, caption=caption)
        await q.message.edit_text("✅ Ho gaya! Enjoy 🎉")
    except Exception as e:
        log.exception("upload failed")
        await q.message.edit_text(f"❌ Upload failed: <code>{str(e)[:200]}</code>")
    finally:
        pending.pop(key, None)
        try:
            os.remove(path)
        except OSError:
            pass

@app.on_message(filters.command("ping"))
async def ping_cmd(_, m: Message):
    if OWNER_ID and m.from_user and m.from_user.id == OWNER_ID:
        await m.reply_text("🏓 pong — bot alive")

if __name__ == "__main__":
    Thread(target=run_health_server, daemon=True).start()
    log.info("starting bot (MTProto)...")
    app.run()
