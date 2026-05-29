import os
import json
import logging
import asyncio
import tempfile
import re
import time
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
    try:
        with open(GROUPS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_groups(groups: set[int]) -> None:
    with open(GROUPS_FILE, "w") as f:
        json.dump(list(groups), f)

ALLOWED_GROUPS: set[int] = _load_groups()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://mp3j.cc",
    "Referer": "https://mp3j.cc/ar/",
}

# { user_id: [results] }
user_search_results: dict[int, list] = {}

# Bot username — filled on startup
BOT_USERNAME: str = ""

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
    """بحث في mp3j.cc ويرجع قائمة نتائج SoundCloud."""
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

    results = []
    for track in data.get("SoundCloud", [])[:10]:
        track_id = track.get("id")
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
        })
    return results


async def mp3j_prepare(track_id: str, query: str) -> bool:
    """يتصل بـ WebSocket على cdn.mp3j.cc لتحضير ملف MP3."""
    uri = (
        f"wss://cdn.mp3j.cc/WS/SoundCloud/track"
        f"?query={requests.utils.quote(query)}&id={track_id}"
    )
    ws_headers = {
        "Origin": "https://mp3j.cc",
        "User-Agent": HEADERS["User-Agent"],
    }
    try:
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
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        return False
    return True


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
SAVEMP3_PATH = "/jycue/youtube-video-to-mp3/"
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
    "%5B%22%22%2C%20%7B%22children%22%3A%20%5B%5B%22site%22%2C%20%22jycue%22%2C%20%22d%22%5D%2C"
    "%20%7B%22children%22%3A%20%5B%5B%22slug%22%2C%20%22youtube-video-to-mp3%22%2C%20%22d%22%5D%2C"
    "%20%7B%22children%22%3A%20%5B%22__PAGE__%22%2C%20%7B%7D%5D%7D%5D%7D%5D%7D%2C%20null%2C%20null%2C%20true%5D"
)


