FROM python:3.11-slim

# ffmpeg for yt-dlp merging + thumbnails; curl for the deno download
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# deno JS runtime — yt-dlp 2026+ REQUIRES a JS runtime for YouTube JS
# challenges, and the bgutil PO-token provider uses it to mint tokens.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) DA=x86_64 ;; \
        arm64) DA=aarch64 ;; \
        *) echo "unsupported architecture" && exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/deno.zip \
        "https://github.com/denoland/deno/releases/latest/download/deno-${DA}-unknown-linux-gnu.zip"; \
    python -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')"; \
    chmod +x /usr/local/bin/deno; \
    deno --version

# bgutil PO-token provider server — mints Proof-of-Origin tokens that fix
# YouTube's "403 unable to download video data" / "sign in to confirm you're
# not a bot" on datacenter IPs. Cloned to the plugin's default lookup path.
RUN set -eux; \
    curl -fsSL \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/heads/master.tar.gz \
        | tar xz -C /root; \
    mv /root/bgutil-ytdlp-pot-provider-master /root/bgutil-ytdlp-pot-provider; \
    cd /root/bgutil-ytdlp-pot-provider/server && deno install

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt bgutil-ytdlp-pot-provider
COPY . .

# Start the PO-token server (127.0.0.1:4416), then the bot.
# If the POT server dies, yt-dlp falls back to script-deno mode automatically.
CMD ["sh", "-c", "deno run --allow-all /root/bgutil-ytdlp-pot-provider/server/src/main.ts & exec python bot.py"]
