#!/usr/bin/env python3
"""
EduStream Bot - YouTube-style downloader → Telegram DM (fallback: Gofile).

Highlights:
  • CloudFront signed cookies (?d=…), EduStream play.php auto-resolve
  • AES-128 HLS with manual segment fetch (cookie-safe)
  • Per-user delivery: Telegram DM or Gofile-only
  • Leech-style settings buttons
  • Group completion notification (pings user + full stats), link stays in DM
"""

import os
import re
import time
import json
import uuid
import shutil
import asyncio
import urllib.parse
import tempfile
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)

# ---------- optional deps ----------
try:
    from pyleaves import Leaves
    PYLEAVES_OK = True
except ImportError:
    Leaves = None
    PYLEAVES_OK = False

try:
    import yt_dlp
    YTDLP_OK = True
    YTDLP_VERSION = getattr(yt_dlp.version, "__version__", "unknown")
except ImportError:
    yt_dlp = None
    YTDLP_OK = False
    YTDLP_VERSION = None

try:
    import curl_cffi
    CURL_CFFI_OK = True
    CURL_CFFI_VERSION = getattr(curl_cffi, "__version__", "unknown")
except ImportError:
    CURL_CFFI_OK = False
    CURL_CFFI_VERSION = None


# ---------- Health server ----------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a, **kw):
        pass


def _run_health():
    port = int(os.environ.get("PORT", 7860))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


threading.Thread(target=_run_health, daemon=True).start()


# ---------- Config ----------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]

CONCURRENT_FRAGMENTS = int(os.environ.get("CONCURRENT_FRAGMENTS", "32"))
MAX_GLOBAL = int(os.environ.get("MAX_GLOBAL_TASKS", "10"))
MAX_PER_USER = int(os.environ.get("MAX_PER_USER", "3"))

GOFILE_TOKEN = os.environ.get("GOFILE_TOKEN", "")
GOFILE_FOLDER = os.environ.get("GOFILE_FOLDER_ID", "")
YTDLP_VERBOSE = os.environ.get("YTDLP_VERBOSE", "").lower() in ("1", "true", "yes")

EDUSTREAM_API_BASE = os.environ.get(
    "EDUSTREAM_API_BASE",
    "https://api-test.edustream.indevs.in/api",
)

BOT_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024

# ---------- Client ----------
app = Client("edustream_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------- State ----------
AUTHORISED = set()
ACTIVE = defaultdict(int)
GLOBAL_ACTIVE = 0
STATS = {"ok": 0, "fail": 0, "gofile": 0}
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/user_settings.json")
SETTINGS = {}


def load_settings():
    global SETTINGS
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                SETTINGS = json.load(f)
        except Exception:
            SETTINGS = {}
    else:
        SETTINGS = {}


def save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(SETTINGS, f, indent=2)
    except Exception as e:
        print(f"save_settings error: {e}")


def get_setting(uid, key, default):
    u = str(uid)
    if u not in SETTINGS:
        SETTINGS[u] = {"delivery": "telegram"}
        save_settings()
    return SETTINGS[u].get(key, default)


def set_setting(uid, key, value):
    u = str(uid)
    SETTINGS.setdefault(u, {})
    SETTINGS[u][key] = value
    save_settings()


# ---------- Cookie / URL parsing ----------
COOKIE_KEYS = ("d", "cookie", "cookies")
URL_RE = re.compile(r"https?://\S+")
PROGRESS_TEMPLATE = """
**Percentage:** {percentage}
**Speed:** {speed}/s
**ETA:** {est_time}
"""


def parse_url_and_cookies(url):
    if "?" not in url:
        return url, None
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url, None
    kept = []
    cookies = None
    for p in parts.query.split("&"):
        if not p:
            continue
        key, sep, val = p.partition("=")
        if sep and key.lower() in COOKIE_KEYS and cookies is None:
            cookies = urllib.parse.unquote(val)
        else:
            kept.append(p)
    if cookies is None:
        return url, None
    clean = urllib.parse.urlunsplit(parts._replace(query="&".join(kept)))
    return clean, cookies


def extract_url_and_cookies_from_text(text):
    text = (text or "").strip()
    if not text:
        return None, None

    if text.startswith("{"):
        try:
            data = json.loads(text[text.index("{"):text.rindex("}") + 1])
            vurl = data.get("videoUrl") or data.get("url") or data.get("video_url")
            ck = data.get("cookie") or data.get("cookies")
            if vurl:
                return vurl.strip(), (str(ck).strip() if ck else None)
        except Exception as e:
            print(f"[yl] json parse error: {e}")

    lines = text.splitlines()
    if lines and lines[0].strip().startswith(("http://", "https://")):
        first = lines[0].strip()
        rest = " ".join(l.strip() for l in lines[1:] if l.strip())
        if rest and "=" in rest and "d=" not in first:
            return first, rest
        return first, None

    m = URL_RE.search(text)
    if m:
        return m.group(0), None
    return None, None


# ---------- EduStream play.php resolver ----------
async def resolve_edustream_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return None, None, None

    if "play.php" not in parts.path.lower():
        return None, None, None

    q = urllib.parse.parse_qs(parts.query)

    def _q(*keys):
        for k in keys:
            v = (q.get(k) or [""])[0]
            if v:
                return v.strip()
        return ""

    batch_id  = _q("batch_id", "batchId", "course_id", "courseId")
    subject_id = _q("subject_id", "subjectId")
    video_id  = _q("video_id", "videoId")
    name_hint = _q("video_name", "videoName", "name")
    direct_video = _q("video_url", "videoUrl")

    if direct_video and direct_video.startswith("http"):
        return direct_video, None, name_hint

    if not (batch_id and subject_id and video_id):
        print(f"[edustream] missing ids: batch={batch_id!r} subj={subject_id!r} vid={video_id!r}")
        return None, None, None

    api_url = (
        f"{EDUSTREAM_API_BASE}"
        f"?videoId={urllib.parse.quote(video_id)}"
        f"&courseId={urllib.parse.quote(batch_id)}"
        f"&subjectId={urllib.parse.quote(subject_id)}"
    )
    print(f"[edustream] resolving → {api_url}")

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.get(
                api_url,
                timeout=aiohttp.ClientTimeout(total=25, sock_read=20),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"},
            ) as r:
                if r.status != 200:
                    body = (await r.text())[:200]
                    print(f"[edustream] API HTTP {r.status}: {body}")
                    return None, None, None
                data = await r.json()
    except Exception as e:
        print(f"[edustream] resolve error: {e}")
        return None, None, None

    video_url = (data or {}).get("videoUrl")
    cookie = (data or {}).get("cookie")
    if not video_url or not cookie:
        print(f"[edustream] bad API response: {data}")
        return None, None, None

    print(f"[edustream] resolved: url_len={len(video_url)} cookie_len={len(cookie)} "
          f"name={name_hint[:50]!r}")
    return video_url, cookie, name_hint


# ---------- DM helpers ----------
async def user_has_dm(user_id):
    try:
        await app.get_chat(user_id)
        return True
    except Exception:
        return False


async def ask_user_to_start(bot_username, source_chat_id, user_mention):
    try:
        await app.send_message(
            source_chat_id,
            f"⚠️ {user_mention} — **you need to start the bot in DM first**\n\n"
            f"1️⃣ Tap the button below\n"
            f"2️⃣ Press **START**\n"
            f"3️⃣ Send `/yl <url>` again\n\n"
            f"_(Required so the bot can deliver your file to DM)_",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🚀 Start Bot in DM",
                        url=f"https://t.me/{bot_username}?start=start",
                    )
                ]]
            ),
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[dm-check] could not send hint: {e}")


