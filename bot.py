"""
Video Downloader Telegram Bot v3 (Advanced)

Features:
- Pyrogram (MTProto) + yt-dlp, large files via MTProto
- YouTube 403 bypass via Android player client
- Broadcast / block / unblock (owner)
- Stats dashboard (owner)
- Force-join channels: multiple, on/off, add by chat ID, auto membership check

Env: API_ID, API_HASH, BOT_TOKEN, OWNER_ID, DOWNLOAD_DIR, PORT
"""
import os
import re
import time
import asyncio
import logging
import sqlite3
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
)
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChannelPrivate

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

DB_PATH = os.environ.get("DB_PATH", "/tmp/ytdl/bot.db")

URL_RE = re.compile(r"https?://[^\s]+")
pending = {}
active_tasks = {}

QUALITY_OPTIONS = [
    ("📱 360p",  "bv*[height<=360]+ba/b[height<=360]/b"),
    ("💻 720p",  "bv*[height<=720]+ba/b[height<=720]/b"),
    ("🖥️ 1080p", "bv*[height<=1080]+ba/b[height<=1080]/b"),
    ("🎞️ Best",  "bv*+ba/b"),
    ("🎵 Audio", "bestaudio/b"),
]

# ---------------- database ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT,
                  first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  downloads INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS channels
                 (chat_id INTEGER PRIMARY KEY, title TEXT,
                  invite_link TEXT, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats
                 (date TEXT PRIMARY KEY, downloads INTEGER DEFAULT 0)""")
    c.commit()
    c.close()

def get_setting(key, default=None):
    c = db()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return row["value"] if row else default

def set_setting(key, value):
    c = db()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
              (key, value))
    c.commit()
    c.close()

def register_user(user_id, username=None):
    c = db()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)",
              (user_id, username))
    if username:
        c.execute("UPDATE users SET username=? WHERE user_id=?",
                  (username, user_id))
    c.commit()
    c.close()

def is_blocked(user_id) -> bool:
    c = db()
    row = c.execute("SELECT blocked FROM users WHERE user_id=?",
                    (user_id,)).fetchone()
    c.close()
    return bool(row and row["blocked"])

def bump_downloads(user_id):
    c = db()
    c.execute("UPDATE users SET downloads = downloads + 1 WHERE user_id=?",
              (user_id,))
    today = time.strftime("%Y-%m-%d")
    c.execute("INSERT INTO stats (date, downloads) VALUES (?,1) "
              "ON CONFLICT(date) DO UPDATE SET downloads = downloads + 1",
              (today,))
    c.commit()
    c.close()

def get_channels():
    c = db()
    rows = c.execute("SELECT * FROM channels ORDER BY added_at").fetchall()
    c.close()
    return rows

def add_channel(chat_id, title="", invite_link=""):
    c = db()
    c.execute("INSERT OR REPLACE INTO channels (chat_id, title, invite_link)"
              " VALUES (?,?,?)", (chat_id, title, invite_link))
    c.commit()
    c.close()

def del_channel(chat_id):
    c = db()
    c.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
    c.commit()
    c.close()

# ---------------- health check ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", "8000"))
    try:
        HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
    except Exception as e:
        log.warning("health server: %s", e)

# ---------------- yt-dlp ----------------
def extract_info(url: str) -> dict:
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 30,
        # 403 bypass: use Android player client
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def download_media(url: str, format_id: str, progress_cb=None):
    import yt_dlp

    def hook(d):
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                try:
                    progress_cb(done / total, d.get("speed") or 0, d.get("eta"))
                except Exception:
                    pass

    is_audio = format_id.startswith("bestaudio")
    opts = {
        "format": format_id,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True, "no_warnings": True,
        "socket_timeout": 60, "retries": 3,
        "progress_hooks": [hook],
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    if is_audio:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                   "preferredcodec": "mp3",
                                   "preferredquality": "192"}]
    else:
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        if is_audio:
            mp3 = os.path.splitext(path)[0] + ".mp3"
            if os.path.exists(mp3):
                path = mp3
        elif not os.path.exists(path):
            mp4 = os.path.splitext(path)[0] + ".mp4"
            if os.path.exists(mp4):
                path = mp4
        return path, info.get("title", "video"), info

def fmt_views(n):
    if not n: return "N/A"
    n = int(n)
    for u, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= d: return f"{n/d:.1f}{u}"
    return str(n)

def fmt_dur(s):
    if not s: return "N/A"
    s = int(s); h, r = divmod(s, 3600); m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

def bar(frac, w=12):
    f = int(frac * w)
    return "█" * f + "░" * (w - f)

def fmt_speed(bps):
    if not bps: return "—"
    for u in ("B/s", "KB/s", "MB/s"):
        if bps < 1024: return f"{bps:.1f} {u}"
        bps /= 1024
    return f"{bps:.1f} GB/s"

# ---------------- bot ----------------
app = Client("ytdl_bot", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, workers=16)

def is_owner(_, __, m: Message):
    return OWNER_ID and m.from_user and m.from_user.id == OWNER_ID

owner_filter = filters.create(is_owner)

async def check_force_join(user_id) -> tuple[bool, list]:
    """Returns (ok, missing_channels)."""
    if get_setting("fjoin", "off") != "on":
        return True, []
    missing = []
    for ch in get_channels():
        try:
            await app.get_chat_member(ch["chat_id"], user_id)
        except (UserNotParticipant, Exception):
            missing.append(dict(ch))
    return (len(missing) == 0), missing

def join_keyboard(missing):
    buttons = []
    for ch in missing:
        label = ch.get("title") or str(ch["chat_id"])
        url = ch.get("invite_link")
        if url:
            buttons.append([InlineKeyboardButton(f"📢 Join {label}", url=url)])
        else:
            buttons.append([InlineKeyboardButton(
                f"📢 Join {label}",
                callback_data=f"noop")])
    buttons.append([InlineKeyboardButton("✅ I've Joined — Check Again",
                                         callback_data="recheck")])
    return InlineKeyboardMarkup(buttons)

WELCOME = (
    "👋 <b>Welcome to Video Downloader Bot</b>\n\n"
    "Send me a video link and I'll download it for you.\n\n"
    "✅ YouTube + 1000+ sites\n"
    "✅ Large files via MTProto — no 50MB limit\n\n"
    "Just paste a link to get started! 🚀"
)
HELP_TEXT = (
    "📖 <b>How to use</b>\n\n"
    "1️⃣ Send a video URL\n"
    "2️⃣ Choose quality\n"
    "3️⃣ Wait for download & upload\n\n"
    "/start • /help • /cancel"
)
ADMIN_HELP = (
    "🛠️ <b>Admin Commands</b>\n\n"
    "<b>Broadcast & Users</b>\n"
    "/broadcast — reply to a message to send it to all users\n"
    "/block &lt;user_id&gt; — block a user\n"
    "/unblock &lt;user_id&gt; — unblock\n"
    "/stats — bot statistics\n\n"
    "<b>Force-Join Channels</b>\n"
    "/addchannel &lt;chat_id&gt; — add channel (bot must be admin there)\n"
    "/delchannel &lt;chat_id&gt; — remove\n"
    "/channels — list channels\n"
    "/fjoin on|off — toggle force-join\n\n"
    "💡 To add a channel: make the bot admin in your channel, "
    "then send /addchannel with its chat ID."
)

@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    register_user(m.from_user.id,
                  m.from_user.username if m.from_user else None)
    if is_blocked(m.from_user.id):
        return
    ok, missing = await check_force_join(m.from_user.id)
    if not ok:
        await m.reply_text(
            "🔒 <b>Please join our channels first:</b>",
            reply_markup=join_keyboard(missing))
        return
    await m.reply_text(WELCOME)

@app.on_message(filters.command("help"))
async def help_cmd(_, m: Message):
    await m.reply_text(HELP_TEXT)

@app.on_message(filters.command("admin") & owner_filter)
async def admin_cmd(_, m: Message):
    await m.reply_text(ADMIN_HELP)

@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, m: Message):
    cancelled = False
    for k in list(active_tasks):
        if k.startswith(f"{m.chat.id}:"):
            t = active_tasks.pop(k, None)
            if t and not t.done():
                t.cancel(); cancelled = True
            pending.pop(k, None)
    await m.reply_text("🛑 Cancelled." if cancelled
                       else "ℹ️ No active download.")

# ---- broadcast ----
@app.on_message(filters.command("broadcast") & owner_filter)
async def broadcast_cmd(_, m: Message):
    if not m.reply_to_message:
        await m.reply_text("ℹ️ Reply to a message with /broadcast to send it to all users.")
        return
    c = db()
    users = c.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
    c.close()
    total, sent = len(users), 0
    status = await m.reply_text(f"📣 Broadcasting to {total} users…")
    for (uid,) in users:
        try:
            await m.reply_to_message.copy(uid)
            sent += 1
        except Exception:
            pass
        if sent % 20 == 0:
            try: await status.edit_text(f"📣 {sent}/{total} sent…")
            except Exception: pass
    await status.edit_text(f"✅ Broadcast done: {sent}/{total} delivered.")

@app.on_message(filters.command("block") & owner_filter)
async def block_cmd(_, m: Message):
    if len(m.command) < 2 or not m.command[1].isdigit():
        await m.reply_text("Usage: /block &lt;user_id&gt;"); return
    uid = int(m.command[1])
    c = db(); c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    c.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
    c.commit(); c.close()
    await m.reply_text(f"⛔ User <code>{uid}</code> blocked.")

@app.on_message(filters.command("unblock") & owner_filter)
async def unblock_cmd(_, m: Message):
    if len(m.command) < 2 or not m.command[1].isdigit():
        await m.reply_text("Usage: /unblock &lt;user_id&gt;"); return
    uid = int(m.command[1])
    c = db(); c.execute("UPDATE users SET blocked=0 WHERE user_id=?", (uid,))
    c.commit(); c.close()
    await m.reply_text(f"✅ User <code>{uid}</code> unblocked.")

@app.on_message(filters.command("stats") & owner_filter)
async def stats_cmd(_, m: Message):
    c = db()
    total = c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    blocked = c.execute("SELECT COUNT(*) c FROM users WHERE blocked=1").fetchone()["c"]
    dl_total = c.execute("SELECT COALESCE(SUM(downloads),0) s FROM users").fetchone()["s"]
    today = time.strftime("%Y-%m-%d")
    dl_today = c.execute("SELECT downloads FROM stats WHERE date=?", (today,)).fetchone()
    dl_today = dl_today["downloads"] if dl_today else 0
    ch = c.execute("SELECT COUNT(*) c FROM channels").fetchone()["c"]
    c.close()
    await m.reply_text(
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total users: <b>{total}</b>\n"
        f"⛔ Blocked: <b>{blocked}</b>\n"
        f"⬇️ Total downloads: <b>{dl_total}</b>\n"
        f"📅 Today: <b>{dl_today}</b>\n"
        f"📢 Force-join channels: <b>{ch}</b> "
        f"({'ON' if get_setting('fjoin','off')=='on' else 'OFF'})")

# ---- channels ----
@app.on_message(filters.command("addchannel") & owner_filter)
async def addchannel_cmd(_, m: Message):
    if len(m.command) < 2:
        await m.reply_text("Usage: /addchannel &lt;chat_id&gt;\n\n"
                           "Make the bot admin in the channel first, then send its chat ID.")
        return
    try:
        chat_id = int(m.command[1])
    except ValueError:
        await m.reply_text("❌ chat_id must be a number (e.g. -1001234567890).")
        return
    try:
        chat = await app.get_chat(chat_id)
        title = chat.title or str(chat_id)
        invite = chat.invite_link or ""
        if not invite:
            try:
                invite = await app.export_chat_invite_link(chat_id)
            except Exception:
                pass
    except (ChannelPrivate, Exception) as e:
        await m.reply_text(f"❌ Cannot access that chat: {e}\n\n"
                           "Make sure the bot is admin there.")
        return
    add_channel(chat_id, title, invite)
    await m.reply_text(f"✅ Channel added: <b>{title}</b>\n"
                       f"Force-join is {'ON' if get_setting('fjoin','off')=='on' else 'OFF'}.")

@app.on_message(filters.command("delchannel") & owner_filter)
async def delchannel_cmd(_, m: Message):
    if len(m.command) < 2:
        await m.reply_text("Usage: /delchannel &lt;chat_id&gt;"); return
    try: del_channel(int(m.command[1]))
    except ValueError: await m.reply_text("❌ Invalid chat_id."); return
    await m.reply_text("🗑️ Channel removed.")

@app.on_message(filters.command("channels") & owner_filter)
async def channels_cmd(_, m: Message):
    rows = get_channels()
    if not rows:
        await m.reply_text("📭 No force-join channels set.\nUse /addchannel &lt;chat_id&gt;.")
        return
    lines = ["📢 <b>Force-join channels:</b>\n"]
    for r in rows:
        lines.append(f"• {r['title'] or r['chat_id']} — <code>{r['chat_id']}</code>")
    lines.append(f"\nStatus: <b>{'ON' if get_setting('fjoin','off')=='on' else 'OFF'}</b>")
    await m.reply_text("\n".join(lines))

@app.on_message(filters.command("fjoin") & owner_filter)
async def fjoin_cmd(_, m: Message):
    if len(m.command) < 2 or m.command[1].lower() not in ("on", "off"):
        await m.reply_text("Usage: /fjoin on|off"); return
    set_setting("fjoin", m.command[1].lower())
    await m.reply_text(f"🔒 Force-join is now <b>{m.command[1].upper()}</b>.")

@app.on_callback_query(filters.regex("^recheck$"))
async def recheck_cb(_, q: CallbackQuery):
    ok, missing = await check_force_join(q.from_user.id)
    if ok:
        await q.message.edit_text("✅ <b>Verified!</b> " + WELCOME)
    else:
        await q.answer("Still not joined all channels.", show_alert=True)

# ---- download flow ----
@app.on_message(filters.regex(URL_RE) & ~filters.command(
    ["start","help","cancel","ping","admin","broadcast","block","unblock",
     "stats","addchannel","delchannel","channels","fjoin"]))
async def url_handler(_, m: Message):
    register_user(m.from_user.id,
                  m.from_user.username if m.from_user else None)
    if is_blocked(m.from_user.id):
        return
    ok, missing = await check_force_join(m.from_user.id)
    if not ok:
        await m.reply_text("🔒 <b>Please join our channels first:</b>",
                           reply_markup=join_keyboard(missing))
        return

    match = URL_RE.search(m.text or "")
    url = match.group(0).rstrip(").>,]")

    status = await m.reply_text("🔍 <b>Fetching video info…</b>")

    def _info():
        try: return extract_info(url)
        except Exception as e: return {"_error": str(e)}

    info = await asyncio.to_thread(_info)
    if "_error" in info:
        err = info["_error"]
        hint = ""
        if "403" in err:
            hint = "\n\n💡 YouTube is rate-limiting. Try again in a bit."
        await status.edit_text(f"❌ <b>Could not fetch video.</b>\n<code>{err[:250]}</code>{hint}")
        return

    title = info.get("title", "Unknown")
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    key = f"{m.chat.id}:{m.id}"
    pending[key] = {"url": info.get("webpage_url", url), "title": title,
                    "thumb": info.get("thumbnail")}

    buttons = [[InlineKeyboardButton(lbl, callback_data=f"q|{key}|{i}")]
               for i, (lbl, _) in enumerate(QUALITY_OPTIONS)]
    caption = (f"🎬 <b>{title}</b>\n\n👤 {uploader}\n"
               f"⏱️ {fmt_dur(info.get('duration'))} • 👁️ {fmt_views(info.get('view_count'))}\n\n"
               "<b>Select quality:</b>")
    try:
        if pending[key]["thumb"]:
            await status.delete()
            await m.reply_photo(pending[key]["thumb"], caption=caption,
                                reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await status.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        try: await status.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception: pass

@app.on_callback_query(filters.regex(r"^q\|"))
async def quality_cb(_, q: CallbackQuery):
    try:
        _, key, idx = q.data.split("|"); idx = int(idx)
    except ValueError:
        await q.answer("Invalid option.", show_alert=True); return
    data = pending.get(key)
    if not data:
        await q.answer("Session expired — send the link again.", show_alert=True); return

    ok, missing = await check_force_join(q.from_user.id)
    if not ok:
        await q.message.edit_text("🔒 <b>Please join our channels first:</b>",
                                  reply_markup=join_keyboard(missing))
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
    last = 0.0
    st = {"msg": q.message}

    async def safe_edit(text, markup=None):
        try: await st["msg"].edit_text(text, reply_markup=markup)
        except Exception: pass

    def progress_cb(frac, speed, eta):
        nonlocal last
        now = time.time()
        if now - last < 4: return
        last = now
        pct = int(frac * 100)
        txt = (f"⬇️ <b>Downloading</b>\n📌 {title}\n🎚️ {label}\n\n"
               f"{bar(frac)} {pct}%\n⚡ {fmt_speed(speed)} • ⏳ ETA {int(eta) if eta else '—'}s")
        asyncio.run_coroutine_threadsafe(safe_edit(txt, cancel_btn), loop)

    async def _run():
        try:
            path, real_title, info = await asyncio.to_thread(
                download_media, url, fmt, progress_cb)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("download failed")
            em = str(e)
            hint = ("\n\n💡 YouTube blocked this request (403). Try again later."
                    if "403" in em else "")
            await safe_edit(f"❌ <b>Download failed.</b>\n<code>{em[:250]}</code>{hint}")
            pending.pop(key, None); return

        size_mb = os.path.getsize(path) / (1024 * 1024)
        await safe_edit(f"⬆️ <b>Uploading</b>\n📌 {real_title}\n📦 {size_mb:.1f} MB\n\n<i>Please wait…</i>")
        caption = f"🎬 <b>{real_title}</b>\n🎚️ {label} • 📦 {size_mb:.1f} MB"
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
                    width=info.get("width") or None,
                    height=info.get("height") or None)
            bump_downloads(q.from_user.id)
            await safe_edit("✅ <b>Done!</b> Enjoy 🎉")
        except Exception as e:
            log.exception("upload failed")
            await safe_edit(f"❌ <b>Upload failed.</b>\n<code>{str(e)[:250]}</code>")
        finally:
            pending.pop(key, None); active_tasks.pop(key, None)
            try: os.remove(path)
            except OSError: pass

    task = asyncio.create_task(_run())
    active_tasks[key] = task
    try:
        await task
    except asyncio.CancelledError:
        await safe_edit("🛑 <b>Download cancelled.</b>")
        pending.pop(key, None)

@app.on_callback_query(filters.regex(r"^x\|"))
async def cancel_cb(_, q: CallbackQuery):
    _, key = q.data.split("|", 1)
    t = active_tasks.pop(key, None)
    if t and not t.done():
        t.cancel(); await q.answer("Cancelling…")
    else:
        await q.answer("Nothing to cancel.", show_alert=True)
    pending.pop(key, None)

@app.on_callback_query(filters.regex("^noop$"))
async def noop_cb(_, q: CallbackQuery):
    await q.answer("Open the channel and join.", show_alert=True)

@app.on_message(filters.command("ping"))
async def ping_cmd(_, m: Message):
    if not OWNER_ID or (m.from_user and m.from_user.id == OWNER_ID):
        t0 = time.time()
        msg = await m.reply_text("🏓 <i>ping…</i>")
        ms = int((time.time() - t0) * 1000)
        await msg.edit_text(f"🏓 <b>Pong!</b> {ms} ms ✅")

if __name__ == "__main__":
    init_db()
    Thread(target=run_health_server, daemon=True).start()
    log.info("starting downloader bot v3 (MTProto)…")
    app.run()
