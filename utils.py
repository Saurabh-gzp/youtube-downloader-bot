"""Formatting helpers, safe edit, cooldowns and shared runtime state."""
import html
import re
import time
import asyncio
import logging

from pyrogram.types import InlineKeyboardMarkup

import config

log = logging.getLogger("ytdl.utils")

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# ---- shared runtime state ----
PENDING: dict = {}        # key -> {"type": "url"|"search", ...,"ts": float}
ACTIVE_TASKS: dict = {}   # key -> asyncio.Task
USER_ACTIVE: dict = {}    # user_id -> key (one download per user)
CANCEL_FLAGS: dict = {}   # key -> bool (cooperative cancel for yt-dlp thread)
WAITING: int = 0          # download queue length
LAST_REQ: dict = {}       # user_id -> ts (cooldown)
BCAST_STOP: dict = {}     # owner_id -> bool
UI_STATE: dict = {}       # user_id -> {"mode": "addchannel"|"broadcast_confirm", ...}

START_TS = time.time()
LAST_UPDATE_TS: float = 0.0   # last Telegram update received (0 = none yet)


def esc(s) -> str:
    """Escape a dynamic string for HTML parse mode."""
    return html.escape(str(s or ""), quote=False)


def is_owner(uid: int) -> bool:
    return bool(config.OWNER_ID) and uid == config.OWNER_ID


def fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_dur(s) -> str:
    if not s:
        return "—"
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_views(n) -> str:
    if not n:
        return "—"
    n = int(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def fmt_speed(bps) -> str:
    if not bps:
        return "—"
    bps = float(bps)
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024:
            return f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} GB/s"


def bar(frac: float, width: int = 14) -> str:
    frac = max(0.0, min(1.0, float(frac or 0)))
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


def uptime() -> str:
    s = int(time.time() - START_TS)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}h {m}m {sec}s"


# ---- cooldowns ----
def on_cooldown(uid: int) -> int:
    """Seconds remaining, 0 if allowed. Owner is exempt."""
    if is_owner(uid) or config.COOLDOWN_SECONDS <= 0:
        return 0
    last = LAST_REQ.get(uid, 0)
    remain = config.COOLDOWN_SECONDS - int(time.time() - last)
    return max(0, remain)


def mark_request(uid: int) -> None:
    LAST_REQ[uid] = time.time()


# ---- safe message editing ----
async def safe_edit(msg, text: str, markup: InlineKeyboardMarkup = None) -> None:
    if msg is None:
        return
    try:
        await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        pass


# ---- pending store with TTL ----
def pending_put(key: str, data: dict) -> None:
    data["ts"] = time.time()
    PENDING[key] = data


def pending_get(key: str):
    item = PENDING.get(key)
    if item and time.time() - item.get("ts", 0) > config.PENDING_TTL:
        PENDING.pop(key, None)
        return None
    return item


def pending_pop(key: str):
    return PENDING.pop(key, None)


def cleanup_stale() -> int:
    now = time.time()
    stale = [k for k, v in PENDING.items() if now - v.get("ts", 0) > config.PENDING_TTL]
    for k in stale:
        PENDING.pop(k, None)
    if len(LAST_REQ) > 5000:
        LAST_REQ.clear()
    return len(stale)


# ---- background janitor ----
async def janitor_loop() -> None:
    """Periodic cleanup: stale sessions + old temp files."""
    import os
    import shutil
    from engine import tasks_root

    while True:
        try:
            await asyncio.sleep(300)
            n = cleanup_stale()
            if n:
                log.info("janitor: dropped %d stale sessions", n)
            root = tasks_root()
            if os.path.isdir(root):
                cutoff = time.time() - 7200
                for name in os.listdir(root):
                    p = os.path.join(root, name)
                    try:
                        if os.path.getmtime(p) < cutoff:
                            shutil.rmtree(p, ignore_errors=True)
                            if os.path.isfile(p):
                                os.remove(p)
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("janitor error")
            await asyncio.sleep(60)