# ---------- Helpers ----------
def fb(size):
    if size < 1024:
        return f"{size}B"
    if size < 1048576:
        return f"{size/1024:.1f}KB"
    if size < 1073741824:
        return f"{size/1048576:.1f}MB"
    return f"{size/1073741824:.2f}GB"


def fmt_time(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def sanitize_filename(name, max_len=180):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    name = name.strip().strip(".")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "video"


def is_mp4_file(path):
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return len(head) >= 12 and head[4:8] == b"ftyp"
    except Exception:
        return False


def user_mention_md(user):
    """Return Markdown text that pings the user even if they have no @username."""
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = getattr(user, "first_name", None) or "User"
    return f"[{name}](tg://user?id={user.id})"


async def show_progress(current, total, action, msg, start):
    if PYLEAVES_OK:
        try:
            await Leaves.progress_for_pyrogram(
                current, total, action, msg, start,
                PROGRESS_TEMPLATE, "▓", "░",
            )
            return
        except Exception:
            pass
    if total > 0:
        pct = current / total * 100
        try:
            await msg.edit_text(f"{action}\n{pct:.1f}% ({fb(current)}/{fb(total)})")
        except Exception:
            pass


async def probe_url(url, headers=None):
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, headers=headers or {}) as s:
            async with s.get(
                url, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20, sock_read=15),
            ) as r:
                ct = (r.headers.get("content-type") or "").lower()
                return str(r.url), ct
    except Exception as e:
        print(f"[probe] {e}")
        return url, ""


async def ffprobe_duration(path):
    try:
        r = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await r.communicate()
        return float((out or b"").decode().strip() or 0)
    except Exception:
        return 0.0


async def ensure_playable_mp4(path, pmsg=None):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return path
    fixed = path + ".fixed.mp4"
    try:
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", path, "-c", "copy",
            "-movflags", "+faststart", "-y", fixed,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await p.communicate()
    except Exception as e:
        print(f"[remux] exec error: {e}")
        return path

    if p.returncode != 0 or not os.path.exists(fixed) or os.path.getsize(fixed) == 0:
        print(f"[remux] failed rc={p.returncode}: {stderr.decode()[:150]}")
        try:
            if os.path.exists(fixed):
                os.unlink(fixed)
        except Exception:
            pass
        return path

    dur = await ffprobe_duration(fixed)
    if dur < 0.5:
        try:
            os.unlink(fixed)
        except Exception:
            pass
        return path

    print(f"[remux] OK → {fb(os.path.getsize(fixed))}, {dur:.1f}s")
    try:
        os.unlink(path)
    except Exception:
        pass
    return fixed


