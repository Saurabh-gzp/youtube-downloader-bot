"""yt-dlp download engine: client retry, size guards, cooperative cancel."""
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
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    cf = _cookiefile()
    if cf:
        opts["cookiefile"] = cf
    return opts


_SOFT_ERRORS = ("403", "forbidden", "sign in to confirm", "rate limit",
                "unavailable", "blocked", "unable to download", "precondition")


def extract_info(url: str, clients=None) -> tuple:
    """Try each player client until one works.

    Returns (info_dict, client_used). Raises the last error if all fail.
    Runs in a worker thread — never call from the event loop directly.
    """
    import yt_dlp
    clients = clients or config.YT_CLIENTS
    last_err = None
    for client in clients:
        try:
            opts = _base_opts(client)
            opts["skip_download"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                return info, client
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


def download_media(url: str, fmt: str, client: str, task_dir: str,
                   progress_cb=None, cancelled=None) -> tuple:
    """Download to task_dir. Returns (path, title, info).

    progress_cb(frac, speed, eta) is called from this thread.
    cancelled() -> bool is checked in the progress hook to abort cleanly.
    """
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
        "concurrent_fragment_downloads": 3,
        "max_filesize": int(config.MAX_FILE_GB * (1024 ** 3)) or None,
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
