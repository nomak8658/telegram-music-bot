import os
import json
import logging
import asyncio
import tempfile
import re
import time
import pathlib
from datetime import datetime

import requests
import websockets
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN")
OWNER_ID = 864463823          # المالك الوحيد اللي يقدر يضيف/يحذف مجموعات

GROUPS_FILE = "allowed_groups.json"

def _load_groups() -> set[int]:
    groups: set[int] = set()
    # من متغير البيئة ALLOWED_GROUPS (مفصولة بفاصلة)
    env_val = os.environ.get("ALLOWED_GROUPS", "")
    for part in env_val.split(","):
        part = part.strip()
        if part:
            try: groups.add(int(part))
            except ValueError: pass
    # من ملف JSON (للقروبات المضافة يدوياً بـ /allow)
    try:
        with open(GROUPS_FILE) as f:
            for gid in json.load(f):
                groups.add(int(gid))
    except Exception:
        pass
    return groups

def _save_groups(groups: set[int]) -> None:
    with open(GROUPS_FILE, "w") as f:
        json.dump(list(groups), f)

ALLOWED_GROUPS: set[int] = _load_groups()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://mp3j.cc",
    "Referer": "https://mp3j.cc/ar/",
}

# { user_id: [results] } — محدود بـ 300 مستخدم لمنع memory leak
_MAX_SEARCH_CACHE = 300
user_search_results: dict[int, list] = {}

def _store_search(user_id: int, results: list) -> None:
    if len(user_search_results) >= _MAX_SEARCH_CACHE:
        # احذف أقدم مستخدم
        oldest = next(iter(user_search_results))
        del user_search_results[oldest]
    user_search_results[user_id] = results

# Bot username — filled on startup
BOT_USERNAME: str = ""

# ─── Audio Cache (query → Telegram file_id) — Volume persistent ───────────────
# /data مُثبّت كـ Railway Volume → يبقى بين إعادة التشغيل
_CACHE_FILE = pathlib.Path("/data/audio_cache.json")
_audio_cache: dict[str, str] = {}

def _load_audio_cache() -> None:
    global _audio_cache
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _CACHE_FILE.exists():
            _audio_cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            logger.info(f"Cache loaded: {len(_audio_cache)} tracks")
    except Exception:
        _audio_cache = {}

def _save_audio_cache() -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(_audio_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _ck(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().lower())

def cache_get(query: str) -> str | None:
    return _audio_cache.get(_ck(query))

def cache_set(query: str, file_id: str) -> None:
    _audio_cache[_ck(query)] = file_id
    _save_audio_cache()

_load_audio_cache()

# كلمات النايتكور/المسرع للفلترة

YOUTUBE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)[\w\-]{11}"
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fmt_duration(ms: int) -> str:
    """ميلي‑ثانية → MM:SS"""
    if not ms:
        return ""
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_sec(sec: int) -> str:
    """ثوانٍ → MM:SS"""
    if not sec:
        return ""
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_name(title: str) -> str:
    return re.sub(r'[^\w\s\-]', '', title)[:50].strip()


def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_REGEX.search(text))


def split_artist_title(full_title: str) -> tuple[str, str]:
    """يفصل 'فنان - أغنية' إلى (فنان, أغنية). إذا ما في '-' يرجع ('', full_title)."""
    if " - " in full_title:
        parts = full_title.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return "", full_title


def build_caption(bot_username: str, duration_str: str) -> str:
    """يبني الـ caption بالديزاين المطلوب: • @botname ♪ MM:SS"""
    dur = f" ♪ {duration_str}" if duration_str else ""
    return f"• @{bot_username}{dur}"


# ─── Search helpers ───────────────────────────────────────────────────────────

YT_SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.9",
}


# ─── nogomistars.com ──────────────────────────────────────────────────────────

def _query_to_nogomi_subdomain(query: str) -> str | None:
    """يحوّل النص إلى subdomain لـ nogomistars.com (punycode للعربي)."""
    try:
        slug = query.strip().replace(" ", "-")
        try:
            slug.encode("ascii")
            return slug
        except UnicodeEncodeError:
            pass
        punycode_part = slug.encode("punycode").decode("ascii")
        return "xn--" + punycode_part
    except Exception as e:
        logger.error(f"nogomi subdomain error: {e}")
        return None


def nogomistars_search(query: str) -> list:
    """يبحث في nogomistars.com ويرجع قائمة نتائج مع روابط تحميل مباشرة."""
    try:
        subdomain = _query_to_nogomi_subdomain(query)
        if not subdomain:
            return []
        url = f"https://{subdomain}.nogomistars.com/"
        r = requests.get(url, headers=YT_SEARCH_HEADERS, timeout=15, allow_redirects=True)
        # تحقق من عدم الانتقال للصفحة الرئيسية
        final = r.url.replace("https://", "").replace("http://", "").split("/")[0]
        if subdomain not in final:
            logger.info(f"nogomistars: no results for '{query}' (redirected to {final})")
            return []
        # استخرج بيانات كل بطاقة منفصلة
        dl_urls = re.findall(r'href="(https://[^"]+/d/[^"]+)"', r.text)
        yt_ids  = re.findall(r'ytimg\.com/vi/([\w\-]{11})/', r.text)
        # استخرج كل نصوص alt (فقط من ytimg thumbnails)
        raw_titles = re.findall(
            r'ytimg\.com/vi/[\w\-]{11}/[^"]*"[^>]*alt="([^"]*)"', r.text
        )
        results = []
        count = min(len(dl_urls), len(yt_ids), 8)
        for i in range(count):
            raw = raw_titles[i] if i < len(raw_titles) else ""
            title = re.sub(r"\s*نجومي\s*$", "", raw.strip()).strip()
            title = re.sub(r"\s+", " ", title)
            if not title:
                title = f"أغنية {i + 1}"
            results.append({
                "title": title,
                "dl_url": dl_urls[i],
                "yt_id": yt_ids[i],
                "source": "nogomi",
            })
        logger.info(f"nogomistars found {len(results)} for: {query}")
        return results
    except Exception as e:
        logger.error(f"nogomistars_search error: {e}")
        return []


