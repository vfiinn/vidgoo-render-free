"""Download public videos from Twitter/X, Instagram and TikTok."""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from aiohttp import web
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_FILE_SIZE_BYTES = 50_000_000
COMPRESSION_TARGET_BYTES = 47_000_000
COMPRESSION_TIMEOUT_SECONDS = 900
SUPPORTED_DOMAINS = ("twitter.com", "x.com", "instagram.com", "tiktok.com")
GENERIC_URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
logger = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"


class SafeFormatter(logging.Formatter):
    """Redact tokens from messages AND formatted exception tracebacks."""
    def format(self, record):
        text = super().format(record)
        if BOT_TOKEN:
            text = text.replace(BOT_TOKEN, "[REDACTED]")
        return re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", text)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def extract_supported_url(text: str) -> str | None:
    for match in GENERIC_URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(").,،؛!?؟\"'”»")
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                continue
        except ValueError:
            continue
        if any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS):
            return url
    return None


class YTDLPLogger:
    def debug(self, message):
        # Keep diagnostic details only when explicitly enabling DEBUG locally.
        if message.startswith("[debug]"):
            logger.debug(message)
        else:
            logger.info(message)

    def warning(self, message):
        logger.warning(message)

    def error(self, message):
        logger.error(message)


def build_ydl_options(out_dir: Path, url: str = "") -> dict:
    return {
        "format": "bv*+ba/b",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "usenetrc": False,
        "retries": 3,
        "fragment_retries": 3,
        # Never report success after silently omitting an unavailable fragment.
        "skip_unavailable_fragments": False,
        # Fetch HLS/DASH fragments in parallel without changing video quality.
        "concurrent_fragment_downloads": 4,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoConvertor", "preferedformat": "mp4",
        }],
        # Let current extractors choose headers and browser impersonation.
        "logger": YTDLPLogger(),
        "quiet": True,
        "no_warnings": False,
    }


class DownloadFailure(RuntimeError):
    pass


def download_error_message(error: Exception) -> str:
    message = str(error).lower()
    if "suspended" in message:
        return "المنصة أبلغت أن الحساب أو المحتوى موقوف. جرّب رابطًا آخر."
    if any(x in message for x in ("sign in", "login required", "log in",
                                  "login_required", "requiring login",
                                  "private", "not authorized")):
        return "هذا الطلب يتطلب تسجيل دخول أو صلاحية وصول. البوت يعمل بدون كوكيز حساب."
    if any(x in message for x in ("429", "too many requests", "rate limit")):
        return "المنصة حدّت عدد الطلبات مؤقتًا. انتظر قليلًا قبل المحاولة مجددًا."
    if any(x in message for x in ("unexpected response from webpage",
                                  "challenge", "captcha", "403", "forbidden")):
        return "تعذّر قراءة رد المنصة؛ قد تكون حجبت الطلب أو غيّرت صفحة الفيديو. جرّب لاحقًا."
    if any(x in message for x in ("timed out", "timeout", "connection",
                                  "unable to download webpage")):
        return "تعذّر الاتصال بالمنصة. حاول مجددًا لاحقًا."
    if any(x in message for x in ("not found", "404", "deleted", "unavailable",
                                  "no video", "no formats")):
        return "الفيديو غير متاح للتنزيل أو الرابط لا يحتوي فيديو مدعومًا."
    return "فشل تنزيل الفيديو. تأكد من رابط المنشور العام أو جرّب رابطًا آخر."


def find_downloaded_file(info: dict, out_dir: Path, prepared_filename: str) -> Path:
    root = out_dir.resolve()
    prepared = Path(prepared_filename)
    candidates = [prepared.with_suffix(".mp4"), prepared]
    for item in info.get("requested_downloads") or []:
        if item.get("filepath"):
            candidates.append(Path(item["filepath"]))
    candidates.extend(out_dir.glob("*.mp4"))
    for candidate in candidates:
        candidate = candidate.resolve()
        if (candidate.parent == root and candidate.is_file()
                and candidate.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
                and not re.search(r"\.f[^.]+\.", candidate.name)):
            return candidate
    raise DownloadFailure("لم ينتج التنزيل ملف فيديو مكتملًا.")


