"""
YouTube / Social Media Video Downloader Telegram Bot (Advanced)

- Pyrogram (MTProto) with API_ID/API_HASH: sends files directly, bypassing
  the 50MB Bot HTTP API limit.
- yt-dlp: supports YouTube + 1000+ other sites.
- Tiny HTTP server on $PORT answers the hosting platform health check.

Env vars: API_ID, API_HASH, BOT_TOKEN, OWNER_ID (optional), DOWNLOAD_DIR, PORT
"""
import os
import re
import time
import asyncio
import logging
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ytdl-bot")

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise SystemExit("Missing API_ID / API_HASH / BOT_TOKEN env vars")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/ytdl")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

URL_RE = re.compile(r"https?://[^\s]+")

# pending downloads: key -> dict
pending = {}
# active download tasks for cancellation: key -> asyncio.Task
active_tasks = {}

QUALITY_OPTIONS = [
    ("📱 360p",   "bv*[height<=360]+ba/b[height<=360]/b"),
    ("💻 720p",   "bv*[height<=720]+ba/b[height<=720]/b"),
    ("🖥️ 1080p",  "bv*[height<=1080]+ba/b[height<=1080]/b"),
    ("🎞️ Best",   "bv*+ba/b"),
    ("🎵 Audio",  "bestaudio/b"),
]

# ---------------- health check server ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - downloader bot alive")
    def log_message(self, *a):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", "8000"))
    try:
        HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
    except Exception as e:
        log.warning("health server failed: %s", e)

# ---------------- yt-dlp helpers ----------------
def extract_info(url: str) -> dict:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def download_media(url: str, format_id: str, progress_cb=None) -> tuple[str, str, dict]:
    """Returns (filepath, title, info_dict)."""
    import yt_dlp

    def hook(d):
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta")
            if total:
                try:
                    progress_cb(done / total, speed, eta)
                except Exception:
                    pass

    is_audio = format_id.startswith("bestaudio")
    opts = {
        "format": format_id,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
        "retries": 3,
        "progress_hooks": [hook],
    }
    if is_audio:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        if is_audio:
            base = os.path.splitext(path)[0] + ".mp3"
            if os.path.exists(base):
                path = base
        elif not os.path.exists(path):
            mp4 = os.path.splitext(path)[0] + ".mp4"
            if os.path.exists(mp4):
                path = mp4
        return path, info.get("title", "video"), info

def format_views(n) -> str:
    if not n:
        return "N/A"
    n = int(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return str(n)

def format_duration(sec) -> str:
    if not sec:
        return "N/A"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def progress_bar(frac: float, width: int = 12) -> str:
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)

def format_speed(bps) -> str:
    if not bps:
        return "—"
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024:
            return f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} GB/s"

# ---------------- bot ----------------
app = Client("ytdl_bot", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, workers=16)

WELCOME = (
    "👋 <b>Welcome to Video Downloader Bot</b>\n\n"
    "Send me a video link and I'll download it for you.\n\n"
    "✅ <b>Supported:</b> YouTube + 1000+ sites (Instagram, TikTok, Twitter/X, Facebook, Vimeo, etc.)\n"
    "✅ <b>Large files:</b> sent directly via MTProto — no 50MB limit\n\n"
    "Just paste a link to get started! 🚀"
)

HELP_TEXT = (
    "📖 <b>How to use</b>\n\n"
    "1️⃣ Send a video URL\n"
    "2️⃣ Choose quality (360p / 720p / 1080p / Best / Audio)\n"
    "3️⃣ Wait for download & upload — that's it!\n\n"
    "💡 <b>Tips:</b>\n"
    "• Use /cancel to stop an active download\n"
    "• Audio mode extracts high-quality MP3\n"
    "• Playlists: send a playlist link (first video will be processed)\n\n"
    "Commands: /start • /help • /cancel"
)

@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    await m.reply_text(WELCOME)

@app.on_message(filters.command("help"))
async def help_cmd(_, m: Message):
    await m.reply_text(HELP_TEXT)

@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, m: Message):
    key = f"{m.chat.id}:{m.reply_to_message.id}" if m.reply_to_message else None
    # cancel the most recent active task for this chat
    cancelled = False
    for k in list(active_tasks):
        if k.startswith(f"{m.chat.id}:"):
            task = active_tasks.pop(k, None)
            if task and not task.done():
                task.cancel()
                cancelled = True
            pending.pop(k, None)
    await m.reply_text("🛑 Download cancelled." if cancelled
                       else "ℹ️ No active download to cancel.")