def nogomistars_download(dl_url: str, title: str) -> tuple[str | None, str, int]:
    """تحمّل MP3 مباشرة من رابط nogomistars."""
    try:
        r = requests.get(
            dl_url,
            headers={**YT_SEARCH_HEADERS, "Referer": "https://nogomistars.com/"},
            timeout=30,
            stream=True,
        )
        content_type = r.headers.get("content-type", "")
        if r.status_code != 200 or "text/html" in content_type:
            logger.warning(f"nogomistars_download: {r.status_code} ct={content_type}")
            return None, title, 0
        fd, path = tempfile.mkstemp(suffix=".mp3")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            size = os.path.getsize(path)
            if size < 1024:  # أقل من 1KB = ملف فاشل
                os.unlink(path)
                logger.warning(f"nogomistars_download: file too small ({size}B)")
                return None, title, 0
            logger.info(f"nogomistars downloaded {size//1024}KB: {title}")
            return path, title, 0
        except Exception as e:
            if os.path.exists(path):
                os.unlink(path)
            raise e
    except Exception as e:
        logger.error(f"nogomistars_download error: {e}")
        return None, title, 0


def sm3ha_search_first(query: str) -> str | None:
    """يبحث في sm3ha.io ويرجع رابط يوتيوب لأول نتيجة، أو None."""
    try:
        slug = query.replace(" ", "-")
        url  = f"https://v1.sm3ha.io/s/{slug}"
        r    = requests.get(url, headers=YT_SEARCH_HEADERS, timeout=15)
        vids = re.findall(r'href="#([A-Za-z0-9_-]{11})"', r.text)
        if vids:
            vid = vids[0]
            logger.info(f"sm3ha search found: {vid} for query: {query}")
            return f"https://www.youtube.com/watch?v={vid}"
    except Exception as e:
        logger.error(f"sm3ha_search_first error: {e}")
    return None


def sm3ha_search_all(query: str) -> list:
    """يبحث في sm3ha.io ويرجع قائمة نتائج مع العناوين الحقيقية."""
    try:
        slug = query.replace(" ", "-")
        url  = f"https://v1.sm3ha.io/s/{slug}"
        r    = requests.get(url, headers=YT_SEARCH_HEADERS, timeout=15)
        html = r.text

        vids = re.findall(r'href="#([A-Za-z0-9_-]{11})"', html)
        if not vids:
            return []

        # العناوين في alt="..." على صور الـ thumbnail (ytimg)
        # كل صورة لها data-src="https://i.ytimg.com/vi/{ID}/..." + alt="{العنوان}"
        titles: list[str] = re.findall(
            r'data-src="https://i\.ytimg\.com/vi/[\w\-]{11}/[^"]*"[^>]{0,300}?alt="([^"]*)"',
            html,
            re.DOTALL,
        )
        # احتياط: alt قد يكون قبل data-src في بعض الحالات
        if not titles:
            titles = re.findall(
                r'alt="([^"]*)"[^>]{0,300}?data-src="https://i\.ytimg\.com',
                html,
                re.DOTALL,
            )
        # تنظيف القيم الفارغة أو اسم الموقع
        titles = [t.strip() for t in titles if t.strip() and t.strip() != "سمعها"]

        results = []
        for i, vid in enumerate(vids[:8]):
            title = titles[i] if i < len(titles) else query
            results.append({"title": title, "yt_id": vid, "source": "sm3ha"})

        logger.info(f"sm3ha_search_all found {len(results)} (titles={len(titles)}) for: {query}")
        return results
    except Exception as e:
        logger.error(f"sm3ha_search_all error: {e}")
        return []


def youtube_search_first(query: str) -> str | None:
    """يبحث في يوتيوب ويرجع رابط أول فيديو، أو None."""
    try:
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}"
        r = requests.get(url, headers=YT_SEARCH_HEADERS, timeout=15)
        ids = re.findall(r'"videoId":"([\w\-]{11})"', r.text)
        if ids:
            vid = ids[0]
            logger.info(f"YouTube fallback found: {vid} for query: {query}")
            return f"https://www.youtube.com/watch?v={vid}"
    except Exception as e:
        logger.error(f"youtube_search_first error: {e}")
    return None


# ─── mp3j.cc API ──────────────────────────────────────────────────────────────

def mp3j_search(query: str) -> list:
    """بحث في mp3j.cc ويرجع نتائج SoundCloud + YouTube."""
    try:
        resp = requests.post(
            "https://cdn.mp3j.cc/search",
            data={"q": query},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

    sc = data.get("SoundCloud", []) or []
    yt = data.get("YoutubeSearch", []) or []
    logger.info(f"mp3j search '{query}': SC={len(sc)} YT={len(yt)}")

    results = []

    # ── SoundCloud (تحميل سريع عبر WebSocket) ──────────────────────
    for track in sc[:6]:
        track_id = track.get("id")
        if not track_id:
            continue
        title = track.get("title", "بدون عنوان")
        duration_ms = track.get("duration", 0)
        duration = fmt_duration(duration_ms)
        artwork = (track.get("artwork_url") or "").replace("-large.", "-t300x300.")
        results.append({
            "id": str(track_id),
            "title": title,
            "duration": duration,
            "duration_ms": duration_ms,
            "artwork": artwork,
            "query": query,
            "source": "mp3j",
        })

    # ── YoutubeSearch (أسماء حقيقية، تحميل عبر savemp3) ────────────
    for track in yt[:8]:
        yt_id = track.get("id")
        if not yt_id:
            continue
        title = track.get("title") or "بدون عنوان"
        results.append({
            "id": str(yt_id),
            "title": title,
            "duration": "",
            "yt_id": str(yt_id),
            "query": query,
            "source": "mp3j_yt",
        })

    return results


async def mp3j_prepare(track_id: str, query: str) -> bool:
    """يتصل بـ WebSocket على cdn.mp3j.cc لتحضير ملف MP3 — max 45 ثانية كلياً."""
    uri = (
        f"wss://cdn.mp3j.cc/WS/SoundCloud/track"
        f"?query={requests.utils.quote(query)}&id={track_id}"
    )
    ws_headers = {
        "Origin": "https://mp3j.cc",
        "User-Agent": HEADERS["User-Agent"],
    }
    async def _inner() -> bool:
        async with websockets.connect(uri, additional_headers=ws_headers, open_timeout=20) as ws:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                logger.info(f"WS [{track_id}]: {t}")
                if t == "finished":
                    return True
                if t == "error":
                    logger.error(f"WS error: {msg}")
                    return False
        return True
    try:
        return await asyncio.wait_for(_inner(), timeout=45)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        return False


def mp3j_download(track_id: str, query: str, title: str) -> str | None:
    """يحمّل ملف MP3 من cdn.mp3j.cc بعد التحضير."""
    url = (
        f"https://cdn.mp3j.cc/SoundCloud/track"
        f"?id={track_id}"
        f"&q={requests.utils.quote(query)}"
        f"&title={requests.utils.quote(title)}"
    )
    try:
        resp = requests.get(url, headers=HEADERS, stream=True, timeout=60, allow_redirects=True)
        resp.raise_for_status()

        ctype = resp.headers.get("Content-Type", "")
        if "audio" not in ctype and "octet-stream" not in ctype:
            logger.error(f"Unexpected content type: {ctype}")
            return None

        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".mp3", prefix=safe_name(title) + "_"
        )
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                tmp.write(chunk)
        tmp.close()
        return tmp.name
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None