def download_media(url: str, out_dir: Path) -> tuple[Path, str | None]:
    if extract_supported_url(url) != url:
        raise DownloadFailure("الروابط المدعومة: تويتر/X وإنستغرام وتيك توك فقط.")
    try:
        with yt_dlp.YoutubeDL(build_ydl_options(out_dir, url)) as ydl:
            info = ydl.extract_info(url, download=True)
            # A post with multiple videos may still return a playlist object.
            while info and info.get("_type") in ("playlist", "multi_video"):
                info = next((entry for entry in info.get("entries") or [] if entry), None)
            if not info:
                raise DownloadFailure("الرابط لم يُرجع فيديو قابلًا للتنزيل.")
            return find_downloaded_file(info, out_dir, ydl.prepare_filename(info)), info.get("id")
    except yt_dlp.utils.DownloadError as exc:
        logger.warning("Download failed: %s", exc)
        raise DownloadFailure(download_error_message(exc)) from exc


class CompressionError(RuntimeError):
    """رسالة آمنة يمكن عرضها للمستخدم عند فشل الضغط."""


def require_media_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise CompressionError(
                "أداة FFmpeg أو FFprobe غير متوفرة على الخادم."
            )


def run_media_command(command: list[str], timeout: int):
    try:
        return subprocess.run(
            command, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CompressionError("انتهت مهلة ضغط الفيديو. جرّب فيديو أقصر.") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("Media command failed: %s", exc)
        raise CompressionError("تعذّر معالجة الفيديو بواسطة FFmpeg.") from exc


def probe_media(path: Path) -> tuple[float, bool]:
    result = run_media_command([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type", "-of", "json", str(path),
    ], 30)
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data["streams"]
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid duration")
        if not any(s["codec_type"] == "video" for s in streams):
            raise ValueError("Missing video stream")
        return duration, any(s["codec_type"] == "audio" for s in streams)
    except (ValueError, KeyError, TypeError) as exc:
        raise CompressionError("تعذّر قراءة مدة الفيديو أو مسار الصورة.") from exc


def compress_video(source: Path) -> Path:
    """Two-pass H.264/AAC encoding with a muxing margin and one size retry."""
    require_media_tools()
    duration, has_audio = probe_media(source)
    # Separate logs/output from downloaded files, even if their names coincide.
    encode_dir = Path(tempfile.mkdtemp(prefix="encode-", dir=source.parent))
    output = encode_dir / "compressed.mp4"
    passlog = encode_dir / "pass"
    target = COMPRESSION_TARGET_BYTES
    for attempt in range(2):
        total_bitrate = int(target * 8 / duration)
        audio_bitrate = min(96_000, max(32_000, total_bitrate // 5)) if has_audio else 0
        video_bitrate = total_bitrate - audio_bitrate
        if video_bitrate < 50_000:
            raise CompressionError("الفيديو طويل جدًا لضغطه تحت 50MB بجودة مقبولة.")
        common = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source), "-map", "0:v:0",
            "-c:v", "libx264", "-preset", "fast", "-b:v", str(video_bitrate),
            "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-pix_fmt", "yuv420p", "-threads", "2",
            "-passlogfile", str(passlog),
        ]
        run_media_command(
            common + ["-pass", "1", "-an", "-f", "null", os.devnull],
            COMPRESSION_TIMEOUT_SECONDS,
        )
        audio = (
            ["-map", "0:a:0", "-c:a", "aac", "-b:a", str(audio_bitrate), "-ac", "2"]
            if has_audio else ["-an"]
        )
        run_media_command(
            common + ["-pass", "2"] + audio + [
                "-map_metadata", "-1", "-map_chapters", "-1",
                "-movflags", "+faststart", str(output),
            ],
            COMPRESSION_TIMEOUT_SECONDS,
        )
        if not output.is_file() or output.stat().st_size == 0:
            raise CompressionError("لم ينتج الضغط ملف فيديو صالحًا.")
        size = output.stat().st_size
        if size < MAX_FILE_SIZE_BYTES:
            output_duration, output_audio = probe_media(output)
            if abs(output_duration - duration) > max(1.0, duration * 0.01):
                raise CompressionError("الملف المضغوط غير مكتمل؛ لم يتم إرساله.")
            if has_audio and not output_audio:
                raise CompressionError("فُقد الصوت أثناء الضغط؛ لم يتم إرسال الملف.")
            return output
        target = int(target * (COMPRESSION_TARGET_BYTES / size) * 0.9)
        logger.info("Compressed file still too large; retrying at a lower bitrate.")
    raise CompressionError("تعذّر تقليل حجم الفيديو إلى أقل من 50MB.")


async def run_media_worker(function, *args):
    # Cancellation must not remove a directory while its worker still writes to it.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            logger.exception("Media worker failed during cancellation")
        raise



async def download_media_async(url: str, out_dir: Path):
    return await run_media_worker(download_media, url, out_dir)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "أهلًا! ابعت رابط فيديو من تويتر/X أو إنستغرام أو تيك توك 🎬\n"
            "أضغط الفيديو الكبير تلقائيًا ليصبح أقل من 50MB."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "أرسل رابط منشور فيديو من تويتر/X أو إنستغرام أو تيك توك.\n"
            "التحميل بدون كوكيز حساب؛ المحتوى الذي تطلب المنصة تسجيل دخول له غير مدعوم.\n"
            "إذا احتوى المنشور عدة فيديوهات، يُحمّل أول فيديو فقط.\n"
            "ضغط الفيديو الكبير قد يستغرق عدة دقائق."
        )


