"""Entry point — Video Downloader Bot v5 (yt-dlp + MongoDB edition).

Env: API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MONGODB_URI, MONGODB_DB,
     DOWNLOAD_DIR, PORT, MAX_CONCURRENT, COOLDOWN_SECONDS, MAX_FILE_GB,
     YT_CLIENTS, YOUTUBE_COOKIES
"""
import os
import json
import shutil
import asyncio
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, idle
from pyrogram.enums import ParseMode

import config
import utils
import db as dbmod
import handlers
import admin
import engine

log = config.log

app = Client(
    "ytdl_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workers=16,
    parse_mode=ParseMode.HTML,
    sleep_threshold=15,
)
database = dbmod.Database(config.MONGODB_URI, config.MONGODB_DB)

handlers.register(app, database)
admin.register(app, database)

# ---------------- health endpoint (JSON) ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "ok",
            "version": config.VERSION,
            "uptime_s": int(time.time() - utils.START_TS),
            "active_downloads": len(utils.ACTIVE_TASKS),
            "queue": utils.WAITING,
            "mongo": database.ok,
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
    import urllib.request
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
    url = os.environ.get("APP_URL", "").strip()
    if not url:
        return
    while True:
        time.sleep(240)
        try:
            urllib.request.urlopen(url, timeout=20).read()
        except Exception:
            pass


async def run_bot():
    """One full bot lifecycle: mongo, menu, start, notify, idle."""
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

    await asyncio.to_thread(setup_menu_button)

    await app.start()
    me = await app.get_me()
    log.info("Bot live as @%s (id=%s)", me.username, me.id)
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
    await idle()
    janitor.cancel()
    await app.stop()
    log.info("Bot stopped cleanly.")


async def main():
    log.info("Video Downloader Bot v%s starting…", config.VERSION)
    startup_cleanup()
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=keepalive_loop, daemon=True).start()

    # supervisor: never let a crash take the container down for good
    backoff = 5
    while True:
        try:
            await run_bot()
            break  # clean shutdown
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("bot crashed: %s — restarting in %ds", e, backoff)
            try:
                await app.stop()
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    asyncio.run(main())