# ─── ar.savemp3.net API ───────────────────────────────────────────────────────

SAVEMP3_BASE = "https://ar.savemp3.net"
SAVEMP3_PATH = "/gci3a/youtube-video-to-mp3/"
SAVEMP3_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "text/plain;charset=UTF-8",
    "Origin": SAVEMP3_BASE,
    "Referer": SAVEMP3_BASE + SAVEMP3_PATH,
}
SA_VIDEO_INFO  = "404eac7b40c33407fd80d08a14825576322d98251a"
SA_GET_DOWNLOAD= "4076aebc4223e008c04f0eb5c02e5414965330290d"
SA_GET_STATUS  = "4019ed796f6fb69bc5c1885e73d4f437455c7dde97"


_SAVEMP3_ROUTER_STATE = (
    "%5B%22%22%2C%20%7B%22children%22%3A%20%5B%5B%22site%22%2C%20%22gci3a%22%2C%20%22d%22%5D%2C"
    "%20%7B%22children%22%3A%20%5B%5B%22slug%22%2C%20%22youtube-video-to-mp3%22%2C%20%22d%22%5D%2C"
    "%20%7B%22children%22%3A%20%5B%22__PAGE__%22%2C%20%7B%7D%5D%7D%5D%7D%5D%7D%2C%20null%2C%20null%2C%20true%5D"
)


def _savemp3_action(session: requests.Session, action_hash: str, payload: list) -> str:
    r = session.post(
        SAVEMP3_BASE + SAVEMP3_PATH,
        data=json.dumps(payload),
        headers={
            **SAVEMP3_HEADERS,
            "Next-Action": action_hash,
            "Next-Router-State-Tree": _SAVEMP3_ROUTER_STATE,
            "Accept": "text/x-component,*/*",
        },
        timeout=20,
    )
    r.encoding = "utf-8"
    return r.text


def _parse_savemp3_success(text: str) -> dict | None:
    for m in re.finditer(r'\d+:(\{.+?\})\n', text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            if obj.get("success") and "data" in obj:
                return obj["data"]
        except Exception:
            pass
    return None


def savemp3_full_download(yt_url: str) -> tuple[str | None, str, int]:
    """
    يشغّل كل مراحل savemp3.net.
    يرجع (مسار الملف, العنوان, المدة بالثواني) أو (None, '', 0).
    """
    # session واحد طوال العملية — زيارة صفحة واحدة لأخذ الكوكيز
    session = requests.Session()
    session.headers.update({"User-Agent": SAVEMP3_HEADERS["User-Agent"]})
    try:
        session.get(SAVEMP3_BASE + SAVEMP3_PATH, timeout=15)
    except Exception as e:
        logger.error(f"savemp3 page visit error: {e}")
        return None, "", 0

    try:
        raw = _savemp3_action(session, SA_VIDEO_INFO, [yt_url])
        info = _parse_savemp3_success(raw)
    except Exception as e:
        logger.error(f"savemp3_get_info error: {e}")
        return None, "", 0

    if not info or "tasks" not in info:
        logger.error(f"savemp3 getVideoInfo failed")
        return None, "", 0

    title    = info.get("title", "YouTube Audio")
    tasks    = info.get("tasks", [])
    duration = info.get("durationSec", 0)

    task = (
        next((t for t in tasks if t.get("bitrate") == 128), None)
        or next((t for t in tasks if t.get("bitrate") == 192), None)
        or (tasks[0] if tasks else None)
    )
    if not task:
        return None, title, duration

    # Start download
    try:
        payload = [{"task": task, "length": duration, "from": 0, "to": duration}]
        raw2 = _savemp3_action(session, SA_GET_DOWNLOAD, payload)
        d2 = _parse_savemp3_success(raw2)
    except Exception as e:
        logger.error(f"savemp3_start_download error: {e}")
        return None, title, duration

    if not d2 or "taskId" not in d2:
        return None, title, duration

    task_id = d2["taskId"]

    # Poll status — max 45 ثانية
    deadline = time.time() + 45
    download_url = None
    while time.time() < deadline:
        try:
            raw3 = _savemp3_action(session, SA_GET_STATUS, [task_id])
            d3 = _parse_savemp3_success(raw3)
            if d3:
                status = d3.get("status", "")
                logger.info(f"savemp3 status={status}")
                if status == "finished":
                    download_url = d3.get("download")
                    break
                if status == "failed":
                    return None, title, duration
        except Exception as e:
            logger.error(f"savemp3_poll error: {e}")
        time.sleep(2)

    if not download_url:
        return None, title, duration

    # Download file — max 40 ثانية
    try:
        resp = requests.get(
            download_url,
            headers={"User-Agent": SAVEMP3_HEADERS["User-Agent"]},
            stream=True, timeout=40, allow_redirects=True,
        )
        resp.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".mp3", prefix=safe_name(title) + "_yt_"
        )
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                tmp.write(chunk)
        tmp.close()
        return tmp.name, title, duration
    except Exception as e:
        logger.error(f"savemp3 file download error: {e}")
        return None, title, duration