async def edit_status(status, text):
    if status:
        try:
            await status.edit_text(text)
        except Exception:
            logger.warning("Could not update status message", exc_info=True)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    url = extract_supported_url(update.message.text or "")
    if not url:
        await update.message.reply_text(
            "أرسل رابط فيديو من تويتر/X أو إنستغرام أو تيك توك فقط."
        )
        return
    status = None
    request_dir = None
    try:
        status = await update.message.reply_text("⏳ جاري تنزيل الفيديو...")
        request_dir = Path(tempfile.mkdtemp(prefix="request-", dir=DOWNLOAD_DIR))
        file_path, _ = await download_media_async(url, request_dir)
        if file_path.stat().st_size >= MAX_FILE_SIZE_BYTES:
            await edit_status(status, "🗜️ جاري ضغط الفيديو إلى أقل من 50MB؛ قد يستغرق عدة دقائق...")
            file_path = await run_media_worker(compress_video, file_path)
        if not 0 < file_path.stat().st_size < MAX_FILE_SIZE_BYTES:
            raise CompressionError("حجم الملف غير مناسب للرفع؛ لم يتم إرساله.")
        await edit_status(status, f"📤 جاري رفع الفيديو ({file_path.stat().st_size / 1_000_000:.1f}MB)...")
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO
            )
        except Exception:
            logger.warning("Could not send chat action")
        with file_path.open("rb") as video_file:
            await update.message.reply_video(
                video=video_file, caption="✅ تفضل فيديوك",
                supports_streaming=True, write_timeout=180,
                read_timeout=180, connect_timeout=30,
            )
        try:
            await status.delete()
        except Exception:
            pass
    except (DownloadFailure, CompressionError) as exc:
        await edit_status(status, f"❌ {exc}")
    except Exception:
        logger.exception("Download/upload request failed")
        await edit_status(status, "❌ حدث خطأ أثناء تجهيز الفيديو أو إرساله. حاول لاحقًا.")
    finally:
        if request_dir:
            try:
                shutil.rmtree(request_dir)
            except OSError:
                logger.exception("Could not clean request directory")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        logger.error(
            "Polling conflict: another instance uses this BOT_TOKEN. "
            "Run one instance only; deploy overlap may cause a brief conflict."
        )
    else:
        logger.error("Telegram error: %s", context.error)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Process is running")

    def log_message(self, format, *args):
        pass


