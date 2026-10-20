"""yt-dlp download engine: client retry, size guards, PO-token fallback."""
import os
import glob
import uuid
import shutil
import logging

import config
import utils

log = logging.getLogger("ytdl.engine")

_COOKIES_FILE = "/tmp/yt_cookies.txt"


def tasks_root() -> str:
    os.makedirs(config.TASK_DIR, exist_ok=True)
    return config.TASK_DIR


def new_task_dir() -> str:
    d = os.path.join(tasks_root(), uuid.uuid4().hex[:12])
    os.makedirs(d, exist_ok=True)
    return d


def cleanup_task_dir(d: str) -> None:
    shutil.rmtree(d, ignore_errors=True)


def _cookiefile() -> str:
    raw = config.YOUTUBE_COOKIES
    if not raw:
        return ""
    if raw.startswith("/") and os.path.isfile(raw):
        return raw
    try:
        import base64
        data = base64.b64decode(raw, validate=True)
    except Exception:
        data = raw.replace("\\n", "\n").encode()
    try:
        with open(_COOKIES_FILE, "wb") as f:
            f.write(data)
        return _COOKIES_FILE
    except OSError:
        return ""


def _base_opts(client: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "nocheckcertificate": True,
    }
    if client and client != "default":
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    cf = _cookiefile()
    if cf:
        opts["cookiefile"] = cf
    return opts


_SOFT_ERRORS = ("403", "forbidden", "sign in to confirm", "rate limit",
                "unavailable", "blocked", "unable to download", "precondition",
                "requested format is not available", "no video formats",
                "needs to be reloaded", "reload", "captcha", "confirm you",
                "not a bot", "playback", "nsfwi", "age")


def _info_score(info: dict) -> tuple:
    """Rank an info dict by how rich its format list is.

    (max_height, n_video_formats, n_formats) — higher is better. Some player
    clients (notably android) return a tiny format list capped at 360p without
    erroring, so we extract from every client and keep the best one.
    Only formats with a usable URL count — some clients list formats that
    cannot actually be downloaded (SABR / PO-token gated).
    """
    fmts = [f for f in (info.get("formats") or [])
            if f.get("url") or f.get("fragments") or f.get("manifest_url")]
    vids = [f for f in fmts if f.get("vcodec") not in (None, "none")]
    heights = [(f.get("height") or 0) for f in vids]
    return (max(heights) if heights else 0, len(vids), len(fmts))