def _savemp3_action(action_hash: str, payload: list) -> str:
    # زيارة الصفحة أولاً لأخذ الـ cookies المطلوبة
    s = requests.Session()
    s.headers.update({"User-Agent": SAVEMP3_HEADERS["User-Agent"]})
    s.get(SAVEMP3_BASE + SAVEMP3_PATH, timeout=15)
    r = s.post(
        SAVEMP3_BASE + SAVEMP3_PATH,
        data=json.dumps(payload),
        headers={
            **SAVEMP3_HEADERS,
            "Next-Action": action_hash,
            "Next-Router-State-Tree": _SAVEMP3_ROUTER_STATE,
            "Accept": "text/x-component,*/*",
        },
        timeout=60,
    )
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
    try:
        raw = _savemp3_action(SA_VIDEO_INFO, [yt_url])
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
        raw2 = _savemp3_action(SA_GET_DOWNLOAD, payload)
        d2 = _parse_savemp3_success(raw2)
    except Exception as e:
        logger.error(f"savemp3_start_download error: {e}")
        return None, title, duration

    if not d2 or "taskId" not in d2:
        return None, title, duration

    task_id = d2["taskId"]

    # Poll status
    deadline = time.time() + 180
    download_url = None
    while time.time() < deadline:
        try:
            raw3 = _savemp3_action(SA_GET_STATUS, [task_id])
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

    # Download file
    try:
        resp = requests.get(
            download_url,
            headers={"User-Agent": SAVEMP3_HEADERS["User-Agent"]},
            stream=True, timeout=120, allow_redirects=True,
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


# ─── Fallback: بحث + تحميل من المصادر الاحتياطية ─────────────────────────────

async def _fallback_search_download(
    query: str,
    wait_msg,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """يبحث في sm3ha → YouTube ثم يحمّل عبر savemp3."""
    loop = asyncio.get_event_loop()
    await wait_msg.edit_text(f"🔄 جاري البحث عن: *{query}*...", parse_mode="Markdown")
    yt_url = await loop.run_in_executor(None, sm3ha_search_first, query)
    if not yt_url:
        yt_url = await loop.run_in_executor(None, youtube_search_first, query)
    if not yt_url:
        await wait_msg.edit_text("❌ ما قدرت أحمّل الأغنية، جرب لاحقاً.")
        return
    await _download_and_send_yt(yt_url, wait_msg, chat_id, context)


# ─── YouTube fallback: download & send ────────────────────────────────────────

async def _download_and_send_yt(
    yt_url: str,
    wait_msg,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    يأخذ رابط يوتيوب → يحمّله عبر savemp3.net → يرسله.
    يستخدم wait_msg للتحديث أثناء العملية.
    """
    loop = asyncio.get_event_loop()

    await wait_msg.edit_text("⏳ جاري التحويل والتحميل...")

    file_path, title, duration_sec = await loop.run_in_executor(
        None, savemp3_full_download, yt_url
    )

    if not file_path or not os.path.exists(file_path):
        await wait_msg.edit_text("❌ ما قدرت أحمّل الأغنية، جرب لاحقاً.")
        return

    try:
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > 50:
            await wait_msg.edit_text(
                f"❌ الملف كبير ({size_mb:.1f}MB)، تلغرام لا يقبل أكثر من 50MB."
            )
            return

        artist, song_title = split_artist_title(title)
        duration_str = fmt_sec(duration_sec)

        sent = await send_audio_file(
            context.bot, chat_id, file_path,
            title=song_title or title,
            duration_str=duration_str,
            performer=artist,
        )
        if sent:
            await wait_msg.delete()
        else:
            await wait_msg.edit_text("❌ حدث خطأ أثناء الإرسال.")
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
) -> bool:
    """يرسل ملف الصوت بالديزاين المطلوب ويرجع True إذا نجح."""
    caption = build_caption(BOT_USERNAME, duration_str)
    try:
        with open(file_path, "rb") as f:
            await bot.send_audio(
                chat_id=chat_id,
                audio=f,
                title=title,
                performer=performer or None,
                caption=caption,
            )
        return True
    except Exception as e:
        logger.error(f"send_audio error: {e}")
        return False


# ─── يوت: تحميل فوري (SoundCloud أول نتيجة) ──────────────────────────────────

async def yot_instant_search(msg, query: str, context: ContextTypes.DEFAULT_TYPE):
    """يوت + اسم أغنية: يجيب أول نتيجة ويحمّلها فوراً بدون أزرار."""
    wait_msg = await msg.reply_text(
        f"🎵 جاري البحث والتحميل: *{query}*...",
        parse_mode="Markdown",
    )

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, mp3j_search, query)

    if not results:
        # ── Fallback 2: sm3ha.io ────────────────────────────────────
        await wait_msg.edit_text(
            f"🔄 جاري البحث عن: *{query}*...",
            parse_mode="Markdown",
        )
        yt_url = await loop.run_in_executor(None, sm3ha_search_first, query)
        # ── Fallback 3: YouTube scraping ────────────────────────────
        if not yt_url:
            yt_url = await loop.run_in_executor(None, youtube_search_first, query)
        if not yt_url:
            await wait_msg.edit_text("❌ ما لقيت الأغنية، جرب كلمة ثانية.")
            return
        await _download_and_send_yt(yt_url, wait_msg, msg.chat_id, context)
        return

    track    = results[0]
    track_id = track["id"]
    title    = track["title"]
    duration = track["duration"]
    query_q  = track["query"]

    artist, song_title = split_artist_title(title)

    await wait_msg.edit_text(
        f"⏳ جاري التحضير...\n🎵 *{title}*",
        parse_mode="Markdown",
    )

    ok = await mp3j_prepare(track_id, query_q)
    if not ok:
        await _fallback_search_download(title, wait_msg, msg.chat_id, context)
        return

    await wait_msg.edit_text(
        f"📥 جاري التحميل...\n🎵 *{title}*",
        parse_mode="Markdown",
    )

    file_path = await loop.run_in_executor(None, mp3j_download, track_id, query_q, title)

    if not file_path or not os.path.exists(file_path):
        await _fallback_search_download(title, wait_msg, msg.chat_id, context)
        return

    try:
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > 50:
            await wait_msg.edit_text(
                f"❌ الملف كبير ({size_mb:.1f}MB)، تلغرام لا يقبل أكثر من 50MB."
            )
            return

        sent = await send_audio_file(
            context.bot, msg.chat_id, file_path,
            title=song_title or title,
            duration_str=duration,
            performer=artist,
        )
        if sent:
            await wait_msg.delete()
        else:
            await wait_msg.edit_text("❌ حدث خطأ أثناء الإرسال.")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except Exception:
                pass


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
      1. يبحث في يوتيوب ويجيب رابط المقطع
      2. يحط الرابط في ar.savemp3.net
      3. يحمّل الـ MP3 ويرسله
    مستقل تماماً عن بحث ويوت.
    """
    wait_msg = await msg.reply_text(
        f"🔍 جاري البحث عن: *{query}*...",
        parse_mode="Markdown",
    )
    loop = asyncio.get_event_loop()

    # الخطوة 1: يبحث في يوتيوب ويجيب الرابط
    yt_url = await loop.run_in_executor(None, youtube_search_first, query)
    if not yt_url:
        await wait_msg.edit_text("❌ ما لقيت الأغنية في يوتيوب، جرب كلمة ثانية.")
        return

    # الخطوة 2+3: يحط الرابط في ar.savemp3.net ويحمّله
    await _download_and_send_yt(yt_url, wait_msg, msg.chat_id, context)


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

    # ── بحث: قائمة أزرار ───────────────────────────────────────────
    wait_msg = await msg.reply_text(
        f"🔍 جاري البحث عن: *{query}*...",
        parse_mode="Markdown",
    )

    loop    = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, mp3j_search, query)

    if not results:
        # ── Fallback 2: sm3ha.io ──────────────────────────────────
        await wait_msg.edit_text(
            f"🔄 جاري البحث عن: *{query}*...",
            parse_mode="Markdown",
        )
        yt_url = await loop.run_in_executor(None, sm3ha_search_first, query)
        # ── Fallback 3: YouTube scraping ─────────────────────────
        if not yt_url:
            yt_url = await loop.run_in_executor(None, youtube_search_first, query)
        if not yt_url:
            await wait_msg.edit_text("❌ ما لقيت الأغنية، جرب كلمة ثانية.")
            return
        await _download_and_send_yt(yt_url, wait_msg, msg.chat_id, context)
        return

    user_id = update.effective_user.id
    user_search_results[user_id] = results

    keyboard = []
    for i, r in enumerate(results):
        dur   = f" [{r['duration']}]" if r["duration"] else ""
        label = f"🎵 {r['title'][:48]}{dur}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"dl_{i}")])

    await wait_msg.edit_text(
        f"🎶 نتائج لـ *{query}* — اختار الأغنية:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأزرار (بحث فقط)."""
    cb = update.callback_query
    await cb.answer()

    user_id = update.effective_user.id
    results = user_search_results.get(user_id, [])
    idx     = int(cb.data.split("_")[1])

    if idx >= len(results):
        await cb.edit_message_text("❌ انتهت صلاحية النتائج، ابحث مرة ثانية.")
        return

    track    = results[idx]
    track_id = track["id"]
    title    = track["title"]
    duration = track["duration"]
    query    = track["query"]
    artist, song_title = split_artist_title(title)

    await cb.edit_message_text(
        f"⏳ جاري التحضير...\n🎵 *{title}*",
        parse_mode="Markdown",
    )

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

        sent = await send_audio_file(
            context.bot, update.effective_chat.id, file_path,
            title=song_title or title,
            duration_str=duration,
            performer=artist,
        )
        if sent:
            await cb.delete_message()
        else:
            await cb.edit_message_text("❌ حدث خطأ أثناء الإرسال.")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except Exception:
                pass


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
