"""Entry point — Video Downloader Bot v5.2 (yt-dlp + MongoDB, 24/7 hardened).

Env: API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MONGODB_URI, MONGODB_DB,
     DOWNLOAD_DIR, PORT, APP_URL/PUBLIC_URL, MAX_CONCURRENT, COOLDOWN_SECONDS,
     MAX_FILE_GB, YT_CLIENTS, YOUTUBE_COOKIES

24/7 design:
  - supervisor loop restarts the bot lifecycle FOREVER (crash or recycle)
  - watchdog force-recycles the lifecycle if Telegram stays disconnected >90s
  - health server reports Telegram connection state (tg) + last update age
  - keepalive self-pings PUBLIC_URL so the platform never idles the container
"""
import os
import json
import shutil
import asyncio
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.handlers import RawUpdateHandler

import config
import utils
import db as dbmod
import handlers
import admin
import engine

log = config.log

# ---- shared holders (health thread reads them) ----
_APP_HOLDER = []                    # [current Client or empty]
_RESTART_FLAG = {"v": False}        # set by watchdog → run_bot exits cleanly
_POT_OK = {"v": False}              # bgutil PO-token server reachable?


def pot_check():
    """Probe the bgutil PO-token server (started alongside the bot in the
    container). Fixes YouTube 403 on datacenter IPs — see Dockerfile."""
    for _ in range(5):
        try:
            with urllib.request.urlopen("http://127.0.0.1:4416/ping",
                                        timeout=4) as r:
                if r.status == 200:
                    _POT_OK["v"] = True
                    return
        except Exception:
            time.sleep(3)


def make_client() -> Client:
    return Client(
        "ytdl_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workers=16,
        parse_mode=ParseMode.HTML,
        sleep_threshold=15,
    )


database = dbmod.Database(config.MONGODB_URI, config.MONGODB_DB)


async def on_raw_update(_client, _update, _users, _chats):
    """Timestamp every incoming Telegram update (health shows delivery age)."""
    utils.LAST_UPDATE_TS = time.time()


# ---------------- health endpoint (JSON) ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        app = _APP_HOLDER[0] if _APP_HOLDER else None
        try:
            tg = bool(app.is_connected) if app else False
        except Exception:
            tg = False
        last = (int(time.time() - utils.LAST_UPDATE_TS)
                if utils.LAST_UPDATE_TS else -1)
        body = json.dumps({
            "status": "ok" if tg else "degraded",
            "version": config.VERSION,
            "uptime_s": int(time.time() - utils.START_TS),
            "tg": tg,
            "last_tg_update_ago_s": last,
            "active_downloads": len(utils.ACTIVE_TASKS),
            "queue": utils.WAITING,
            "mongo": database.ok,
            "pot": _POT_OK["v"],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def run_health_server():
    try:
        HTTPServer(("0.0.0.0", config.PORT), HealthHandler).serve_forever()
    except Exception as e:
        log.warning("health server stopped: %s", e)


def setup_menu_button():
    """Register commands + menu button via the Bot HTTP API."""
    import urllib.parse
    try:
        data = urllib.parse.urlencode({
            "commands": json.dumps([
                {"command": "start", "description": "🏠 Main menu"},
                {"command": "help", "description": "❓ How to use"},
                {"command": "cancel", "description": "🛑 Cancel download"},
                {"command": "ping", "description": "🏓 Check status"},
            ]),
        }).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/setMyCommands",
            data=data), timeout=15).read()
        log.info("bot commands registered")
    except Exception as e:
        log.warning("menu setup failed: %s", e)


def startup_cleanup():
    """Wipe leftover temp downloads from previous runs."""
    try:
        shutil.rmtree(config.TASK_DIR, ignore_errors=True)
        os.makedirs(config.TASK_DIR, exist_ok=True)
        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    except OSError:
        pass


