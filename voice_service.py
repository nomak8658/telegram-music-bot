#!/usr/bin/env python3
"""
Voice call service using Telethon + pytgcalls.
مستوحى من nomak8658/Music-Bot-Play — مُكيَّف ليعمل مباشرة كـ Python module.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

API_ID         = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
API_HASH       = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")
DATA_DIR       = Path(os.environ.get("DATA_DIR", "/data"))
SESSION_FILE   = DATA_DIR / "telethon_session.txt"

StreamEndCb = Callable[[int], Awaitable[None]]


def _load_session() -> str:
    try:
        if SESSION_FILE.exists():
            s = SESSION_FILE.read_text().strip()
            if s:
                return s
    except Exception as e:
        logger.warning(f"[voice] session read error: {e}")
    return ""


def _save_session(s: str):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(s)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"[voice] session save error: {e}")


class VoiceService:
    def __init__(self):
        self._enabled  = bool(API_ID and API_HASH)
        self._tl       = None   # Telethon TelegramClient
        self._calls    = None   # PyTgCalls instance
        self._session  = SESSION_STRING or _load_session()
        self._end_cb: Optional[StreamEndCb] = None

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def logged_in(self) -> bool:
        return self._tl is not None and self._tl.is_connected()

    def set_stream_end_callback(self, cb: StreamEndCb):
        self._end_cb = cb

    # ── Startup ──────────────────────────────────────────────────────────────

    async def start(self):
        """Auto-connect if a saved session exists."""
        if not self._enabled:
            logger.info("[voice] disabled — TELEGRAM_API_ID/TELEGRAM_API_HASH not set")
            return
        if not self._session:
            logger.info("[voice] no saved session — use /qr to log in")
            return
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            tl = TelegramClient(StringSession(self._session), API_ID, API_HASH)
            await tl.start()
            me = await tl.get_me()
            self._tl = tl
            logger.info(f"[voice] auto-connected as {getattr(me, 'first_name', '?')}")
        except Exception as e:
            logger.error(f"[voice] auto-connect failed: {e}")

    # ── Internal: get/init pytgcalls ─────────────────────────────────────────

    async def _get_calls(self):
        if self._calls is None:
            if not self._tl:
                raise RuntimeError("لا يوجد حساب متصل — استخدم /qr لتسجيل الدخول")
            from pytgcalls import PyTgCalls
            calls = PyTgCalls(self._tl)

            async def _on_any_update(update):
                try:
                    n = type(update).__name__
                    if "Ended" in n or "End" in n:
                        cid = getattr(update, "chat_id", None)
                        if cid is not None and self._end_cb:
                            await self._end_cb(cid)
                except Exception as ex:
                    logger.warning(f"[voice] stream_end cb error: {ex}")

            registered = False
            if hasattr(calls, "on_update"):
                try:
                    @calls.on_update()
                    async def _h1(_, upd): await _on_any_update(upd)
                    registered = True
                except Exception:
                    pass
            if not registered and hasattr(calls, "on_stream_end"):
                try:
                    @calls.on_stream_end()
                    async def _h2(_, upd): await _on_any_update(upd)
                except Exception:
                    pass

            await calls.start()
            self._calls = calls
        return self._calls

    # ── QR Login ─────────────────────────────────────────────────────────────

    async def qr_login(
        self,
        on_qr_url: Callable[[str], Awaitable[None]],
        on_done:   Callable[[dict], Awaitable[None]],
    ):
        """
        Start QR login.
        on_qr_url(url) — called immediately when QR is ready.
        on_done(result) — called when login succeeds, fails, or times out.
        """
        if not self._enabled:
            await on_done({"ok": False, "error": "TELEGRAM_API_ID/TELEGRAM_API_HASH غير مضبوطَين في Railway"})
            return
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            if self._tl is not None:
                try:
                    await self._tl.disconnect()
                except Exception:
                    pass
                self._tl = None
                self._calls = None

            tl = TelegramClient(StringSession(), API_ID, API_HASH)
            await tl.connect()
            qr = await tl.qr_login()
            await on_qr_url(qr.url)
            asyncio.create_task(self._wait_qr(tl, qr, on_done))
        except Exception as e:
            await on_done({"ok": False, "error": f"{type(e).__name__}: {e}"})

    async def _wait_qr(self, tl, qr, on_done):
        try:
            await qr.wait(120)
            me = await tl.get_me()
            from telethon.sessions import StringSession
            sess = tl.session.save()
            self._tl     = tl
            self._calls  = None
            self._session = sess
            _save_session(sess)
            await on_done({
                "ok": True,
                "name":  getattr(me, "first_name", "") or "",
                "phone": getattr(me, "phone", "") or "",
            })
        except asyncio.TimeoutError:
            await on_done({"ok": False, "error": "انتهت صلاحية رمز QR (120 ث). اكتب /qr مجدداً."})
            try:
                await tl.disconnect()
            except Exception:
                pass
        except Exception as e:
            n = type(e).__name__
            if "SessionPasswordNeeded" in n or "2FA" in str(e):
                msg = "الحساب محمي بـ 2FA. أوقف التحقق بخطوتين مؤقتاً ثم أعد المحاولة."
            else:
                msg = str(e)
            await on_done({"ok": False, "error": msg})
            try:
                await tl.disconnect()
            except Exception:
                pass

    # ── Session check ────────────────────────────────────────────────────────

    async def check_session(self) -> dict:
        try:
            if not self._tl or not self._tl.is_connected():
                return {"ok": False, "error": "غير متصل"}
            me = await self._tl.get_me()
            return {
                "ok":    True,
                "name":  getattr(me, "first_name", "") or "",
                "phone": getattr(me, "phone", "") or "",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Voice commands ───────────────────────────────────────────────────────

    async def join_and_play(self, chat_id: int, audio_file: str) -> dict:
        try:
            from pytgcalls.types import MediaStream
            tgc = await self._get_calls()
            stream = MediaStream(audio_file, video_flags=MediaStream.Flags.IGNORE)
            await tgc.play(chat_id, stream)
            return {"ok": True}
        except Exception as e:
            logger.error(f"[voice] join_and_play: {e}")
            return {"ok": False, "error": repr(e)}

    async def stop(self, chat_id: int) -> dict:
        try:
            tgc = await self._get_calls()
            await tgc.leave_call(chat_id)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def pause(self, chat_id: int) -> dict:
        try:
            tgc = await self._get_calls()
            await tgc.pause_stream(chat_id)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def resume(self, chat_id: int) -> dict:
        try:
            tgc = await self._get_calls()
            await tgc.resume_stream(chat_id)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


voice_svc = VoiceService()
