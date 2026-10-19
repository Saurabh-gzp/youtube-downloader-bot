"""
Video Downloader Telegram Bot v4 — Button-based UI

- Pyrogram (MTProto) + yt-dlp, large files via MTProto
- YouTube 403 bypass via Android player client
- Full button menus: user menu + admin panel (no commands needed)
- Broadcast / block / stats / force-join channels, all via buttons
- Menu button (Start/Help) set automatically on startup

Env: API_ID, API_HASH, BOT_TOKEN, OWNER_ID, DOWNLOAD_DIR, PORT
"""
import os
import re
import time
import asyncio
import logging
import sqlite3
import urllib.request
import urllib.parse
import json
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
)
from pyrogram.errors import UserNotParticipant, ChannelPrivate

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
# per-user UI state: user_id -> {"mode": ..., "data": ...}
ui_state = {}

QUALITY_OPTIONS = [
    ("📱 360p",  "bv*[height<=360]+ba/b[height<=360]/b"),
    ("💻 720p",  "bv*[height<=720]+ba/b[height<=720]/b"),
    ("🖥️ 1080p", "bv*[height<=1080]+ba/b[height<=1080]/b"),
    ("🎞️ Best",  "bv*+ba/b"),
    ("🎵 Audio", "bestaudio/b"),
]

# ============ database ============
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
    c.commit(); c.close()

def get_setting(key, default=None):
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else default

def set_setting(key, value):
    c = db()
    c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
              (key, value))
    c.commit(); c.close()

def register_user(uid, username=None):
    c = db()
    c.execute("INSERT OR IGNORE INTO users (user_id,username) VALUES (?,?)",
              (uid, username))
    if username:
        c.execute("UPDATE users SET username=? WHERE user_id=?",
                  (username, uid))
    c.commit(); c.close()

def is_blocked(uid):
    c = db()
    r = c.execute("SELECT blocked FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return bool(r and r["blocked"])

def bump_downloads(uid):
    c = db()
    c.execute("UPDATE users SET downloads=downloads+1 WHERE user_id=?", (uid,))
    today = time.strftime("%Y-%m-%d")
    c.execute("INSERT INTO stats (date,downloads) VALUES (?,1) "
              "ON CONFLICT(date) DO UPDATE SET downloads=downloads+1", (today,))
    c.commit(); c.close()

def get_channels():
    c = db()
    rows = c.execute("SELECT * FROM channels ORDER BY added_at").fetchall()
    c.close()
    return rows

def add_channel_db(chat_id, title="", invite=""):
    c = db()
    c.execute("INSERT OR REPLACE INTO channels (chat_id,title,invite_link)"
              " VALUES (?,?,?)", (chat_id, title, invite))
    c.commit(); c.close()

def del_channel_db(chat_id):
    c = db()
    c.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
    c.commit(); c.close()

def get_recent_users(limit=10):
    c = db()
    rows = c.execute("SELECT user_id,username,blocked FROM users "
                     "ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return rows

# ============ health check ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a): pass

def run_health_server():
    try:
        HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))),
                   HealthHandler).serve_forever()
    except Exception as e:
        log.warning("health: %s", e)

