"""Central configuration — all env vars in one place."""
import os
import logging


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


API_ID = _int("API_ID", 0)
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = _int("OWNER_ID", 0)

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise SystemExit(
        "Missing required env vars: API_ID / API_HASH / BOT_TOKEN. "
        "Get API_ID & API_HASH from https://my.telegram.org and BOT_TOKEN from @BotFather."
    )

# --- storage ---
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/ytdl").strip() or "/tmp/ytdl"
TASK_DIR = os.path.join(DOWNLOAD_DIR, "tasks")

# --- MongoDB (persistent storage) ---
MONGODB_URI = os.environ.get("MONGODB_URI", "").strip()
MONGODB_DB = os.environ.get("MONGODB_DB", "ytdl_bot").strip() or "ytdl_bot"

# --- limits & tuning ---
PORT = _int("PORT", 8000)
MAX_CONCURRENT = max(1, _int("MAX_CONCURRENT", 2))          # parallel downloads
COOLDOWN_SECONDS = max(0, _int("COOLDOWN_SECONDS", 15))      # per-user anti-spam
MAX_FILE_GB = _float("MAX_FILE_GB", 1.9)                     # Telegram bot upload limit is 2GB
PENDING_TTL = max(300, _int("PENDING_TTL", 1800))            # stale menu/session lifetime (s)
ASK_COOLDOWN = max(5, _int("ASK_COOLDOWN", 25))              # /ask AI cooldown per user
MAX_BROADCAST_SPEED = max(5, _int("BROADCAST_PER_SECOND", 20))

# --- yt-dlp ---
# Player clients tried in order until one works (YouTube 403 bypass).
YT_CLIENTS = [c.strip() for c in os.environ.get(
    "YT_CLIENTS", "android,web_safari,android_vr,web").split(",") if c.strip()]
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "").strip()  # raw cookies.txt content or path

# --- AI (OpenRouter) ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
AI_MODEL = os.environ.get("AI_MODEL", "openai/gpt-4o-mini").strip()
AI_ENABLED = OPENROUTER_API_KEY.startswith("sk-or")          # guard against wrong keys

VERSION = "5.0"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ytdl")
