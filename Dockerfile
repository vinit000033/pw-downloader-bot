# syntax=docker/dockerfile:1.6

# ==============================================================
# STAGE 1 — builder
# Compiles native deps (tgcrypto, curl_cffi) and produces wheels.
# ==============================================================
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ python3-dev libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels

# Runtime deps of the bot. Pinned to sane minimums; yt-dlp left
# unpinned so rebuilds pick up the latest extractor fixes.
RUN pip wheel --wheel-dir=/wheels \
        "pyrogram>=2,<3" \
        tgcrypto \
        "aiohttp>=3.9" \
        pyleaves \
        yt-dlp \
        "curl_cffi>=0.7"


# ==============================================================
# STAGE 2 — runtime
# Slim image with only what's needed at run time.
# ==============================================================
FROM python:3.11-slim

# ffmpeg     — HLS→MP4, remux, split
# tini       — clean SIGTERM propagation to ffmpeg children
# ca-certs   — HTTPS for yt-dlp / aiohttp / Gofile
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Reuse wheels from the builder stage (no compilers in final image)
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* \
    && rm -rf /wheels

# Bot source
COPY bot.py .

# Persistent volume for user_settings.json
RUN mkdir -p /data

# -------------------- runtime config --------------------
# PORT               : health-check server (HF Spaces / reverse proxy)
# SETTINGS_FILE      : persisted to the /data volume
# CONCURRENT_FRAGMENTS: parallel HLS fragment downloads
# MAX_GLOBAL_TASKS   : global concurrent jobs
# MAX_PER_USER       : per-user concurrent jobs
# SPLIT_MB           : default split threshold for large files
# GOFILE_TOKEN       : if unset → guest uploads (each upload = new guest)
# GOFILE_FOLDER_ID   : optional Gofile folder for uploads
# -----------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    SETTINGS_FILE=/data/user_settings.json \
    CONCURRENT_FRAGMENTS=16 \
    MAX_GLOBAL_TASKS=20 \
    MAX_PER_USER=6 \
    SPLIT_MB=2000 \
    GOFILE_TOKEN="" \
    GOFILE_FOLDER_ID=""

EXPOSE 7860

# Health-check hits the in-process HTTP server in bot.py
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
        sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}', timeout=5).status==200 else 1)"

# tini as PID 1 → graceful shutdown of the bot + its ffmpeg subprocesses
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]