# ============ yt-dlp ============
def extract_info(url: str) -> dict:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def download_media(url, format_id, progress_cb=None):
    import yt_dlp
    def hook(d):
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                try: progress_cb(done/total, d.get("speed") or 0, d.get("eta"))
                except Exception: pass
    is_audio = format_id.startswith("bestaudio")
    opts = {
        "format": format_id,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "socket_timeout": 60, "retries": 3, "progress_hooks": [hook],
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
            if os.path.exists(mp3): path = mp3
        elif not os.path.exists(path):
            mp4 = os.path.splitext(path)[0] + ".mp4"
            if os.path.exists(mp4): path = mp4
        return path, info.get("title", "video"), info

def fmt_views(n):
    if not n: return "N/A"
    n = int(n)
    for u, d in (("B",1e9),("M",1e6),("K",1e3)):
        if n >= d: return f"{n/d:.1f}{u}"
    return str(n)

def fmt_dur(s):
    if not s: return "N/A"
    s=int(s); h,r=divmod(s,3600); m,sec=divmod(r,60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

def bar(frac, w=12):
    f=int(frac*w); return "█"*f+"░"*(w-f)

def fmt_speed(bps):
    if not bps: return "—"
    for u in ("B/s","KB/s","MB/s"):
        if bps<1024: return f"{bps:.1f} {u}"
        bps/=1024
    return f"{bps:.1f} GB/s"

# ============ menu button (Telegram side menu) ============
def setup_menu_button():
    """Set the bot's side menu button with Start/Help via Bot HTTP API."""
    try:
        data = urllib.parse.urlencode({
            "menu_button": json.dumps({"type": "commands"}),
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
            data=data)
        urllib.request.urlopen(req, timeout=15).read()

        cmds = json.dumps([
            {"command": "start", "description": "🏠 Main menu"},
            {"command": "help", "description": "❓ How to use"},
        ])
        data = urllib.parse.urlencode({"commands": cmds}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
            data=data)
        urllib.request.urlopen(req, timeout=15).read()
        log.info("menu button + commands set")
    except Exception as e:
        log.warning("menu button setup failed: %s", e)

# ============ bot ============
app = Client("ytdl_bot", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, workers=16)

def is_owner_uid(uid):
    return OWNER_ID and uid == OWNER_ID

# ----- keyboards -----
def main_menu_kb(uid):
    rows = [
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ]
    if is_owner_uid(uid):
        rows.append([InlineKeyboardButton("🛠️ Admin Panel",
                                          callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics", callback_data="admin:stats")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin:broadcast")],
        [InlineKeyboardButton("👥 Users", callback_data="admin:users")],
        [InlineKeyboardButton("📢 Channels", callback_data="admin:channels")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")],
    ])

def channels_kb():
    rows = []
    for ch in get_channels():
        title = ch["title"] or str(ch["chat_id"])
        rows.append([InlineKeyboardButton(
            f"🗑️ {title}", callback_data=f"ch:del:{ch['chat_id']}")])
    fjoin = get_setting("fjoin", "off")
    rows.append([InlineKeyboardButton(
        f"🔒 Force-Join: {'✅ ON' if fjoin=='on' else '❌ OFF'}",
        callback_data="ch:toggle")])
    rows.append([InlineKeyboardButton("➕ Add Channel",
                                      callback_data="ch:add")])
    rows.append([InlineKeyboardButton("🔙 Admin Panel",
                                      callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)

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
    "💡 Tip: use 🛑 Cancel to stop a download."
)

async def check_force_join(uid):
    if get_setting("fjoin", "off") != "on":
        return True, []
    missing = []
    for ch in get_channels():
        try:
            await app.get_chat_member(ch["chat_id"], uid)
        except Exception:
            missing.append(dict(ch))
    return (len(missing) == 0), missing

def join_kb(missing):
    rows = []
    for ch in missing:
        label = ch.get("title") or str(ch["chat_id"])
        if ch.get("invite_link"):
            rows.append([InlineKeyboardButton(f"📢 Join {label}",
                                              url=ch["invite_link"])])
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify",
                                       callback_data="recheck")])
    return InlineKeyboardMarkup(rows)

# ----- commands (also reachable via menu button) -----
@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    register_user(m.from_user.id,
                  m.from_user.username if m.from_user else None)
    if is_blocked(m.from_user.id):
        return
    ok, missing = await check_force_join(m.from_user.id)
    if not ok:
        await m.reply_text("🔒 <b>Join our channels to continue:</b>",
                           reply_markup=join_kb(missing))
        return
    await m.reply_text(WELCOME, reply_markup=main_menu_kb(m.from_user.id))

@app.on_message(filters.command("help"))
async def help_cmd(_, m: Message):
    await m.reply_text(HELP_TEXT,
                       reply_markup=main_menu_kb(m.from_user.id if m.from_user else 0))

# ----- menu callbacks -----
@app.on_callback_query(filters.regex(r"^menu:"))
async def menu_cb(_, q: CallbackQuery):
    action = q.data.split(":", 1)[1]
    uid = q.from_user.id
    if action == "main":
        await q.message.edit_text(WELCOME, reply_markup=main_menu_kb(uid))
    elif action == "help":
        await q.message.edit_text(HELP_TEXT, reply_markup=main_menu_kb(uid))
    elif action == "admin":
        if not is_owner_uid(uid):
            await q.answer("Owner only.", show_alert=True); return
        await q.message.edit_text("🛠️ <b>Admin Panel</b>\nChoose an option:",
                                  reply_markup=admin_kb())
    await q.answer()

@app.on_callback_query(filters.regex(r"^admin:"))
async def admin_cb(_, q: CallbackQuery):
    if not is_owner_uid(q.from_user.id):
        await q.answer("Owner only.", show_alert=True); return
    action = q.data.split(":", 1)[1]

    if action == "stats":
        c = db()
        total = c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        blocked = c.execute("SELECT COUNT(*) c FROM users WHERE blocked=1").fetchone()["c"]
        dl = c.execute("SELECT COALESCE(SUM(downloads),0) s FROM users").fetchone()["s"]
        today = time.strftime("%Y-%m-%d")
        r = c.execute("SELECT downloads FROM stats WHERE date=?", (today,)).fetchone()
        ch = c.execute("SELECT COUNT(*) c FROM channels").fetchone()["c"]
        c.close()
        await q.message.edit_text(
            "📊 <b>Statistics</b>\n\n"
            f"👥 Users: <b>{total}</b>\n⛔ Blocked: <b>{blocked}</b>\n"
            f"⬇️ Downloads: <b>{dl}</b> (today: <b>{r['downloads'] if r else 0}</b>)\n"
            f"📢 Channels: <b>{ch}</b>",
            reply_markup=admin_kb())

    elif action == "broadcast":
        ui_state[q.from_user.id] = {"mode": "broadcast"}
        await q.message.edit_text(
            "📣 <b>Broadcast mode</b>\n\nSend the message to broadcast to all users "
            "(or /cancel).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Admin Panel",
                                       callback_data="menu:admin")]]))

    elif action == "users":
        rows = get_recent_users(10)
        if not rows:
            await q.message.edit_text("👥 No users yet.",
                                      reply_markup=admin_kb()); return
        kb = []
        for r in rows:
            name = f"@{r['username']}" if r['username'] else str(r['user_id'])
            icon = "⛔" if r['blocked'] else "✅"
            kb.append([InlineKeyboardButton(
                f"{icon} {name}",
                callback_data=f"user:{r['user_id']}")])
        kb.append([InlineKeyboardButton("🔙 Admin Panel",
                                        callback_data="menu:admin")])
        await q.message.edit_text("👥 <b>Recent users</b> — tap to block/unblock:",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "channels":
        await q.message.edit_text(
            "📢 <b>Force-Join Channels</b>\n\n"
            "Tap 🗑️ to remove. Add the bot as admin in a channel first, "
            "then use ➕ Add Channel.",
            reply_markup=channels_kb())
    await q.answer()

@app.on_callback_query(filters.regex(r"^user:"))
async def user_cb(_, q: CallbackQuery):
    if not is_owner_uid(q.from_user.id):
        await q.answer("Owner only.", show_alert=True); return
    uid = int(q.data.split(":")[1])
    c = db()
    r = c.execute("SELECT blocked,username FROM users WHERE user_id=?",
                  (uid,)).fetchone()
    blocked = not (r and r["blocked"])
    c.execute("UPDATE users SET blocked=? WHERE user_id=?",
              (1 if blocked else 0, uid))
    c.commit(); c.close()
    await q.answer(f"{'Blocked' if blocked else 'Unblocked'} {uid}")
    # refresh list
    rows = get_recent_users(10)
    kb = []
    for r2 in rows:
        name = f"@{r2['username']}" if r2['username'] else str(r2['user_id'])
        icon = "⛔" if r2['blocked'] else "✅"
        kb.append([InlineKeyboardButton(f"{icon} {name}",
                                        callback_data=f"user:{r2['user_id']}")])
    kb.append([InlineKeyboardButton("🔙 Admin Panel",
                                    callback_data="menu:admin")])
    await q.message.edit_text("👥 <b>Recent users</b> — tap to block/unblock:",
                              reply_markup=InlineKeyboardMarkup(kb))

@app.on_callback_query(filters.regex(r"^ch:"))
async def ch_cb(_, q: CallbackQuery):
    if not is_owner_uid(q.from_user.id):
        await q.answer("Owner only.", show_alert=True); return
    parts = q.data.split(":")
    if parts[1] == "toggle":
        cur = get_setting("fjoin", "off")
        set_setting("fjoin", "off" if cur == "on" else "on")
        await q.answer(f"Force-join {('OFF' if cur=='on' else 'ON')}")
    elif parts[1] == "del":
        del_channel_db(int(parts[2]))
        await q.answer("Channel removed.")
    elif parts[1] == "add":
        ui_state[q.from_user.id] = {"mode": "addchannel"}
        await q.message.edit_text(
            "➕ <b>Add Channel</b>\n\nSend the channel's chat ID "
            "(e.g. <code>-1001234567890</code>).\n"
            "The bot must be admin in that channel.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Channels",
                                       callback_data="admin:channels")]]))
        await q.answer()
        return
    await q.message.edit_text(
        "📢 <b>Force-Join Channels</b>", reply_markup=channels_kb())
    await q.answer()