def extract_info(url: str, clients=None) -> tuple:
    """Extract from every player client and return the RICHEST result.

    Returns (info_dict, client_used). Clients that soft-fail (403, rate
    limit…) are skipped; the remaining results are ranked by format-list
    richness so the user always sees every quality the video really has.
    Runs in a worker thread — never call from the event loop directly.
    """
    import yt_dlp
    clients = list((clients or config.YT_CLIENTS)[:5])  # cap latency
    best = None            # (score, info, client)
    last_err = None
    for client in clients:
        try:
            opts = _base_opts(client)
            opts["skip_download"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                score = _info_score(info)
                if best is None or score > best[0]:
                    best = (score, info, client)
                # HD in hand and the list is rich — no need to try more clients
                if score[0] >= 1080 and score[1] >= 6:
                    break
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            last_err = e
            if any(p in msg.lower() for p in _SOFT_ERRORS):
                log.warning("extract failed with client=%s: %.120s", client, msg)
                continue
            raise
        except Exception as e:
            last_err = e
            continue
    if best:
        return best[1], best[2]
    raise last_err or RuntimeError("extract failed")


def estimate_size(info: dict, quality: dict) -> int:
    """Best-effort size estimate (bytes) for a quality option, 0 if unknown."""
    formats = info.get("formats") or []

    def fsize(f):
        return f.get("filesize") or f.get("filesize_approx") or 0

    if quality.get("audio"):
        audios = [fsize(f) for f in formats
                  if f.get("acodec") not in (None, "none")]
        return int(max(audios) * 1.05) if audios else 0

    h = quality.get("h")
    vids = [f for f in formats
            if f.get("vcodec") not in (None, "none")
            and (f.get("height") or 0) > 0
            and (h is None or f["height"] <= h)]
    if not vids:
        return 0
    vsize = max(fsize(f) for f in vids)
    audios = [fsize(f) for f in formats
              if f.get("acodec") not in (None, "none")
              and f.get("vcodec") in (None, "none")]
    asize = max(audios) if audios else 0
    return int((vsize + asize) * 1.05)


_EXTRA_FALLBACK_CLIENTS = ("mweb", "tv", "web_safari", "android_vr")
_EXCLUDE_CLIENTS = {"android"}  # capped at 360p without PO token — useless for downloads


def _client_chain(primary: str) -> list:
    """Primary client first, then proven fallbacks — capped to 4 attempts."""
    chain = [primary] if primary and primary not in _EXCLUDE_CLIENTS else []
    for c in tuple(_EXTRA_FALLBACK_CLIENTS) + tuple(config.YT_CLIENTS):
        if c not in chain and c not in _EXCLUDE_CLIENTS:
            chain.append(c)
    return chain[:4]


def download_media(url: str, fmt: str, client: str, task_dir: str,
                   progress_cb=None, cancelled=None) -> tuple:
    """Download with automatic client fallback on YouTube IP blocks.

    YouTube 403s ("unable to download video data") are format-URL blocks
    tied to the player client that issued them — switching to another
    client (fresh URLs + fresh PO token) usually succeeds. Retries are
    bounded to the first 4 clients in the chain.
    """
    import yt_dlp
    chain = _client_chain(client)
    last = None
    for attempt, c in enumerate(chain):
        if cancelled is not None and cancelled():
            raise yt_dlp.utils.DownloadCancelled()
        try:
            return _download_once(url, fmt, c, task_dir, progress_cb, cancelled)
        except yt_dlp.utils.DownloadCancelled:
            raise
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            last = e
            hard = any(p in msg for p in
                       ("403", "forbidden", "sign in", "not a bot",
                        "rate limit", "precondition", "unavailable",
                        "removed", "unable to download"))
            if not hard:
                raise
            nxt = chain[attempt + 1] if attempt + 1 < len(chain) else None
            if not nxt:
                raise
            log.warning("download failed with client=%s (%.140s) — retrying with %s",
                        c, str(e), nxt)
            # clean partial files so the next attempt starts fresh
            for pat in ("*.part", "*.ytdl", "*.f*.part-Frag*"):
                for p in glob.glob(os.path.join(task_dir, pat)):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            if progress_cb:
                try:
                    progress_cb(0.0, 0, 0)
                except Exception:
                    pass
    raise last or RuntimeError("download failed")


def _download_once(url: str, fmt: str, client: str, task_dir: str,
                   progress_cb=None, cancelled=None) -> tuple:
    """Single download attempt with one player client."""
    import yt_dlp

    def hook(d):
        if cancelled is not None and cancelled():
            raise yt_dlp.utils.DownloadCancelled()
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                try:
                    progress_cb(done / total, d.get("speed") or 0, d.get("eta"))
                except Exception:
                    pass

    is_audio = fmt.endswith("bestaudio") or "bestaudio" in fmt.split("/")
    opts = _base_opts(client)
    opts.update({
        "format": fmt,
        "outtmpl": os.path.join(task_dir, "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "concurrent_fragment_downloads": 2,   # keep RAM low on small containers
        "http_chunk_size": 10 * 1024 * 1024,  # 10MB chunks → bounded memory
        "max_filesize": int(config.MAX_FILE_GB * (1024 ** 3)) or None,
        # resolution first, then Telegram-friendly codecs at equal resolution
        "format_sort": ["res", "vcodec:h264", "acodec:aac"],
    })
    if is_audio:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["merge_output_format"] = "mp4"

    before = set(glob.glob(os.path.join(task_dir, "*")))
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "video")

    after = set(glob.glob(os.path.join(task_dir, "*")))
    new_files = [f for f in (after - before)
                 if os.path.isfile(f) and os.path.getsize(f) > 1024]
    if not new_files:
        with yt_dlp.YoutubeDL(opts) as ydl2:
            guess = ydl2.prepare_filename(info)
        for cand in (guess,
                     os.path.splitext(guess)[0] + ".mp4",
                     os.path.splitext(guess)[0] + ".mp3",
                     os.path.splitext(guess)[0] + ".mkv"):
            if os.path.isfile(cand) and os.path.getsize(cand) > 1024:
                return cand, title, info
        raise FileNotFoundError(
            "Download finished but no file was produced — the site may have blocked it.")
    return max(new_files, key=os.path.getsize), title, info


def make_thumbnail(video_path: str, out_jpg: str) -> str:
    """Grab a frame at 2s as an upload thumbnail. Returns path or ''."""
    try:
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", "2",
             "-i", video_path, "-frames:v", "1", "-vf", "scale=320:-2",
             out_jpg],
            timeout=30, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if os.path.isfile(out_jpg) and os.path.getsize(out_jpg) > 1024:
            return out_jpg
    except Exception:
        pass
    return ""


def platform_of(url: str) -> str:
    u = url.lower()
    for name in ("youtube", "youtu.be", "instagram", "tiktok", "twitter",
                 "x.com", "facebook", "fb.watch", "pinterest", "dailymotion",
                 "vimeo", "reddit", "soundcloud", "twitch"):
        if name in u:
            return name.split(".")[0].capitalize()
    return "Web"