def keepalive_loop():
    """Self-ping the public URL every 4 minutes so the container never
    counts as idle (platforms sometimes sleep apps on HTTP inactivity,
    which would take a polling Telegram bot offline)."""
    url = config.PUBLIC_URL
    if not url:
        return
    while True:
        time.sleep(240)
        try:
            urllib.request.urlopen(url, timeout=20).read()
        except Exception:
            pass


async def watchdog_loop(app: Client):
    """Force a full lifecycle recycle if Telegram stays disconnected >90s.

    Pyrogram reconnects on its own, but if it gets stuck this brings the
    bot back without needing a container restart."""
    disc_since = None
    while True:
        await asyncio.sleep(15)
        try:
            connected = bool(app.is_connected)
        except Exception:
            connected = False
        if connected:
            disc_since = None
            continue
        now = time.time()
        if disc_since is None:
            disc_since = now
        elif now - disc_since > 90:
            log.warning("watchdog: TG disconnected >90s — forcing lifecycle recycle")
            _RESTART_FLAG["v"] = True
            return


async def run_bot():
    """One full bot lifecycle: mongo, handlers, start, notify, serve.
    Returns (cleanly) only when a restart is requested."""
    _RESTART_FLAG["v"] = False
    app = make_client()                 # fresh Client every lifecycle
    _APP_HOLDER.clear()
    _APP_HOLDER.append(app)
    handlers.register(app, database)
    admin.register(app, database)
    app.add_handler(RawUpdateHandler(on_raw_update), -3)

    for attempt in range(1, 4):
        if await database.ping():
            break
        log.warning("MongoDB unreachable (attempt %d/3)%s", attempt,
                    " — continuing WITHOUT persistence" if attempt == 3 else "")
        if attempt < 3:
            await asyncio.sleep(5)
    await database.ensure_indexes()
    if database.ok:
        log.info("MongoDB connected: db=%s", config.MONGODB_DB)
    else:
        log.error("MongoDB DOWN — bot runs in degraded mode (no persistence). "
                  "Check MONGODB_URI / Atlas network access (allow 0.0.0.0/0).")

    await asyncio.to_thread(pot_check)
    if _POT_OK["v"]:
        log.info("POT server reachable — YouTube 403 protection ON")
    else:
        log.warning("POT server NOT reachable — script-mode fallback will be used")

    await app.start()
    me = await app.get_me()
    log.info("Bot live as @%s (id=%s)", me.username, me.id)
    utils.LAST_UPDATE_TS = 0.0
    if config.OWNER_ID:
        try:
            await app.send_message(
                config.OWNER_ID,
                f"🚀 <b>Video Downloader Bot v{config.VERSION} started</b>\n"
                f"⬇️ yt-dlp engine ready (up to 1080p + MP3)\n"
                f"🗄️ Mongo: {'✅' if database.ok else '❌ down'}\n"
                f"⚙️ Max concurrent: {config.MAX_CONCURRENT}")
        except Exception as e:
            log.warning("owner notify failed: %s", e)

    janitor = asyncio.create_task(utils.janitor_loop())
    watchdog = asyncio.create_task(watchdog_loop(app))
    try:
        while not _RESTART_FLAG["v"]:
            await asyncio.sleep(5)
        log.warning("restart requested — recycling bot lifecycle")
    finally:
        watchdog.cancel()
        janitor.cancel()
        try:
            await app.stop()
        except Exception:
            pass


async def main():
    log.info("Video Downloader Bot v%s starting…", config.VERSION)
    startup_cleanup()
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=keepalive_loop, daemon=True).start()
    await asyncio.to_thread(setup_menu_button)

    # supervisor: 24/7 — recycle the lifecycle forever, back off on crashes
    backoff = 5
    while True:
        try:
            await run_bot()
            await asyncio.sleep(2)      # clean recycle → immediate restart
            backoff = 5
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("bot crashed: %s — restarting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    asyncio.run(main())