def webhook_secret(token: str) -> str:
    """Derive a stable secret header without exposing the bot token in URLs."""
    return hmac.new(
        token.encode("utf-8"), b"vidgoo-webhook-v1", hashlib.sha256
    ).hexdigest()


def create_web_app(application, base_url: str, token: str) -> web.Application:
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("WEBHOOK_URL must be the HTTPS service URL without a path.")
    secret = webhook_secret(token)
    # Telegram may retry a delivery; avoid downloading it twice in this process.
    received = OrderedDict()

    async def health(request):
        if not application.running:
            return web.json_response({"status": "starting"}, status=503)
        return web.json_response({"status": "ok"})

    async def receive_update(request):
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("ascii")):
            raise web.HTTPUnauthorized()
        if not application.running:
            raise web.HTTPServiceUnavailable()
        try:
            payload = await request.json()
            if (not isinstance(payload, dict)
                    or type(payload.get("update_id")) is not int):
                raise ValueError("Invalid update")
            update = Update.de_json(payload, application.bot)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text="Invalid update") from None
        if update.update_id in received:
            return web.json_response({"ok": True})
        try:
            application.update_queue.put_nowait(update)
        except asyncio.QueueFull:
            # A non-2xx response tells Telegram to retry instead of losing this update.
            raise web.HTTPServiceUnavailable() from None
        received[update.update_id] = None
        if len(received) > 256:
            received.popitem(last=False)
        # The download runs through Application's queue, not in this HTTP request.
        return web.json_response({"ok": True})

    async def lifecycle(web_app):
        async with application:
            await application.start()
            try:
                await application.bot.set_webhook(
                    url=f"{base_url.rstrip('/')}{WEBHOOK_PATH}",
                    secret_token=secret,
                    allowed_updates=["message"],
                    max_connections=4,
                    drop_pending_updates=False,
                )
                logger.info("Vidgoo webhook registered successfully")
                yield
            finally:
                # Keep the webhook so Telegram can wake the service after idle sleep.
                # Drain accepted updates before closing the Telegram API session.
                await application.stop()

    web_app = web.Application(client_max_size=1024 * 1024)
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    web_app.router.add_post(WEBHOOK_PATH, receive_update)
    web_app.cleanup_ctx.append(lifecycle)
    return web_app


def create_application(token: str, *, webhook: bool):
    builder = Application.builder().token(token)
    if webhook:
        builder = builder.updater(None).update_queue(asyncio.Queue(maxsize=32))
    application = builder.build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.add_error_handler(on_error)
    return application


def main():
    configure_logging()
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير موجود.")
    require_media_tools()
    logger.info("Runtime: yt-dlp=%s; curl-cffi=%s",
                version("yt-dlp"), version("curl-cffi"))
    base_url = (os.environ.get("WEBHOOK_URL", "").strip()
                or os.environ.get("RENDER_EXTERNAL_URL", "").strip())
    port = int(os.environ.get("PORT", "10000"))
    if base_url:
        application = create_application(BOT_TOKEN, webhook=True)
        web_app = create_web_app(application, base_url, BOT_TOKEN)
        logger.info("Starting Vidgoo web server on port %s", port)
        web.run_app(web_app, host="0.0.0.0", port=port, access_log=None)
        return
    if os.environ.get("RENDER"):
        raise SystemExit("Add WEBHOOK_URL in Render Environment with your HTTPS service URL.")

    # Local polling is available when this token is not in use on Render.
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        app = create_application(BOT_TOKEN, webhook=False)
        logger.info("Bot started")
        # A stopped/closed Application must not be reused in an infinite retry loop.
        app.run_polling(allowed_updates=["message"], drop_pending_updates=False)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