@app.on_message(filters.regex(URL_RE) & ~filters.command(["start", "help", "cancel", "ping"]))
async def url_handler(_, m: Message):
    match = URL_RE.search(m.text or "")
    if not match:
        return
    url = match.group(0).rstrip(").>,]")

    status = await m.reply_text("🔍 <b>Fetching video info…</b>")

    def _info():
        try:
            return extract_info(url)
        except Exception as e:
            return {"_error": str(e)}

    info = await asyncio.to_thread(_info)
    if "_error" in info:
        await status.edit_text(
            f"❌ <b>Could not fetch video.</b>\n<code>{info['_error'][:250]}</code>")
        return

    title = info.get("title", "Unknown")
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    duration = format_duration(info.get("duration"))
    views = format_views(info.get("view_count"))
    webpage = info.get("webpage_url", url)
    thumb = info.get("thumbnail")

    key = f"{m.chat.id}:{m.id}"
    pending[key] = {"url": webpage, "title": title, "thumb": thumb}

    buttons = [[InlineKeyboardButton(label, callback_data=f"q|{key}|{i}")]
               for i, (label, _) in enumerate(QUALITY_OPTIONS)]

    caption = (
        f"🎬 <b>{title}</b>\n\n"
        f"👤 {uploader}\n"
        f"⏱️ {duration} &nbsp;•&nbsp; 👁️ {views} views\n\n"
        f"<b>Select quality:</b>"
    )
    try:
        if thumb:
            await status.delete()
            await m.reply_photo(
                thumb, caption=caption,
                reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await status.edit_text(caption,
                                   reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        await status.edit_text(caption,
                               reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query(filters.regex(r"^q\|"))
async def quality_cb(_, q: CallbackQuery):
    try:
        _, key, idx = q.data.split("|")
        idx = int(idx)
    except ValueError:
        await q.answer("Invalid option.", show_alert=True)
        return

    data = pending.get(key)
    if not data:
        await q.answer("Session expired — please send the link again.",
                       show_alert=True)
        return

    label, fmt = QUALITY_OPTIONS[idx]
    url, title = data["url"], data["title"]
    is_audio = "Audio" in label

    cancel_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 Cancel", callback_data=f"x|{key}")]])
    await q.message.edit_text(
        f"⬇️ <b>Downloading</b>\n📌 {title}\n🎚️ {label}\n\n<i>Starting…</i>",
        reply_markup=cancel_btn)

    loop = asyncio.get_event_loop()
    last_update = 0.0
    state = {"msg": q.message, "label": label, "title": title,
             "cancel_btn": cancel_btn}

    def progress_cb(frac: float, speed, eta):
        nonlocal last_update
        now = time.time()
        if now - last_update < 4:
            return
        last_update = now
        pct = int(frac * 100)
        bar = progress_bar(frac)
        eta_txt = f"{int(eta)}s" if eta else "—"
        text = (f"⬇️ <b>Downloading</b>\n📌 {title}\n🎚️ {label}\n\n"
                f"{bar} {pct}%\n"
                f"⚡ {format_speed(speed)} &nbsp;•&nbsp; ⏳ ETA {eta_txt}")
        asyncio.run_coroutine_threadsafe(
            safe_edit(state["msg"], text, state["cancel_btn"]), loop)

    async def safe_edit(msg, text, markup=None):
        try:
            await msg.edit_text(text, reply_markup=markup)
        except Exception:
            pass

    async def _run():
        try:
            path, real_title, info = await asyncio.to_thread(
                download_media, url, fmt, progress_cb)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("download failed")
            await safe_edit(state["msg"],
                            f"❌ <b>Download failed.</b>\n<code>{str(e)[:250]}</code>")
            pending.pop(key, None)
            return

        size_mb = os.path.getsize(path) / (1024 * 1024)
        await safe_edit(state["msg"],
                        f"⬆️ <b>Uploading</b>\n📌 {real_title}\n📦 {size_mb:.1f} MB\n\n<i>Please wait…</i>")

        caption = f"🎬 <b>{real_title}</b>\n🎚️ {label} &nbsp;•&nbsp; 📦 {size_mb:.1f} MB"
        thumb = data.get("thumb")
        try:
            if is_audio:
                await app.send_audio(
                    q.message.chat.id, path, caption=caption,
                    title=real_title[:60],
                    performer=info.get("uploader") or info.get("channel"),
                    duration=int(info.get("duration") or 0) or None)
            else:
                await app.send_video(
                    q.message.chat.id, path, caption=caption,
                    duration=int(info.get("duration") or 0) or None,
                    width=info.get("width") or 0 or None,
                    height=info.get("height") or 0 or None)
            await safe_edit(state["msg"], "✅ <b>Done!</b> Enjoy 🎉")
        except Exception as e:
            log.exception("upload failed")
            await safe_edit(state["msg"],
                            f"❌ <b>Upload failed.</b>\n<code>{str(e)[:250]}</code>")
        finally:
            pending.pop(key, None)
            active_tasks.pop(key, None)
            for p in (path,):
                try:
                    os.remove(p)
                except OSError:
                    pass

    task = asyncio.create_task(_run())
    active_tasks[key] = task
    try:
        await task
    except asyncio.CancelledError:
        await safe_edit(q.message, "🛑 <b>Download cancelled.</b>")
        pending.pop(key, None)

@app.on_callback_query(filters.regex(r"^x\|"))
async def cancel_cb(_, q: CallbackQuery):
    _, key = q.data.split("|", 1)
    task = active_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
        await q.answer("Cancelling…")
    else:
        await q.answer("Nothing to cancel.", show_alert=True)
    pending.pop(key, None)

@app.on_message(filters.command("ping"))
async def ping_cmd(_, m: Message):
    if not OWNER_ID or (m.from_user and m.from_user.id == OWNER_ID):
        t0 = time.time()
        msg = await m.reply_text("🏓 <i>ping…</i>")
        ms = int((time.time() - t0) * 1000)
        await msg.edit_text(f"🏓 <b>Pong!</b> {ms} ms — bot is alive ✅")

if __name__ == "__main__":
    Thread(target=run_health_server, daemon=True).start()
    log.info("starting downloader bot (MTProto)…")
    app.run()