# ─── yt-dlp: تحميل YouTube مباشرة ────────────────────────────────────────────

def ytdlp_download(yt_url: str) -> tuple[str | None, str, int]:
    """
    يحمّل صوت YouTube عبر yt-dlp (الأسرع والأكثر موثوقية).
    يرجع (مسار الملف, العنوان, المدة بالثواني) أو (None, '', 0).
    المتصل مسؤول عن حذف الملف بعد الاستخدام.
    """
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        logger.error("yt-dlp غير مثبّت")
        return None, "", 0

    tmp_dir = tempfile.mkdtemp()
    outtmpl  = os.path.join(tmp_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        },
    }
    title    = ""
    duration = 0
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(yt_url, download=True)
        if info:
            title    = info.get("title", "YouTube Audio")
            duration = int(info.get("duration") or 0)
        files = [f for f in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, f))]
        if not files:
            logger.error("ytdlp: no file after download")
            return None, title or "YouTube Audio", duration
        file_path = os.path.join(tmp_dir, files[0])
        size = os.path.getsize(file_path)
        if size < 1024:
            logger.error(f"ytdlp: file too small {size}B")
            return None, title or "YouTube Audio", duration
        logger.info(f"ytdlp downloaded {size // 1024}KB: {title}")
        return file_path, title or "YouTube Audio", duration
    except Exception as e:
        logger.error(f"ytdlp_download error: {e}")
        # تنظيف الملفات المؤقتة عند الفشل
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        return None, title or "", duration


# ─── Fallback: بحث + تحميل من المصادر الاحتياطية ─────────────────────────────