# ----- text input for admin modes -----
@app.on_message(filters.text & ~filters.regex(URL_RE))
async def admin_text(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    state = ui_state.get(uid)
    if not state or not is_owner_uid(uid):
        return
    mode = state.get("mode")

    if mode == "broadcast":
        ui_state.pop(uid, None)
        c = db()
        users = c.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
        c.close()
        total, sent = len(users), 0
        status = await m.reply_text(f"📣 Broadcasting to {total} users…")
        for (u,) in users:
            try:
                await m.copy(u); sent += 1
            except Exception:
                pass
            if sent % 25 == 0:
                try: await status.edit_text(f"📣 {sent}/{total}…")
                except Exception: pass
        await status.edit_text(f"✅ Broadcast done: {sent}/{total} delivered.",
                               reply_markup=admin_kb())

    elif mode == "addchannel":
        ui_state.pop(uid, None)
        txt = (m.text or "").strip()
        try:
            chat_id = int(txt)
        except ValueError:
            await m.reply_text("❌ Send a numeric chat ID.",
                               reply_markup=channels_kb()); return
        try:
            chat = await app.get_chat(chat_id)
            title = chat.title or str(chat_id)
            invite = chat.invite_link or ""
            if not invite:
                try: invite = await app.export_chat_invite_link(chat_id)
                except Exception: pass
        except Exception as e:
            await m.reply_text(f"❌ Cannot access: {e}\nMake the bot admin there first.",
                               reply_markup=channels_kb()); return
        add_channel_db(chat_id, title, invite)
        await m.reply_text(f"✅ Added: <b>{title}</b>", reply_markup=channels_kb())

@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if uid in ui_state:
        ui_state.pop(uid, None)
        await m.reply_text("Cancelled.", reply_markup=main_menu_kb(uid))
        return
    cancelled = False
    for k in list(active_tasks):
        if k.startswith(f"{m.chat.id}:"):
            t = active_tasks.pop(k, None)
            if t and not t.done(): t.cancel(); cancelled = True
            pending.pop(k, None)
    await m.reply_text("🛑 Cancelled." if cancelled else "ℹ️ Nothing to cancel.")

@app.on_callback_query(filters.regex(r"^recheck$"))
async def recheck_cb(_, q: CallbackQuery):
    ok, _ = await check_force_join(q.from_user.id)
    if ok:
        await q.message.edit_text(WELCOME,
                                  reply_markup=main_menu_kb(q.from_user.id))
    else:
        await q.answer("Please join all channels first.", show_alert=True)

# ----- download flow -----
@app.on_message(filters.regex(URL_RE))
async def url_handler(_, m: Message):
    # skip if this text was meant for admin input
    uid = m.from_user.id if m.from_user else 0
    if uid in ui_state:
        return
    register_user(uid, m.from_user.username if m.from_user else None)
    if is_blocked(uid):
        return
    ok, missing = await check_force_join(uid)
    if not ok:
        rows = []
        for ch in missing:
            label = ch.get("title") or str(ch["chat_id"])
            if ch.get("invite_link"):
                rows.append([InlineKeyboardButton(f"📢 Join {label}",
                                                  url=ch["invite_link"])])
        rows.append([InlineKeyboardButton("✅ I've Joined — Verify",
                                           callback_data="recheck")])
        await m.reply_text("🔒 <b>Join our channels to continue:</b>",
                           reply_markup=InlineKeyboardMarkup(rows))
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
        hint = "\n\n💡 YouTube is rate-limiting — try again shortly." if "403" in err else ""
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
    ok, _ = await check_force_join(q.from_user.id)
    if not ok:
        await q.answer("Join channels first.", show_alert=True); return

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
            hint = ("\n\n💡 YouTube blocked this (403). Try again later."
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

@app.on_callback_query(filters.regex(r"^noop$"))
async def noop_cb(_, q: CallbackQuery):
    await q.answer("Open the channel and join.", show_alert=True)

@app.on_message(filters.command("ping"))
async def ping_cmd(_, m: Message):
    if not OWNER_ID or (m.from_user and m.from_user.id == OWNER_ID):
        t0 = time.time()
        msg = await m.reply_text("🏓 <i>ping…</i>")
        await msg.edit_text(f"🏓 <b>Pong!</b> {int((time.time()-t0)*1000)} ms ✅")

if __name__ == "__main__":
    init_db()
    setup_menu_button()
    Thread(target=run_health_server, daemon=True).start()
    log.info("starting downloader bot v4 (button UI)…")
    app.run()