# ---------- Netscape cookiefile ----------
def _cookie_pairs(cookie_header):
    pairs = []
    for item in (cookie_header or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            pairs.append((name, value))
    return pairs


def write_netscape_cookiefile(cookie_header, url, path):
    if not cookie_header:
        return None
    pairs = _cookie_pairs(cookie_header)
    if not pairs:
        return None

    host = ""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").strip()
    except Exception:
        pass

    lines = ["# Netscape HTTP Cookie File\n"]
    seen = set()
    for dom in (host, ".cloudfront.net"):
        if not dom or dom in seen:
            continue
        seen.add(dom)
        df = dom if dom.startswith(".") else f".{dom}"
        for name, value in pairs:
            lines.append(f"{df}\tTRUE\t/\tFALSE\t2147483647\t{name}\t{value}\n")

    try:
        with open(path, "w", newline="\n") as f:
            f.writelines(lines)
        print(f"[cookiefile] wrote {len(pairs)} cookies × {len(seen)} domains "
              f"→ {os.path.basename(path)}")
        return path
    except Exception as e:
        print(f"[cookiefile] write error: {e}")
        return None


# ---------- yt-dlp logger ----------
class _YtdlpLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []
    def debug(self, msg):
        if YTDLP_VERBOSE:
            print(f"[yt-dlp] {msg}")
    def info(self, msg):
        if YTDLP_VERBOSE:
            print(f"[yt-dlp] {msg}")
    def warning(self, msg):
        self.warnings.append(msg)
        if YTDLP_VERBOSE:
            print(f"[yt-dlp WARN] {msg}")
    def error(self, msg):
        self.errors.append(msg)
        print(f"[yt-dlp ERR] {msg}")


def _clear_dir(path):
    try:
        for f in os.listdir(path):
            full = os.path.join(path, f)
            try:
                if os.path.isfile(full) or os.path.islink(full):
                    os.unlink(full)
                elif os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


# ---------- Attempt list ----------
def _build_attempts(is_hls=False, cookies=None, has_cookiefile=False):
    http_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookies:
        if not has_cookiefile:
            http_headers["Cookie"] = cookies
        http_headers["X-Cookie"] = cookies

    common = {
        "noplaylist": True,
        "quiet": not YTDLP_VERBOSE,
        "no_warnings": not YTDLP_VERBOSE,
        "verbose": YTDLP_VERBOSE,
        "socket_timeout": 60,
        "retries": 15,
        "nocheckcertificate": True,
        "force_ipv4": True,
        "http_headers": http_headers,
    }

    if is_hls:
        common.update({
            "format": "best",
            "hls_prefer_native": True,
            "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
            "skip_unavailable_fragments": False,
            "abort_on_unavailable_fragment": True,
            "fragment_retries": 50,
            "buffersize": 32 * 1024 * 1024,
        })
    else:
        common.update({
            "format": "bv*+ba/b",
            "hls_prefer_native": True,
            "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
            "skip_unavailable_fragments": True,
            "fragment_retries": 20,
        })

    attempts = []
    a = dict(common)
    a.update({
        "label": "mp4",
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    })
    attempts.append(a)

    if is_hls:
        b = dict(common)
        b.update({
            "label": "hls-ffmpeg",
            "hls_prefer_native": False,
            "hls_use_mpegts": True,
            "skip_unavailable_fragments": False,
            "abort_on_unavailable_fragment": True,
        })
        b.pop("concurrent_fragment_downloads", None)
        attempts.append(b)

    for target in ("chrome124", "chrome123", "chrome120", "chrome"):
        if not CURL_CFFI_OK:
            break
        a = dict(common)
        a.update({
            "label": f"{target}+mp4",
            "merge_output_format": "mp4",
            "impersonate": target,
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        })
        attempts.append(a)

    a = dict(common)
    a.update({"label": "mkv", "merge_output_format": "mkv"})
    attempts.append(a)

    a = dict(common)
    a["label"] = "best"
    a["format"] = "b"
    attempts.append(a)

    a = dict(common)
    a["label"] = "audio"
    a["format"] = "ba/b"
    attempts.append(a)

    return attempts


# ---------- yt-dlp downloader ----------
async def download_ytdlp(url, tmpdir, pmsg, start, filename_hint=None):
    if not YTDLP_OK:
        raise Exception("yt-dlp not installed")

    clean_url, cookies = parse_url_and_cookies(url)

    cookiefile = None
    if cookies:
        print(f"[cookies] {len(cookies)} chars → cookiefile + headers")
        cf_path = os.path.join(tmpdir, f"cookies_{uuid.uuid4().hex[:8]}.txt")
        cookiefile = write_netscape_cookiefile(cookies, clean_url, cf_path)

    headers = {}
    if cookies:
        headers["Cookie"] = cookies
        headers["X-Cookie"] = cookies
    headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    final_url, ctype = await probe_url(clean_url, headers=headers)
    is_hls = ".m3u8" in final_url.lower() or "mpegurl" in ctype.lower()
    print(f"[probe] final={final_url[:70]}… ct={ctype} hls={is_hls}")
    target = final_url if final_url != clean_url else clean_url

    loop = asyncio.get_running_loop()
    last = [0.0]
    ylog = _YtdlpLogger()

    def hook(d):
        if d.get("status") == "downloading":
            now = time.time()
            if now - last[0] >= 2:
                last[0] = now
                dl = d.get("downloaded_bytes", 0)
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if loop.is_closed():
                    return
                coro = show_progress(dl, tot, "📥 Downloading", pmsg, start)
                try:
                    asyncio.run_coroutine_threadsafe(coro, loop)
                except Exception:
                    try:
                        coro.close()
                    except Exception:
                        pass

    attempts = _build_attempts(
        is_hls=is_hls, cookies=cookies, has_cookiefile=bool(cookiefile),
    )
    errors = []
    info = None
    path = None
    skip_impersonate = False

    for i, attempt in enumerate(attempts, 1):
        label = attempt.get("label", f"attempt{i}")
        if "impersonate" in attempt and skip_impersonate:
            print(f"[yt-dlp] skipping {label} (impersonate disabled)")
            continue

        opts = {
            "outtmpl": os.path.join(tmpdir, f"dl_{uuid.uuid4().hex[:8]}.%(ext)s"),
            "logger": ylog,
            "progress_hooks": [hook],
            **{k: v for k, v in attempt.items() if k != "label"},
        }
        if cookiefile:
            opts["cookiefile"] = cookiefile

        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(target, download=True)

        try:
            print(f"[yt-dlp] attempt {i}/{len(attempts)}: {label}")
            info = await loop.run_in_executor(None, _run)
            print(f"[yt-dlp] attempt {i} finished downloading")
        except AssertionError:
            if "impersonate" in opts:
                skip_impersonate = True
                print(f"[yt-dlp] {label} AssertionError → disabling impersonate")
            errors.append(f"[{label}] AssertionError()")
            _clear_dir(tmpdir)
            continue
        except Exception as e:
            err = str(e).strip() or f"{e.__class__.__name__}()"
            if YTDLP_VERBOSE:
                print(f"[yt-dlp] {label} traceback:\n{traceback.format_exc()}")
            errors.append(f"[{label}] {err[:200]}")
            print(f"[yt-dlp] attempt {i} failed: {err[:200]}")
            _clear_dir(tmpdir)
            continue

        path = None
        requested = info.get("requested_downloads") or []
        if requested:
            candidate = requested[0].get("filepath")
            if candidate and os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                path = candidate
        if not path:
            files = [
                os.path.join(tmpdir, f) for f in os.listdir(tmpdir)
                if not f.endswith(".part")
                and os.path.isfile(os.path.join(tmpdir, f))
                and os.path.getsize(os.path.join(tmpdir, f)) > 0
            ]
            if files:
                path = max(files, key=os.path.getsize)

        if not path:
            errors.append(f"[{label}] no output file")
            _clear_dir(tmpdir)
            continue

        expected_dur = info.get("duration") or 0
        actual_dur = await ffprobe_duration(path)
        print(f"[verify] {label}: expected={expected_dur:.1f}s "
              f"actual={actual_dur:.1f}s size={fb(os.path.getsize(path))}")

        if expected_dur > 10 and actual_dur > 0 and actual_dur < expected_dur * 0.5:
            errors.append(f"[{label}] incomplete: {actual_dur:.0f}s of {expected_dur:.0f}s")
            print(f"[verify] {label} incomplete → discarding")
            _clear_dir(tmpdir)
            path = None
            info = None
            continue

        break

    if not path or not info:
        raise Exception("all yt-dlp attempts failed:\n" + "\n".join(errors[:5]))

    path = await ensure_playable_mp4(path, pmsg)
    title = filename_hint or info.get("title") or "video"
    ext = os.path.splitext(path)[1] or ".mp4"
    return path, sanitize_filename(f"{title}{ext}")


# ---------- Custom HLS downloader ----------
async def download_hls_manual(url, tmpdir, pmsg, start, headers, filename_hint=None):
    import urllib.parse as up

    session_headers = dict(headers or {})
    session_headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36",
    )

    connector = aiohttp.TCPConnector(ssl=False, limit=CONCURRENT_FRAGMENTS * 2)
    async with aiohttp.ClientSession(
        headers=session_headers, connector=connector,
    ) as session:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=60, sock_read=30)
        ) as r:
            if r.status != 200:
                body = (await r.text())[:200]
                raise Exception(f"master HTTP {r.status}: {body}")
            master_text = await r.text()
            master_url = str(r.url)
        print(f"[hls] master: {len(master_text)} bytes at {master_url[:70]}…")

        variant_urls = []
        lines = master_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                for j in range(i + 1, len(lines)):
                    c = lines[j].strip()
                    if c and not c.startswith("#"):
                        variant_urls.append(up.urljoin(master_url, c))
                        break

        if variant_urls:
            media_url = variant_urls[0]
            print(f"[hls] {len(variant_urls)} variants → {media_url[:80]}…")
            async with session.get(
                media_url, timeout=aiohttp.ClientTimeout(total=60, sock_read=30)
            ) as r:
                if r.status != 200:
                    body = (await r.text())[:200]
                    raise Exception(f"media HTTP {r.status}: {body}")
                media_text = await r.text()
                media_base = str(r.url)
        else:
            media_text = master_text
            media_base = master_url

        key_re = re.compile(r'#EXT-X-KEY:[^\n]*?URI="([^"]+)"')
        key_uris = list({m for m in key_re.findall(media_text)})
        print(f"[hls] found {len(key_uris)} key URI(s)")

        key_local_map = {}
        for i, kuri in enumerate(key_uris):
            key_abs = up.urljoin(media_base, kuri)
            print(f"[hls] fetching key {i}: {key_abs[:90]}…")
            async with session.get(
                key_abs, timeout=aiohttp.ClientTimeout(total=30, sock_read=15)
            ) as r:
                if r.status != 200:
                    body = (await r.text())[:200]
                    raise Exception(f"key {i} HTTP {r.status}: {body}")
                key_data = await r.read()
            if len(key_data) != 16:
                print(f"[hls] WARNING: key {i} is {len(key_data)} bytes "
                      f"(AES-128 expects 16) — first bytes: {key_data[:20]!r}")
            lk = os.path.join(tmpdir, f"key_{i}.bin")
            with open(lk, "wb") as f:
                f.write(key_data)
            key_local_map[kuri] = lk
            print(f"[hls] key {i}: {len(key_data)} bytes → {os.path.basename(lk)}")

        segments = []
        current_dur = 0.0
        for line in media_text.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF:"):
                try:
                    current_dur = float(line.split(":", 1)[1].split(",")[0])
                except Exception:
                    current_dur = 0.0
            elif line and not line.startswith("#"):
                segments.append((current_dur, up.urljoin(media_base, line)))
                current_dur = 0.0

        print(f"[hls] {len(segments)} segments")

        sem = asyncio.Semaphore(CONCURRENT_FRAGMENTS)
        done = [0]
        total_bytes = [0]
        failed = []

        async def fetch_segment(idx, seg_url):
            async with sem:
                local = os.path.join(tmpdir, f"seg_{idx:05d}.ts")
                for attempt in range(1, 4):
                    try:
                        async with session.get(
                            seg_url,
                            timeout=aiohttp.ClientTimeout(total=90, sock_read=60),
                        ) as r:
                            if r.status != 200:
                                raise Exception(f"HTTP {r.status}")
                            size = 0
                            with open(local, "wb") as f:
                                async for chunk in r.content.iter_chunked(64 * 1024):
                                    f.write(chunk)
                                    size += len(chunk)
                        done[0] += 1
                        total_bytes[0] += size
                        if done[0] % 20 == 0 or done[0] == len(segments):
                            pct = done[0] / len(segments) * 100
                            try:
                                await show_progress(
                                    done[0], len(segments),
                                    f"📥 Segments {pct:.0f}% ({fb(total_bytes[0])})",
                                    pmsg, start,
                                )
                            except Exception:
                                pass
                        return local
                    except Exception as e:
                        if attempt == 3:
                            print(f"[hls] segment {idx} failed after 3 tries: {e}")
                            failed.append((idx, str(e)))
                            return None
                        await asyncio.sleep(attempt)

        tasks = [fetch_segment(i, u) for i, (_, u) in enumerate(segments)]
        results = await asyncio.gather(*tasks)

        ok_count = sum(1 for r in results if r)
        print(f"[hls] segments downloaded: {ok_count}/{len(segments)} "
              f"({fb(total_bytes[0])})")

        if ok_count == 0:
            raise Exception("all segments failed to download")

        first_key_local = next(iter(key_local_map.values()), None)
        playlist_lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10",
                          "#EXT-X-MEDIA-SEQUENCE:0"]
        if first_key_local:
            playlist_lines.append(
                f'#EXT-X-KEY:METHOD=AES-128,URI="{first_key_local}"'
            )

        for i, (dur, _url) in enumerate(segments):
            if not results[i]:
                continue
            playlist_lines.append(f"#EXTINF:{dur if dur > 0 else 10.0:.6f},")
            playlist_lines.append(os.path.join(tmpdir, f"seg_{i:05d}.ts"))
        playlist_lines.append("#EXT-X-ENDLIST")

        local_playlist = os.path.join(tmpdir, "local_playlist.m3u8")
        with open(local_playlist, "w") as f:
            f.write("\n".join(playlist_lines))
        print(f"[hls] wrote local playlist ({ok_count} segments)")

        output = os.path.join(tmpdir, f"hls_{uuid.uuid4().hex[:8]}.mp4")
        cmd = [
            "ffmpeg",
            "-protocol_whitelist", "file,crypto,data",
            "-allowed_extensions", "ALL",
            "-i", local_playlist,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            "-y", output,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        err_lines = []

        async def _read_stderr():
            while True:
                ln = await proc.stderr.readline()
                if not ln:
                    break
                err_lines.append(ln.decode(errors="ignore").strip())

        asyncio.create_task(_read_stderr())

        while True:
            ln = await proc.stdout.readline()
            if not ln:
                break

        await proc.wait()
        if proc.returncode != 0:
            tail = " ".join(err_lines[-8:])[:400]
            raise Exception(f"FFmpeg (custom HLS) rc={proc.returncode}: {tail}")
        if not os.path.exists(output) or os.path.getsize(output) == 0:
            raise Exception("FFmpeg produced no output")

        dur = await ffprobe_duration(output)
        print(f"[hls] done: {fb(os.path.getsize(output))}, {dur:.1f}s")
        base = filename_hint or f"video_{uuid.uuid4().hex[:8]}"
        return output, sanitize_filename(f"{base}.mp4")


# ---------- Plain FFmpeg fallback ----------
async def download_ffmpeg(url, tmpdir, pmsg, start, filename_hint=None):
    clean_url, cookies = parse_url_and_cookies(url)
    if cookies:
        print(f"[ffmpeg] {len(cookies)} chars → Cookie header")

    output = os.path.join(tmpdir, f"ffmpeg_{uuid.uuid4().hex[:8]}.mp4")
    cmd = [
        "ffmpeg",
        "-protocol_whitelist", "file,crypto,data,http,https,tcp,tls",
        "-allowed_extensions", "ALL",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-user_agent", (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    ]
    if cookies:
        cmd += ["-headers", f"Cookie: {cookies}\r\nX-Cookie: {cookies}\r\n"]
    cmd += [
        "-i", clean_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        "-y", output,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    dur = [0.0]
    err_lines = []

    async def _read_stderr():
        while True:
            ln = await proc.stderr.readline()
            if not ln:
                break
            s = ln.decode(errors="ignore")
            err_lines.append(s.strip())
            if "Duration:" in s and dur[0] == 0:
                m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", s)
                if m:
                    dur[0] = (
                        int(m.group(1)) * 3600 + int(m.group(2)) * 60
                        + float(m.group(3))
                    )

    asyncio.create_task(_read_stderr())

    last = [0.0]
    while True:
        ln = await proc.stdout.readline()
        if not ln:
            break
        s = ln.decode(errors="ignore").strip()
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k == "out_time_us" and dur[0] > 0:
            t = int(v) / 1e6
            now = time.time()
            if now - last[0] >= 2:
                last[0] = now
                await show_progress(t, dur[0], "📥 Downloading (ffmpeg)", pmsg, start)

    await proc.wait()
    if proc.returncode != 0:
        tail = " ".join(err_lines[-5:])[:300]
        raise Exception(f"FFmpeg rc={proc.returncode}: {tail}")
    if not os.path.exists(output) or os.path.getsize(output) == 0:
        raise Exception("FFmpeg produced no output")

    actual_dur = await ffprobe_duration(output)
    print(f"[ffmpeg] finished: {fb(os.path.getsize(output))}, {actual_dur:.1f}s")
    base = filename_hint or f"video_{uuid.uuid4().hex[:8]}"
    return output, sanitize_filename(f"{base}.mp4")


# ---------- Telegram DM upload ----------
async def upload_to_telegram_dm(path, filename, user_id, pmsg, start):
    size = os.path.getsize(path)
    if size > BOT_UPLOAD_LIMIT:
        raise Exception(f"file {fb(size)} > Telegram bot limit {fb(BOT_UPLOAD_LIMIT)}")

    caption = f"🎓 **EduStream**\n📦 `{fb(size)}`"
    if is_mp4_file(path):
        await app.send_video(
            chat_id=user_id, video=path, file_name=filename,
            caption=caption, supports_streaming=True,
            progress=show_progress,
            progress_args=("📤 Uploading to Telegram", pmsg, start),
        )
    else:
        await app.send_document(
            chat_id=user_id, document=path, file_name=filename,
            caption=caption,
            progress=show_progress,
            progress_args=("📤 Uploading to Telegram", pmsg, start),
        )


# ---------- Gofile upload ----------
async def _gofile_upload_once(path, filename):
    headers = {}
    if GOFILE_TOKEN:
        headers["Authorization"] = f"Bearer {GOFILE_TOKEN}"

    fh = open(path, "rb")
    form = aiohttp.FormData()
    form.add_field("file", fh, filename=filename)
    if GOFILE_FOLDER:
        form.add_field("folderId", GOFILE_FOLDER)

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.post(
                "https://upload.gofile.io/uploadfile",
                data=form, headers=headers,
                timeout=aiohttp.ClientTimeout(total=None, sock_read=900),
            ) as r:
                if r.status != 200:
                    body = (await r.text())[:200]
                    raise Exception(f"Gofile HTTP {r.status}: {body}")
                payload = await r.json()
    finally:
        try:
            fh.close()
        except Exception:
            pass

    if payload.get("status") != "ok":
        raise Exception(f"Gofile: {payload.get('status')}")
    data = payload.get("data", {})
    link = data.get("downloadPage") or data.get("directLink")
    if not link:
        raise Exception("Gofile: no downloadPage in response")
    return link


async def upload_gofile(path, filename, pmsg, max_retries=3):
    size = os.path.getsize(path)
    try:
        await pmsg.edit_text(f"☁️ Uploading **{filename}** ({fb(size)}) to Gofile…")
    except Exception:
        pass

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return await _gofile_upload_once(path, filename)
        except Exception as e:
            last_err = e
            if attempt == max_retries:
                break
            wait = 2 ** attempt
            print(f"[gofile] attempt {attempt} failed: {e} — retry in {wait}s")
            await asyncio.sleep(wait)
    raise last_err or Exception("Gofile upload failed")


# ---------- Settings UI ----------
def build_settings_keyboard(user_id):
    delivery = get_setting(user_id, "delivery", "telegram")
    tg_mark = " ✅" if delivery == "telegram" else ""
    gf_mark = " ✅" if delivery == "gofile" else ""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"📩 Telegram DM{tg_mark}",
                callback_data="delivery:telegram",
            ),
            InlineKeyboardButton(
                f"☁️ Gofile Only{gf_mark}",
                callback_data="delivery:gofile",
            ),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="settings:refresh"),
            InlineKeyboardButton("❌ Close", callback_data="settings:close"),
        ],
    ])