async def _fallback_search_download(
    query: str,
    wait_msg,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    fallback لما يفشل التحميل الأساسي.
    يجرب بالترتيب: sm3ha+savemp3 → youtube+savemp3 → nogomistars → خطأ
    """
    loop = asyncio.get_event_loop()

    # ── 1: sm3ha → savemp3 ──────────────────────────────────────────
    await wait_msg.edit_text("🔄 جاري البحث في مصدر آخر...")
    yt_url = await loop.run_in_executor(None, sm3ha_search_first, query)
    if yt_url:
        ok = await _download_and_send_yt(yt_url, wait_msg, chat_id, context, cache_key=query)
        if ok:
            return

    # ── 2: YouTube مباشر → savemp3 ──────────────────────────────────
    yt_url2 = await loop.run_in_executor(None, youtube_search_first, query)
    if yt_url2 and yt_url2 != yt_url:
        ok = await _download_and_send_yt(yt_url2, wait_msg, chat_id, context, cache_key=query)
        if ok:
            return

    # ── 3: nogomistars ───────────────────────────────────────────────
    await wait_msg.edit_text("🔄 جاري البحث في نوقومي ستارز...")
    nogomi_res = await loop.run_in_executor(None, nogomistars_search, query)
    if nogomi_res:
        track = nogomi_res[0]
        await wait_msg.edit_text(f"📥 جاري التحميل...\n🎵 *{track['title']}*", parse_mode="Markdown")
        file_path, dl_title, _ = await loop.run_in_executor(
            None, nogomistars_download, track["dl_url"], track["title"])
        if file_path and os.path.exists(file_path):
            try:
                artist, song_title = split_artist_title(dl_title)
                ok2, file_id = await send_audio_file(context.bot, chat_id, file_path,
                    title=song_title or dl_title, duration_str="", performer=artist)
                if ok2:
                    if file_id: cache_set(query, file_id)
                    await wait_msg.delete()
                else:
                    await wait_msg.edit_text("❌ حدث خطأ أثناء الإرسال.")
                return
            finally:
                if os.path.exists(file_path):
                    try: os.unlink(file_path)
                    except Exception: pass
        yt_id3 = track.get("yt_id")
        if yt_id3:
            ok = await _download_and_send_yt(f"https://www.youtube.com/watch?v={yt_id3}",
                wait_msg, chat_id, context, cache_key=query)
            if ok:
                return

    await wait_msg.edit_text("❌ ما قدرت أحمّل الأغنية، جرب لاحقاً.")


# ─── YouTube fallback: download & send ────────────────────────────────────────

async def _download_and_send_yt(
    yt_url: str,
    wait_msg,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    cache_key: str = "",
) -> bool:
    """
    يحمّل YouTube ويرسله.
    الترتيب: yt-dlp → savemp3 → nogomistars
    يرجع True إذا نجح.
    """
    loop = asyncio.get_event_loop()
    file_path, title, duration_sec = "", "", 0

    # ── 1: yt-dlp ────────────────────────────────────────────────────
    file_path, title, duration_sec = await loop.run_in_executor(None, ytdlp_download, yt_url)

    # ── 2: savemp3 ───────────────────────────────────────────────────
    if not file_path or not os.path.exists(file_path):
        logger.info(f"ytdlp failed → savemp3: {yt_url}")
        file_path, title, duration_sec = await loop.run_in_executor(
            None, savemp3_full_download, yt_url
        )

    # ── 3: nogomistars (بحث بالعنوان) ───────────────────────────────
    if (not file_path or not os.path.exists(file_path)) and cache_key:
        logger.info(f"savemp3 failed → nogomistars: {cache_key}")
        nogomi = await loop.run_in_executor(None, nogomistars_search, cache_key)
        if nogomi:
            fp2, t2, d2 = await loop.run_in_executor(
                None, nogomistars_download, nogomi[0]["dl_url"], nogomi[0]["title"]
            )
            if fp2 and os.path.exists(fp2):
                file_path, title, duration_sec = fp2, t2, d2

    if not file_path or not os.path.exists(file_path):
        return False
    try:
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > 50:
            await wait_msg.edit_text(f"❌ الملف كبير ({size_mb:.1f}MB)، تلغرام لا يقبل أكثر من 50MB.")
            return True  # حصلنا على ملف، بس كبير — لا تحاول غيره
        artist, song_title = split_artist_title(title)
        duration_str = fmt_sec(duration_sec)
        ok, file_id = await send_audio_file(context.bot, chat_id, file_path,
            title=song_title or title, duration_str=duration_str, performer=artist)
        if ok:
            if cache_key and file_id:
                cache_set(cache_key, file_id)
            await wait_msg.delete()
        else:
            await wait_msg.edit_text("❌ حدث خطأ أثناء الإرسال.")
        return ok
    finally:
        if file_path and os.path.exists(file_path):
            try: os.unlink(file_path)
            except Exception: pass


async def _download_and_send_yt_cb(
    yt_url: str,
    cb,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    cache_key: str = "",
) -> None:
    """
    يحمّل YouTube ويرسله عبر callback_query.
    الترتيب: yt-dlp → savemp3 → nogomistars
    """
    loop = asyncio.get_event_loop()
    await cb.edit_message_text("⏳ جاري التحميل...")

    # ── 1: yt-dlp ────────────────────────────────────────────────────
    file_path, title, duration_sec = await loop.run_in_executor(None, ytdlp_download, yt_url)

    # ── 2: savemp3 ───────────────────────────────────────────────────
    if not file_path or not os.path.exists(file_path):
        logger.info(f"ytdlp failed → savemp3: {yt_url}")
        await cb.edit_message_text("⏳ جاري التحويل (مصدر احتياطي)...")
        file_path, title, duration_sec = await loop.run_in_executor(
            None, savemp3_full_download, yt_url
        )

    # ── 3: nogomistars (بحث بالعنوان) ───────────────────────────────
    if (not file_path or not os.path.exists(file_path)) and cache_key:
        logger.info(f"savemp3 failed → nogomistars: {cache_key}")
        await cb.edit_message_text("⏳ جاري البحث في مصدر ثالث...")
        nogomi = await loop.run_in_executor(None, nogomistars_search, cache_key)
        if nogomi:
            fp2, t2, d2 = await loop.run_in_executor(
                None, nogomistars_download, nogomi[0]["dl_url"], nogomi[0]["title"]
            )
            if fp2 and os.path.exists(fp2):
                file_path, title, duration_sec = fp2, t2, d2

    if not file_path or not os.path.exists(file_path):
        await cb.edit_message_text("❌ ما قدرت أحمّل الأغنية، جرب لاحقاً.")
        return

    try:
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > 50:
            await cb.edit_message_text(
                f"❌ الملف كبير ({size_mb:.1f}MB)، تلغرام لا يقبل أكثر من 50MB."
            )
            return
        artist, song_title = split_artist_title(title)
        ok, file_id = await send_audio_file(
            context.bot, chat_id, file_path,
            title=song_title or title,
            duration_str=fmt_sec(duration_sec),
            performer=artist,
        )
        if ok:
            if cache_key and file_id:
                cache_set(cache_key, file_id)
            await cb.delete_message()
        else:
            await cb.edit_message_text("❌ حدث خطأ أثناء الإرسال.")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except Exception:
                pass


# ─── Shared send helper ────────────────────────────────────────────────────────

async def send_audio_file(
    bot,
    chat_id: int,
    file_path: str,
    title: str,
    duration_str: str,
    performer: str = "",
) -> tuple[bool, str]:
    """يرسل ملف الصوت ويرجع (نجح, file_id) للكاش."""
    caption = build_caption(BOT_USERNAME, duration_str)
    try:
        with open(file_path, "rb") as f:
            sent_msg = await bot.send_audio(
                chat_id=chat_id,
                audio=f,
                title=title,
                performer=performer or None,
                caption=caption,
            )
        return True, sent_msg.audio.file_id
    except Exception as e:
        logger.error(f"send_audio error: {e}")
        return False, ""


async def send_cached(bot, chat_id: int, file_id: str, title: str, duration_str: str, performer: str = "") -> bool:
    """يرسل الأغنية من الكاش (file_id) بثانية."""
    caption = build_caption(BOT_USERNAME, duration_str)
    try:
        await bot.send_audio(chat_id=chat_id, audio=file_id,
            title=title, performer=performer or None, caption=caption)
        return True
    except Exception as e:
        logger.warning(f"cache send failed (stale file_id?): {e}")
        return False


# ─── يوت: تحميل فوري (SoundCloud أول نتيجة) ──────────────────────────────────

async def yot_instant_search(msg, query: str, context: ContextTypes.DEFAULT_TYPE):
    """يوت + اسم أغنية: يجيب أول نتيجة ويحمّلها فوراً.
    الترتيب: mp3j.cc → sm3ha.io → nogomistars.com
    """
    # ── كاش: إذا سبق وشُغّلت هذي الأغنية تجي بثانية ───────────────
    cached_fid = cache_get(query)
    if cached_fid:
        ok = await send_cached(context.bot, msg.chat_id, cached_fid, query, "")
        if ok:
            return

    wait_msg = await msg.reply_text(
        f"🎵 جاري البحث والتحميل: *{query}*...", parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    # ── 1: mp3j.cc ──────────────────────────────────────────────────
    results = await loop.run_in_executor(None, mp3j_search, query)
    if results:
        track    = results[0]
        track_id = track["id"]
        title    = track["title"]
        duration = track["duration"]
        query_q  = track["query"]
        artist, song_title = split_artist_title(title)
        await wait_msg.edit_text(f"⏳ جاري التحضير...\n🎵 *{title}*", parse_mode="Markdown")
        ok = await mp3j_prepare(track_id, query_q)
        if ok:
            await wait_msg.edit_text(f"📥 جاري التحميل...\n🎵 *{title}*", parse_mode="Markdown")
            file_path = await loop.run_in_executor(None, mp3j_download, track_id, query_q, title)
            if file_path and os.path.exists(file_path):
                try:
                    size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    if size_mb > 50:
                        await wait_msg.edit_text(f"❌ الملف كبير ({size_mb:.1f}MB)، تلغرام لا يقبل أكثر من 50MB.")
                        return
                    ok2, file_id = await send_audio_file(context.bot, msg.chat_id, file_path,
                        title=song_title or title, duration_str=duration, performer=artist)
                    if ok2:
                        if file_id: cache_set(query, file_id)
                        await wait_msg.delete()
                    else:
                        await wait_msg.edit_text("❌ حدث خطأ أثناء الإرسال.")
                    return
                finally:
                    if file_path and os.path.exists(file_path):
                        try: os.unlink(file_path)
                        except Exception: pass

    # ── 2: sm3ha.io → savemp3.net ────────────────────────────────────
    sm3ha_results = await loop.run_in_executor(None, sm3ha_search_all, query)
    for r in sm3ha_results[:2]:
        yt_id = r["yt_id"]
        await wait_msg.edit_text("⏳ جاري التحويل والتحميل...")
        ok = await _download_and_send_yt(
            f"https://www.youtube.com/watch?v={yt_id}",
            wait_msg, msg.chat_id, context, cache_key=query)
        if ok:
            return

    # ── 3: nogomistars.com ──────────────────────────────────────────
    nogomi_results = await loop.run_in_executor(None, nogomistars_search, query)
    if nogomi_results:
        track = nogomi_results[0]
        await wait_msg.edit_text(f"📥 جاري التحميل...\n🎵 *{track['title']}*", parse_mode="Markdown")
        file_path, dl_title, _ = await loop.run_in_executor(
            None, nogomistars_download, track["dl_url"], track["title"])
        if file_path and os.path.exists(file_path):
            try:
                artist, song_title = split_artist_title(dl_title)
                ok2, file_id = await send_audio_file(context.bot, msg.chat_id, file_path,
                    title=song_title or dl_title, duration_str="", performer=artist)
                if ok2:
                    if file_id: cache_set(query, file_id)
                    await wait_msg.delete()
                else:
                    await wait_msg.edit_text("❌ حدث خطأ أثناء الإرسال.")
                return
            finally:
                if os.path.exists(file_path):
                    try: os.unlink(file_path)
                    except Exception: pass
        yt_id2 = track.get("yt_id")
        if yt_id2:
            await _download_and_send_yt(f"https://www.youtube.com/watch?v={yt_id2}",
                wait_msg, msg.chat_id, context, cache_key=query)
            return

    await wait_msg.edit_text("❌ ما لقيت الأغنية، جرب كلمة ثانية.")


# ─── يوت: تحميل يوتيوب مباشرة ─────────────────────────────────────────────────

async def yot_youtube(msg, query: str, context: ContextTypes.DEFAULT_TYPE):
    """يوت + رابط يوتيوب: يحمّل من savemp3.net فوراً."""
    yt_match = YOUTUBE_REGEX.search(query)
    yt_url   = yt_match.group(0)
    if not yt_url.startswith("http"):
        yt_url = "https://" + yt_url

    wait_msg = await msg.reply_text("⏳ جاري التحميل...")
    await _download_and_send_yt(yt_url, wait_msg, msg.chat_id, context)


# ─── شغل: يوتيوب بحث → ar.savemp3.net تحميل ─────────────────────────────────

async def cmd_shaghl(msg, query: str, context: ContextTypes.DEFAULT_TYPE):
    """
    شغل [اسم الأغنية]:
      الترتيب: mp3j.cc → sm3ha.io → savemp3.net → nogomistars.com
    """
    cached_fid = cache_get(query)
    if cached_fid:
        ok = await send_cached(context.bot, msg.chat_id, cached_fid, query, "")
        if ok:
            return

    wait_msg = await msg.reply_text(f"🔍 جاري البحث عن: *{query}*...", parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    # ── 1: mp3j.cc ──────────────────────────────────────────────────
    mp3j_results = await loop.run_in_executor(None, mp3j_search, query)
    if mp3j_results:
        track    = mp3j_results[0]
        track_id = track["id"]
        title_mp = track["title"]
        query_q  = track["query"]
        artist, song_title = split_artist_title(title_mp)
        await wait_msg.edit_text(f"⏳ جاري التحضير...\n🎵 *{title_mp}*", parse_mode="Markdown")
        ok = await mp3j_prepare(track_id, query_q)
        if ok:
            await wait_msg.edit_text(f"📥 جاري التحميل...\n🎵 *{title_mp}*", parse_mode="Markdown")
            file_path = await loop.run_in_executor(None, mp3j_download, track_id, query_q, title_mp)
            if file_path and os.path.exists(file_path):
                try:
                    ok2, file_id = await send_audio_file(context.bot, msg.chat_id, file_path,
                        title=song_title or title_mp, duration_str=track["duration"], performer=artist)
                    if ok2:
                        if file_id: cache_set(query, file_id)
                        await wait_msg.delete()
                    else:
                        await wait_msg.edit_text("❌ حدث خطأ أثناء الإرسال.")
                    return
                finally:
                    if os.path.exists(file_path):
                        try: os.unlink(file_path)
                        except Exception: pass

    # ── 2: sm3ha.io → savemp3.net ────────────────────────────────────
    sm3ha_res = await loop.run_in_executor(None, sm3ha_search_all, query)
    yt_id = sm3ha_res[0]["yt_id"] if sm3ha_res else None
    if yt_id:
        await wait_msg.edit_text("⏳ جاري التحويل والتحميل...")
        ok = await _download_and_send_yt(f"https://www.youtube.com/watch?v={yt_id}",
            wait_msg, msg.chat_id, context, cache_key=query)
        if ok:
            return

    # ── 3: nogomistars.com ───────────────────────────────────────────
    nogomi_results = await loop.run_in_executor(None, nogomistars_search, query)
    if nogomi_results:
        track = nogomi_results[0]
        await wait_msg.edit_text(f"📥 جاري التحميل...\n🎵 *{track['title']}*", parse_mode="Markdown")
        file_path, dl_title, _ = await loop.run_in_executor(
            None, nogomistars_download, track["dl_url"], track["title"])
        if file_path and os.path.exists(file_path):
            try:
                artist, song_title = split_artist_title(dl_title)
                ok2, file_id = await send_audio_file(context.bot, msg.chat_id, file_path,
                    title=song_title or dl_title, duration_str="", performer=artist)
                if ok2:
                    if file_id: cache_set(query, file_id)
                    await wait_msg.delete()
                else:
                    await wait_msg.edit_text("❌ حدث خطأ أثناء الإرسال.")
                return
            finally:
                if os.path.exists(file_path):
                    try: os.unlink(file_path)
                    except Exception: pass
        yt_id2 = track.get("yt_id")
        if yt_id2:
            ok = await _download_and_send_yt(f"https://www.youtube.com/watch?v={yt_id2}",
                wait_msg, msg.chat_id, context, cache_key=query)
            if ok:
                return

    await wait_msg.edit_text("❌ ما لقيت الأغنية، جرب كلمة ثانية.")


# ─── Telegram Handlers ────────────────────────────────────────────────────────

async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يضيف مجموعة للقائمة البيضاء — للمالك فقط."""
    global ALLOWED_GROUPS
    msg = update.message
    if not msg or update.effective_user.id != OWNER_ID:
        return

    # إذا استخدم الأمر داخل مجموعة → أضف هذه المجموعة
    if msg.chat.type in ("group", "supergroup"):
        chat_id = msg.chat_id
        ALLOWED_GROUPS.add(chat_id)
        _save_groups(ALLOWED_GROUPS)
        await msg.reply_text(f"✅ تم السماح لهذه المجموعة.\nID: `{chat_id}`", parse_mode="Markdown")
        return

    # من الخاص: /allow -1001234567890
    if context.args:
        try:
            chat_id = int(context.args[0])
            ALLOWED_GROUPS.add(chat_id)
            _save_groups(ALLOWED_GROUPS)
            await msg.reply_text(f"✅ تم السماح للمجموعة `{chat_id}`.", parse_mode="Markdown")
        except ValueError:
            await msg.reply_text("❌ ID غير صحيح، مثال: `/allow -1001234567890`", parse_mode="Markdown")
    else:
        # اعرض القائمة الحالية
        if ALLOWED_GROUPS:
            ids = "\n".join(f"`{g}`" for g in sorted(ALLOWED_GROUPS))
            await msg.reply_text(f"المجموعات المسموحة:\n{ids}", parse_mode="Markdown")
        else:
            await msg.reply_text("لا توجد مجموعات مسموحة بعد.\nاستخدم `/allow` داخل المجموعة أو `/allow [ID]`.", parse_mode="Markdown")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يحذف مجموعة من القائمة البيضاء — للمالك فقط."""
    global ALLOWED_GROUPS
    msg = update.message
    if not msg or update.effective_user.id != OWNER_ID:
        return

    if msg.chat.type in ("group", "supergroup"):
        chat_id = msg.chat_id
        ALLOWED_GROUPS.discard(chat_id)
        _save_groups(ALLOWED_GROUPS)
        await msg.reply_text(f"🚫 تم إيقاف البوت في هذه المجموعة.\nID: `{chat_id}`", parse_mode="Markdown")
        return

    if context.args:
        try:
            chat_id = int(context.args[0])
            ALLOWED_GROUPS.discard(chat_id)
            _save_groups(ALLOWED_GROUPS)
            await msg.reply_text(f"🚫 تم حذف المجموعة `{chat_id}`.", parse_mode="Markdown")
        except ValueError:
            await msg.reply_text("❌ ID غير صحيح.", parse_mode="Markdown")
    else:
        await msg.reply_text("اكتب ID المجموعة: `/deny -1001234567890`", parse_mode="Markdown")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *بوت أغاني*\n\n"
        "الأوامر:\n"
        "• `بحث [اسم الأغنية]` — قائمة نتائج للاختيار\n"
        "• `يوت [اسم الأغنية]` — تحميل فوري\n\n"
        "أمثلة:\n"
        "`بحث طلال مداح`\n"
        "`يوت محمد عبده`",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    text     = msg.text.strip()
    is_group = msg.chat.type in ("group", "supergroup")

    # ── فلتر: فقط المجموعات المسموحة — الخاص مغلق كلياً ───────────
    if not is_group:
        return
    if msg.chat_id not in ALLOWED_GROUPS:
        return

    # ── استخرج نوع الأمر والاستعلام ────────────────────────────────
    is_yot = False
    query  = None

    is_shaghl = False

    if text.startswith("بحث "):
        query = text[4:].strip()
    elif text.startswith("يوت "):
        query  = text[4:].strip()
        is_yot = True
    elif text.startswith("شغل "):
        query     = text[4:].strip()
        is_shaghl = True
    elif text in ("بحث", "يوت", "شغل"):
        await msg.reply_text(
            "اكتب اسم الأغنية بعد الأمر\n"
            "مثال: `بحث طلال مداح` أو `يوت محمد عبده` أو `شغل طلال مداح`",
            parse_mode="Markdown",
        )
        return
    else:
        if is_group:
            return
        query = text

    if not query:
        return

    # ── شغل: يوتيوب → ar.savemp3.net ──────────────────────────────
    if is_shaghl:
        await cmd_shaghl(msg, query, context)
        return

    # ── يوت: فوري ──────────────────────────────────────────────────
    if is_yot:
        if is_youtube_url(query):
            await yot_youtube(msg, query, context)
        else:
            await yot_instant_search(msg, query, context)
        return

    # في الخاص: إذا أرسل رابط يوتيوب بدون أمر → تحميل مباشر
    if not is_group and is_youtube_url(query):
        await yot_youtube(msg, query, context)
        return

    # ── بحث: قائمة أزرار ────────────────────────────────────────────
    wait_msg = await msg.reply_text(
        f"🔍 جاري البحث عن: *{query}*...",
        parse_mode="Markdown",
    )

    loop = asyncio.get_event_loop()
    user_id = update.effective_user.id

    def _make_keyboard(results_list):
        keyboard = []
        for i, r in enumerate(results_list):
            dur   = f" [{r['duration']}]" if r.get("duration") else ""
            label = f"🎵 {r['title'][:48]}{dur}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"dl_{i}")])
        return keyboard

    # ── ابحث في mp3j (SoundCloud+YouTube) و nogomistars بالتوازي ───
    mp3j_res, nogomi_res = await asyncio.gather(
        loop.run_in_executor(None, mp3j_search, query),
        loop.run_in_executor(None, nogomistars_search, query),
    )

    # mp3j يرجع نوعين: SoundCloud (تحميل سريع) و YouTube (أسماء حقيقية)
    sc_res = [r for r in mp3j_res if r.get("source") == "mp3j"]
    yt_res = [r for r in mp3j_res if r.get("source") == "mp3j_yt"]

    # ── رتّب: SoundCloud أول (أسرع) → nogomistars → mp3j YouTube ───
    combined = sc_res + nogomi_res + yt_res

    # أزل التكرار حسب yt_id / id
    seen: set[str] = set()
    deduped = []
    for r in combined:
        key = r.get("yt_id") or r.get("id") or r.get("title")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(r)
    combined = deduped[:12]          # أقصى 12 نتيجة

    if not combined:
        await wait_msg.edit_text("❌ ما لقيت الأغنية، جرب كلمة ثانية.")
        return

    _store_search(user_id, combined)
    await wait_msg.edit_text(
        f"🎶 نتائج لـ *{query}* — اختار الأغنية:",
        reply_markup=InlineKeyboardMarkup(_make_keyboard(combined)),
        parse_mode="Markdown",
    )


async def handle_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأزرار (بحث فقط).
    يدعم نتائج nogomistars (source=nogomi) ونتائج mp3j.cc (source=mp3j).
    """
    cb = update.callback_query
    await cb.answer()

    user_id = update.effective_user.id
    results = user_search_results.get(user_id, [])
    idx     = int(cb.data.split("_")[1])

    if idx >= len(results):
        await cb.edit_message_text("❌ انتهت صلاحية النتائج، ابحث مرة ثانية.")
        return

    track  = results[idx]
    title  = track["title"]
    source = track.get("source", "mp3j")
    artist, song_title = split_artist_title(title)

    # ── كاش: لو شُغّلت قبل تجي بثانية ──────────────────────────────
    cached_fid = cache_get(title)
    if cached_fid:
        caption = build_caption(BOT_USERNAME, "")
        try:
            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=cached_fid,
                title=song_title or title,
                performer=artist or None,
                caption=caption,
            )
            await cb.delete_message()
            return
        except Exception:
            pass  # الكاش منتهي → كمّل التحميل الطبيعي

    await cb.edit_message_text(
        f"⏳ جاري التحميل...\n🎵 *{title}*",
        parse_mode="Markdown",
    )

    loop = asyncio.get_event_loop()

    # ── sm3ha.io: تحميل عبر savemp3 ────────────────────────────────
    if source == "sm3ha":
        yt_id  = track.get("yt_id", "")
        yt_url = f"https://www.youtube.com/watch?v={yt_id}"
        await _download_and_send_yt_cb(yt_url, cb, update.effective_chat.id, context, cache_key=title)
        return

    # ── mp3j YouTube: تحميل عبر savemp3 ────────────────────────────
    if source == "mp3j_yt":
        yt_id = track.get("yt_id", "")
        yt_url = f"https://www.youtube.com/watch?v={yt_id}"
        await _download_and_send_yt_cb(yt_url, cb, update.effective_chat.id, context, cache_key=title)
        return

    # ── nogomistars.com: تحميل مباشر ───────────────────────────────
    if source == "nogomi":
        dl_url = track["dl_url"]
        file_path, dl_title, _ = await loop.run_in_executor(
            None, nogomistars_download, dl_url, title
        )
        if file_path and os.path.exists(file_path):
            try:
                size_mb = os.path.getsize(file_path) / (1024 * 1024)
                if size_mb > 50:
                    await cb.edit_message_text(
                        f"❌ الملف كبير ({size_mb:.1f}MB)، تلغرام لا يقبل أكثر من 50MB."
                    )
                    return
                await cb.edit_message_text(
                    f"📤 جاري الإرسال...\n🎵 *{title}*",
                    parse_mode="Markdown",
                )
                ok2, file_id = await send_audio_file(
                    context.bot, update.effective_chat.id, file_path,
                    title=song_title or title, duration_str="", performer=artist)
                if ok2:
                    if file_id: cache_set(title, file_id)
                    await cb.delete_message()
                else:
                    await cb.edit_message_text("❌ حدث خطأ أثناء الإرسال.")
                return
            finally:
                if os.path.exists(file_path):
                    try: os.unlink(file_path)
                    except Exception: pass
        # تحميل مباشر فشل → savemp3
        yt_id = track.get("yt_id")
        if yt_id:
            await _download_and_send_yt_cb(f"https://www.youtube.com/watch?v={yt_id}",
                cb, update.effective_chat.id, context, cache_key=title)
        else:
            await _fallback_search_download(title, cb.message, update.effective_chat.id, context)
        return

    # ── mp3j.cc: WebSocket prepare → direct MP3 download ───────────
    track_id = track["id"]
    duration = track["duration"]
    query    = track["query"]

    ok = await mp3j_prepare(track_id, query)
    if not ok:
        await _fallback_search_download(title, cb.message, update.effective_chat.id, context)
        return

    await cb.edit_message_text(
        f"📥 جاري التحميل...\n🎵 *{title}*",
        parse_mode="Markdown",
    )

    loop      = asyncio.get_event_loop()
    file_path = await loop.run_in_executor(None, mp3j_download, track_id, query, title)

    if not file_path or not os.path.exists(file_path):
        await _fallback_search_download(title, cb.message, update.effective_chat.id, context)
        return

    try:
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > 50:
            await cb.edit_message_text(
                f"❌ الملف كبير ({size_mb:.1f}MB)، تلغرام لا يقبل أكثر من 50MB."
            )
            return

        await cb.edit_message_text(
            f"📤 جاري الإرسال...\n🎵 *{title}*",
            parse_mode="Markdown",
        )

        ok2, file_id = await send_audio_file(
            context.bot, update.effective_chat.id, file_path,
            title=song_title or title, duration_str=duration, performer=artist)
        if ok2:
            if file_id: cache_set(title, file_id)
            await cb.delete_message()
        else:
            await cb.edit_message_text("❌ حدث خطأ أثناء الإرسال.")
    finally:
        if file_path and os.path.exists(file_path):
            try: os.unlink(file_path)
            except Exception: pass


# ─── Main ─────────────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """يجيب اسم المستخدم للبوت بعد ما يشتغل."""
    global BOT_USERNAME
    me = await application.bot.get_me()
    BOT_USERNAME = me.username or ""
    logger.info(f"Bot username: @{BOT_USERNAME}")


def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        return

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
