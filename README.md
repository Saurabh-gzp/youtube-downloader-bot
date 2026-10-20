# YouTube Downloader Telegram Bot v5

MTProto-based (Pyrogram) video downloader with **MongoDB persistence**, live
search, AI assistant, queue system and a full admin panel.

## Features
- 🔗 Send any link (YouTube + 1000+ sites) → quality buttons with **size estimates**
- 🔍 **Search**: type a song/movie name → top 5 YouTube results as buttons
- 🤖 **AI**: `/ask` command via OpenRouter (`OPENROUTER_API_KEY`), AI-cleaned search queries
- 🍃 **MongoDB**: users, force-join channels, settings, stats and download logs survive restarts
- ⚙️ **Queue + concurrency limit** (`MAX_CONCURRENT`), one download per user, per-user cooldown
- 📦 **Size guard**: refuses files over `MAX_FILE_GB` (Telegram bot limit = 2 GB) *before* downloading
- 📊 Live progress bars for **both download and upload**, auto thumbnail, streaming video
- 🛡️ YouTube 403 bypass: multi-client retry (`YT_CLIENTS`) + optional `YOUTUBE_COOKIES`
- 🛠️ Admin panel: stats, maintenance mode, paginated user list (block/unblock), force-join channels, FloodWait-safe broadcast with stop button
- 🩺 Health endpoint on `$PORT` returning JSON status

## Env vars
| Var | Required | Default | Notes |
|-----|----------|---------|-------|
| `API_ID` / `API_HASH` | ✅ | — | from my.telegram.org |
| `BOT_TOKEN` | ✅ | — | from @BotFather |
| `OWNER_ID` | – | 0 | Telegram user id of owner (admin panel) |
| `MONGODB_URI` | recommended | — | `mongodb+srv://user:pass@cluster/` |
| `MONGODB_DB` | – | `ytdl_bot` | database name |
| `DOWNLOAD_DIR` | – | `/tmp/ytdl` | temp download dir (wiped on restart) |
| `PORT` | – | `8000` | health-check port (set by platform) |
| `MAX_CONCURRENT` | – | `2` | parallel downloads |
| `COOLDOWN_SECONDS` | – | `15` | per-user anti-spam |
| `MAX_FILE_GB` | – | `1.9` | refuse bigger files before download |
| `YT_CLIENTS` | – | `android,web_safari,android_vr,web` | yt-dlp player clients, tried in order |
| `YOUTUBE_COOKIES` | – | — | raw cookies.txt content (or path) for age-restricted videos |
| `OPENROUTER_API_KEY` | – | — | enables `/ask` + AI search (must start with `sk-or-`) |
| `AI_MODEL` | – | `openai/gpt-4o-mini` | any OpenRouter model id |

## Run locally
```bash
pip install -r requirements.txt
export API_ID=... API_HASH=... BOT_TOKEN=... MONGODB_URI=...
python bot.py
```

**Note:** `ffmpeg` must be installed (`apt install ffmpeg`) — used for merging and thumbnails.

## Deploy (Antideploy)
Push this folder (`POST /api/v1/deploy`) — `Dockerfile` and `Procfile` are ready.
Secrets (BOT_TOKEN, MONGODB_URI, …) are set in the Antideploy console; they are
applied on the next deploy/redeploy.