def build_settings_text(user_id):
    delivery = get_setting(user_id, "delivery", "telegram")
    if delivery == "telegram":
        current = (
            "📩 **Telegram DM**\n"
            "   └ Files ≤ 2 GB → sent to your DM\n"
            "   └ Files > 2 GB or DM blocked → Gofile link"
        )
    else:
        current = (
            "☁️ **Gofile Only**\n"
            "   └ All files → uploaded to Gofile\n"
            "   └ You get a download link in your DM"
        )
    return (
        "⚙️ **Your Settings**\n\n"
        f"📥 **Delivery Method:**\n{current}\n\n"
        "Choose your preferred delivery below 👇"
    )


@app.on_callback_query(filters.regex(r"^(delivery|settings):"))
async def on_settings_callback(c, cb: CallbackQuery):
    user_id = cb.from_user.id
    action, value = cb.data.split(":", 1)

    if action == "delivery":
        if value not in ("telegram", "gofile"):
            await cb.answer("Invalid option")
            return
        set_setting(user_id, "delivery", value)
        label = "Telegram DM" if value == "telegram" else "Gofile Only"
        await cb.answer(f"✅ Set to {label}")
        try:
            await cb.message.edit_text(
                build_settings_text(user_id),
                reply_markup=build_settings_keyboard(user_id),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    elif action == "settings":
        if value == "close":
            try:
                await cb.message.delete()
            except Exception:
                pass
            await cb.answer("Closed")
        elif value == "refresh":
            await cb.answer("Refreshed 🔄")
            try:
                await cb.message.edit_text(
                    build_settings_text(user_id),
                    reply_markup=build_settings_keyboard(user_id),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass


# ---------- Completion builder ----------
def build_group_completion(
    user_mention, filename, size, duration, elapsed, avg_speed,
    delivered, gofile_link, tg_error, dm_link_sent,
):
    lines = [
        f"🎉 {user_mention} **your download is ready!**",
        "",
        f"📁 **File:** `{filename}`",
        f"📦 **Size:** `{fb(size)}`",
        f"⏱️ **Duration:** `{fmt_time(duration)}`",
        f"⚡ **Total time:** `{fmt_time(elapsed)}`",
        f"🚀 **Avg speed:** `{fb(int(avg_speed))}/s`",
        "",
    ]

    if delivered == "telegram":
        lines.append("📩 **File delivered to your DM** ✅")
    else:
        if dm_link_sent:
            lines.append("☁️ **Gofile link sent to your DM** ✅")
        else:
            if tg_error:
                lines.append(
                    f"⚠️ **Telegram DM failed** ({str(tg_error)[:60]})"
                )
            lines.append(
                f"❌ **Couldn't DM you** — tap the button below to start the bot\n"
                f"(Gofile link kept private for security)"
            )

    return "\n".join(lines)


def build_private_completion(
    filename, size, duration, elapsed, avg_speed, delivered, gofile_link,
):
    lines = [
        "✅ **Download Complete**",
        "",
        f"📁 `{filename}`",
        f"📦 `{fb(size)}` · ⏱️ `{fmt_time(duration)}`",
        f"⚡ `{fmt_time(elapsed)}` · 🚀 `{fb(int(avg_speed))}/s`",
        "",
    ]
    if delivered == "telegram":
        lines.append("📩 **File sent above** ☝️")
    else:
        lines.append(f"☁️ **Gofile:** [Download Link]({gofile_link})")
    return "\n".join(lines)


# ---------- Main pipeline ----------
async def process(url, source_chat_id, user_id, user_mention,
                  from_group=False, filename_hint=None, user_obj=None):
    global STATS
    pmsg = await app.send_message(source_chat_id, "📥 Starting download…")
    start = time.time()
    tmpdir = tempfile.mkdtemp(prefix="edustream_")
    path = None

    try:
        clean_url, cookies = parse_url_and_cookies(url)
        headers = {}
        if cookies:
            headers["Cookie"] = cookies
            headers["X-Cookie"] = cookies
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )

        try:
            path, filename = await download_ytdlp(
                url, tmpdir, pmsg, start, filename_hint=filename_hint,
            )
        except Exception as yerr:
            print(f"[process] yt-dlp failed: {str(yerr)[:300]}")
            print(f"[process] trying custom HLS downloader…")
            try:
                path, filename = await download_hls_manual(
                    clean_url, tmpdir, pmsg, start,
                    headers=headers, filename_hint=filename_hint,
                )
            except Exception as herr:
                print(f"[process] custom HLS failed: {str(herr)[:300]}")
                print(f"[process] falling back to plain ffmpeg…")
                try:
                    path, filename = await download_ffmpeg(
                        url, tmpdir, pmsg, start, filename_hint=filename_hint,
                    )
                except Exception as ferr:
                    raise Exception(
                        f"Download failed.\n"
                        f"yt-dlp: {str(yerr)[:200]}\n"
                        f"custom-hls: {str(herr)[:200]}\n"
                        f"ffmpeg: {str(ferr)[:200]}"
                    )

        size = os.path.getsize(path)
        duration = await ffprobe_duration(path)
        elapsed = time.time() - start
        avg_speed = size / elapsed if elapsed > 0 else 0
        print(f"[process] ready: {filename} ({fb(size)}, {duration:.1f}s, "
              f"{fmt_time(elapsed)}, {fb(int(avg_speed))}/s)")

        # ---- Delivery ----
        delivery_pref = get_setting(user_id, "delivery", "telegram")
        delivered = None
        gofile_link = None
        tg_error = None
        dm_link_sent = False

        if delivery_pref == "gofile":
            gofile_link = await upload_gofile(path, filename, pmsg)
            delivered = "gofile"
            STATS["gofile"] += 1
            print(f"[delivery] Gofile (user pref) OK")

            # DM the link (only if source chat is not the same as user DM)
            if source_chat_id != user_id:
                try:
                    await app.send_message(
                        user_id,
                        f"📁 **{filename}**\n"
                        f"📦 `{fb(size)}` · ⏱️ `{fmt_time(duration)}`\n\n"
                        f"☁️ **Gofile Link:**\n{gofile_link}",
                        disable_web_page_preview=False,
                    )
                    dm_link_sent = True
                    print(f"[delivery] Gofile link DM'd to {user_id}")
                except Exception as e:
                    print(f"[delivery] could not DM gofile link: {e}")
        else:
            # Telegram DM first
            try:
                await upload_to_telegram_dm(path, filename, user_id, pmsg, start)
                delivered = "telegram"
                print(f"[delivery] Telegram DM to {user_id} OK")
            except Exception as tg_err:
                tg_error = tg_err
                print(f"[delivery] Telegram DM failed: {tg_err} → Gofile")
                try:
                    gofile_link = await upload_gofile(path, filename, pmsg)
                    delivered = "gofile"
                    STATS["gofile"] += 1

                    if source_chat_id != user_id:
                        try:
                            await app.send_message(
                                user_id,
                                f"📁 **{filename}**\n"
                                f"📦 `{fb(size)}` · ⏱️ `{fmt_time(duration)}`\n\n"
                                f"☁️ **Gofile Link:**\n{gofile_link}\n\n"
                                f"⚠️ _Telegram DM failed: {str(tg_err)[:80]}_",
                                disable_web_page_preview=False,
                            )
                            dm_link_sent = True
                        except Exception as e2:
                            print(f"[delivery] could not DM gofile link: {e2}")
                except Exception as gf_err:
                    raise Exception(
                        f"Delivery failed.\nTelegram: {str(tg_err)[:200]}\n"
                        f"Gofile: {str(gf_err)[:200]}"
                    )

        STATS["ok"] += 1

        # ---- Completion messages ----
        if from_group:
            # Group: notification + stats, NO link
            group_final = build_group_completion(
                user_mention, filename, size, duration, elapsed, avg_speed,
                delivered, gofile_link, tg_error, dm_link_sent,
            )
            # Add a "Start Bot" button if we couldn't DM the link
            reply_markup = None
            if delivered == "gofile" and not dm_link_sent:
                try:
                    me = await app.get_me()
                    bot_username = me.username or "EduStreamBot"
                    reply_markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🚀 Start Bot in DM",
                            url=f"https://t.me/{bot_username}?start=start",
                        )
                    ]])
                except Exception:
                    pass
            try:
                await pmsg.edit_text(
                    group_final,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except Exception:
                await app.send_message(
                    source_chat_id, group_final,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
        else:
            # Private chat: same chat as DM
            private_final = build_private_completion(
                filename, size, duration, elapsed, avg_speed,
                delivered, gofile_link,
            )
            try:
                await pmsg.edit_text(
                    private_final,
                    disable_web_page_preview=False,
                )
            except Exception:
                await app.send_message(
                    source_chat_id, private_final,
                    disable_web_page_preview=False,
                )

    except Exception as e:
        STATS["fail"] += 1
        err = str(e)[:600] or repr(e) or e.__class__.__name__
        print(f"[process] Error: {err}")
        traceback.print_exc()

        fail_text = (
            f"❌ **Download Failed**\n\n"
            f"👤 {user_mention}\n\n"
            f"```\n{err[:400]}\n```"
        )
        try:
            await pmsg.edit_text(fail_text)
        except Exception:
            pass

    finally:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass
        _clear_dir(tmpdir)
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass


# ---------- Commands ----------
@app.on_message(filters.command("yl"))
async def yl_cmd(c, m: Message):
    global GLOBAL_ACTIVE

    if m.chat.type in ("group", "supergroup") and m.chat.id not in AUTHORISED:
        await m.reply("❌ Not authorised. Ask admin to run /authorise.")
        return

    text = (m.text or m.caption or "").strip()
    cmd_re = re.compile(r"^/yl(?:@\w+)?\s*", re.IGNORECASE)
    text = cmd_re.sub("", text, count=1).strip()

    url, cookies = extract_url_and_cookies_from_text(text)
    if not url:
        await m.reply(
            "**Usage**\n"
            "• `/yl <url>`\n"
            "• `/yl <url>?d=<CloudFront-Cookie>=<..>; ...`\n"
            "• `/yl {json: videoUrl + cookie}`\n"
            "• `/yl <url>\\n<Cookie: ...>`\n"
            "• `/yl <play.php?...>` – EduStream (auto-resolved)"
        )
        return

    user = m.from_user
    if not user:
        await m.reply("❌ Cannot identify sender.")
        return

    user_mention = user_mention_md(user)
    from_group = m.chat.type in ("group", "supergroup")

    # DM check for groups (both delivery modes need DM)
    if from_group:
        if not await user_has_dm(user.id):
            me = await app.get_me()
            bot_username = me.username or "EduStreamBot"
            await ask_user_to_start(bot_username, m.chat.id, user_mention)
            return

    # Concurrency
    if ACTIVE[user.id] >= MAX_PER_USER:
        await m.reply(
            f"❌ {user_mention} — you already have **{MAX_PER_USER}** active tasks.\n"
            f"Wait for one to finish."
        )
        return
    if GLOBAL_ACTIVE >= MAX_GLOBAL:
        await m.reply(
            f"❌ Bot busy ({GLOBAL_ACTIVE}/{MAX_GLOBAL}). Try again shortly."
        )
        return

    # Resolve EduStream
    filename_hint = None
    if "play.php" in url.lower():
        resolved_url, resolved_cookies, name_hint = await resolve_edustream_url(url)
        if resolved_url:
            url = resolved_url
            if resolved_cookies:
                cookies = resolved_cookies
            filename_hint = name_hint
            print(f"[yl] resolved play.php → {url[:70]}…")
        else:
            await m.reply("⚠️ Couldn't resolve the EduStream link.")
            return

    if cookies and "d=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}d={urllib.parse.quote(cookies, safe='')}"

    ACTIVE[user.id] += 1
    GLOBAL_ACTIVE += 1

    try:
        try:
            await m.delete()
        except Exception:
            pass

        target = user.id if not from_group else m.chat.id
        await process(
            url,
            source_chat_id=target,
            user_id=user.id,
            user_mention=user_mention,
            from_group=from_group,
            filename_hint=filename_hint,
            user_obj=user,
        )
    finally:
        ACTIVE[user.id] = max(0, ACTIVE[user.id] - 1)
        GLOBAL_ACTIVE = max(0, GLOBAL_ACTIVE - 1)


@app.on_message(filters.command("settings"))
async def _settings(c, m: Message):
    user_id = m.from_user.id
    await m.reply(
        build_settings_text(user_id),
        reply_markup=build_settings_keyboard(user_id),
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("ping"))
async def _ping(c, m):
    await m.reply("🏓 Pong!")


@app.on_message(filters.command("authorise"))
async def _authorise(c, m):
    if m.chat.type not in ("group", "supergroup"):
        await m.reply("Groups only.")
        return
    member = await app.get_chat_member(m.chat.id, m.from_user.id)
    if member.status not in ("administrator", "creator"):
        await m.reply("Admins only.")
        return
    AUTHORISED.add(m.chat.id)
    await m.reply("✅ Authorised.")


@app.on_message(filters.command("stats"))
async def _stats(c, m):
    await m.reply(
        f"**Stats**\n"
        f"✅ Completed:  {STATS['ok']}\n"
        f"❌ Failed:     {STATS['fail']}\n"
        f"☁️ Gofile:     {STATS['gofile']}\n"
        f"🚀 Running:    {GLOBAL_ACTIVE}/{MAX_GLOBAL}\n"
        f"👥 Active users: {sum(1 for v in ACTIVE.values() if v > 0)}"
    )


@app.on_message(filters.command(["start", "help"]))
async def _help(c, m):
    await m.reply(
        "🎓 **EduStream Bot**\n"
        "YouTube-style downloader\n\n"
        f"**Concurrency:** {MAX_PER_USER} tasks/user, {MAX_GLOBAL} global\n"
        f"**HLS speed:** {CONCURRENT_FRAGMENTS} parallel fragments\n"
        f"**AES-128 HLS:** ✅ cookie-safe segment fetching\n\n"
        "**Commands**\n"
        "`/yl <url>` – download and deliver\n"
        "`/settings` – choose Telegram DM or Gofile delivery\n"
        "`/stats` – bot statistics\n"
        "`/authorise` – enable bot in group (admins)\n"
        "`/ping` – liveness check\n\n"
        "**Supported URLs**\n"
        "• Plain URLs\n"
        "• `<url>?d=<cookies>` (CloudFront)\n"
        "• `{\"videoUrl\": \"...\", \"cookie\": \"...\"}` (JSON)\n"
        "• `<url>\\n<Cookie: a=b; c=d>` (multiline)\n"
        "• `https://edustream.freedev.app/play.php?...` (auto-resolved)\n\n"
        "**Delivery**\n"
        "Files ≤ 2 GB → your DM (or Gofile if you chose Gofile)\n"
        "Files > 2 GB → Gofile link\n"
        "Gofile links are **always** sent to DM — never exposed in groups."
    )


# ---------- Main ----------
if __name__ == "__main__":
    load_settings()
    print(f"pyleaves        : {'✅' if PYLEAVES_OK else '❌'}")
    print(f"yt-dlp          : {'✅ v' + YTDLP_VERSION if YTDLP_OK else '❌ not installed'}")
    print(f"curl_cffi       : {'✅ v' + CURL_CFFI_VERSION if CURL_CFFI_OK else '⚠️ missing'}")
    print(f"gofile          : {'✅ token set' if GOFILE_TOKEN else '⚠️ guest mode'}")
    if GOFILE_FOLDER:
        print(f"gofile          : folder → {GOFILE_FOLDER}")
    print(f"edustream api   : {EDUSTREAM_API_BASE}")
    print(f"tg limit        : {fb(BOT_UPLOAD_LIMIT)}")
    print(f"fragments       : {CONCURRENT_FRAGMENTS} parallel HLS fragments")
    print(f"concurrency     : {MAX_PER_USER} per user / {MAX_GLOBAL} global")
    print(f"aes-128 hls     : ✅ segments + keys fetched via aiohttp (cookie-safe)")
    print(f"play.php        : ✅ auto-resolve via EduStream API")
    print(f"delivery        : ✅ per-user (telegram | gofile), group gets stats only")
    print(f"dm-check        : ✅ ask to start bot in DM when required")
    print(f"ytdlp-verb      : {'on' if YTDLP_VERBOSE else 'off'}")
    print("Starting bot…")
    app.run()
