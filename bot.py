"""Download public videos, MP3 audio and post photos from X, Instagram and TikTok."""
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from aiohttp import web
from PIL import Image, ImageOps, UnidentifiedImageError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

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
MEDIA_MODES = {"video": "الفيديو", "audio": "الصوت", "photos": "الصور"}
MAX_PHOTOS = 20
PHOTO_TIMEOUT_SECONDS = 60
PHOTO_HTTP_TIMEOUT_SECONDS = 12
PHOTO_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "avif", "gif")
CHOICE_TTL_SECONDS = 15 * 60
UPLOAD_TIMEOUTS = dict(write_timeout=180, read_timeout=180, connect_timeout=30)
NO_AUDIO_MESSAGE = (
    "تعذّر الحصول على مسار صوت من هذا المنشور. قد لا تتيح المنصة الصوت لهذا الخادم "
    "أو قد يكون المقطع بلا صوت. لم يتم إنشاء ملف MP3 صامت."
)
INSTAGRAM_COMPLETE_FORMAT = "best*[format_id!^=dash-][vcodec!=?none]"


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


def build_ydl_options(out_dir: Path, url: str = "", mode: str = "video") -> dict:
    host = (urlsplit(url).hostname or "").lower()
    instagram = host == "instagram.com" or host.endswith(".instagram.com")
    options = {
        # Prefer Instagram's complete MP4: it often has sound even when the DASH
        # response exposes only video. Other platforms retain their original selector.
        "format": f"{INSTAGRAM_COMPLETE_FORMAT}/bv+ba/b" if instagram else "bv*+ba/b",
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
    if mode == "audio":
        # Prefer audio; unknown codecs are checked against the downloaded file.
        options["format"] = "bestaudio/best*[acodec!=none]/best*[acodec!=?none]"
        if instagram:
            options["format"] = f"bestaudio/{INSTAGRAM_COMPLETE_FORMAT}/best*[acodec!=none]"
        options.pop("merge_output_format", None)
        # Probe the source before converting, never extract from a video-only DASH track.
        options["postprocessors"] = []
    elif mode != "video":
        raise ValueError("Unsupported yt-dlp mode")
    return options


class DownloadFailure(RuntimeError):
    pass


def download_error_message(error: Exception) -> str:
    message = str(error).lower()
    if "unable to obtain file audio codec" in message:
        return NO_AUDIO_MESSAGE
    if "suspended" in message:
        return "المنصة أبلغت أن الحساب أو المحتوى موقوف. جرّب رابطًا آخر."
    if "empty media response" in message:
        return (
            "إنستغرام لم يُرجع بيانات هذا المنشور. قد يكون غير متاح دون تسجيل دخول "
            "أو حُجب الطلب. جرّب رابطًا عامًا آخر؛ البوت يعمل بدون كوكيز حساب."
        )
    if any(x in message for x in ("sign in", "login required", "log in",
                                  "login_required", "requiring login",
                                  "private", "not authorized", "authrequired",
                                  "authentication required", "authenticated cookies")):
        return "هذا الطلب يتطلب تسجيل دخول أو صلاحية وصول. البوت يعمل بدون كوكيز حساب."
    if any(x in message for x in ("429", "too many requests", "rate limit")):
        return "المنصة حدّت عدد الطلبات مؤقتًا. انتظر قليلًا قبل المحاولة مجددًا."
    if any(x in message for x in ("unexpected response from webpage",
                                  "challenge", "captcha", "403", "forbidden")):
        return "تعذّر قراءة رد المنصة؛ قد تكون حجبت الطلب أو غيّرت صفحة المنشور. جرّب لاحقًا."
    if any(x in message for x in ("timed out", "timeout", "connection",
                                  "unable to download webpage")):
        return "تعذّر الاتصال بالمنصة. حاول مجددًا لاحقًا."
    if any(x in message for x in ("not found", "404", "deleted", "unavailable",
                                  "no video", "no formats")):
        return "المحتوى غير متاح للتنزيل أو لا يحتوي الوسائط المطلوبة."
    return "فشل تنزيل المحتوى. تأكد من رابط المنشور العام أو جرّب رابطًا آخر."


def find_downloaded_file(info: dict, out_dir: Path, prepared_filename: str,
                         mode: str = "video") -> Path:
    root = out_dir.resolve()
    prepared = Path(prepared_filename)
    extension = prepared.suffix if mode == "source" else (".mp3" if mode == "audio" else ".mp4")
    candidates = [prepared.with_suffix(extension), prepared]
    if info.get("filepath"):
        candidates.append(Path(info["filepath"]))
    for item in info.get("requested_downloads") or []:
        if item.get("filepath"):
            candidates.append(Path(item["filepath"]))
    candidates.extend(out_dir.glob("*" + extension))
    allowed = {
        "audio": (".mp3",),
        "video": (".mp4", ".mkv", ".webm", ".mov"),
        "source": (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac",
                   ".mp4", ".mkv", ".webm", ".mov"),
    }[mode]
    for candidate in candidates:
        candidate = candidate.resolve()
        if (candidate.parent == root and candidate.is_file()
                and candidate.suffix.lower() in allowed
                and not re.search(r"\.f[^.]+\.", candidate.name)):
            return candidate
    raise DownloadFailure("لم ينتج التنزيل ملف صوت مكتملًا." if mode == "audio"
                          else "لم ينتج التنزيل ملف فيديو مكتملًا.")


def is_instagram_reel(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (host == "instagram.com" or host.endswith(".instagram.com")) and bool(
        re.fullmatch(r"/(?:reels?|tv)/[A-Za-z0-9_-]+/?", parsed.path)
    )


def canonical_media_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "instagram.com" or host.endswith(".instagram.com"):
        match = re.fullmatch(r"/(p|reels?|tv)/([A-Za-z0-9_-]+)/?", parsed.path)
        if match:
            kind = "reel" if match[1] in ("reel", "reels") else match[1]
            return f"https://www.instagram.com/{kind}/{match[2]}/"
    return url


def first_media_entry(info: dict | None):
    while info and info.get("_type") in ("playlist", "multi_video"):
        info = next((entry for entry in info.get("entries") or [] if entry), None)
    if not info:
        raise DownloadFailure("الرابط لم يُرجع وسائط قابلة للتنزيل.")
    return info


def download_media_attempt(url: str, out_dir: Path, mode: str, *,
                           metadata: dict | None = None, selector: str | None = None):
    options = build_ydl_options(out_dir, url, mode)
    if selector:
        options["format"] = selector
    with yt_dlp.YoutubeDL(options) as ydl:
        if metadata is None:
            info = ydl.extract_info(url, download=True)
        else:
            # Strip previous download state so a new selector cannot reuse an old merge.
            clean = copy.deepcopy(metadata)
            for key in ("requested_downloads", "requested_formats", "_filename", "filepath",
                        "__files_to_move", "url", "format_id", "ext", "protocol", "vcodec", "acodec",
                        "manifest_url", "manifest_stream_number", "format", "format_note", "container",
                        "filesize", "filesize_approx", "width", "height", "resolution", "fps", "abr",
                        "vbr", "tbr", "asr", "audio_channels", "fragments", "fragment_base_url",
                        "hls_media_playlist_data", "hls_aes", "request_data", "downloader_options"):
                clean.pop(key, None)
            info = ydl.process_ie_result(clean, download=True)
        info = first_media_entry(info)
        source_mode = "source" if mode == "audio" else "video"
        path = find_downloaded_file(info, out_dir, ydl.prepare_filename(info), source_mode)
        return path, info


def audio_fallback_selectors(info: dict, mode: str, excluded: set[str]):
    """Known audio first, then verify progressive files regardless of metadata flags."""
    formats = list(reversed(info.get("formats") or []))
    audio = [f for f in formats if f.get("vcodec") == "none"
             and f.get("acodec") not in (None, "none")]
    videos = [f for f in formats if f.get("vcodec") != "none"]
    combined = [f for f in videos if f.get("acodec") not in (None, "none")]
    progressive = [f for f in videos
                   if not str(f.get("format_id", "")).startswith("dash-")
                   and f.get("protocol") not in ("http_dash_segments", "m3u8", "m3u8_native")]
    selectors = []
    if mode == "audio":
        selectors.extend(f.get("format_id") for f in audio)
    elif audio and videos:
        # A claimed combined track would cause yt-dlp to omit the extra audio.
        mergeable = [f for f in videos if f.get("acodec") in (None, "none")]
        if mergeable:
            selectors.append(f"{mergeable[0]['format_id']}+{audio[0]['format_id']}")
    selectors.extend(f.get("format_id") for f in combined + progressive)
    seen = set(excluded)
    urls = {str(f.get("format_id")): f.get("url") for f in formats}
    def signature(selector):
        return tuple(urls.get(item) or f"format:{item}" for item in selector.split("+"))
    seen_files = {signature(selector) for selector in excluded}
    for selector in selectors:
        if selector and selector not in seen and signature(selector) not in seen_files:
            seen.add(selector)
            seen_files.add(signature(selector))
            yield selector


def validate_download_audio(path: Path, mode: str):
    _, has_audio = probe_media(path, audio_only=(mode == "audio"))
    if not has_audio:
        raise MissingAudioStream(NO_AUDIO_MESSAGE)


def recover_download_audio(url: str, out_dir: Path, mode: str, original: dict):
    metadata_sets = [original]
    host = (urlsplit(url).hostname or "").lower()
    if host == "instagram.com" or host.endswith(".instagram.com"):
        # A successful Instagram response sometimes omits its audio adaptation.
        # Refresh once only; a blocked/login-required response stops the request.
        options = build_ydl_options(out_dir, url, "video")
        options.update(socket_timeout=12, extractor_retries=0, retries=0)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                refreshed = first_media_entry(ydl.extract_info(url, download=False))
            metadata_sets.insert(0, refreshed)
        except yt_dlp.utils.DownloadError as exc:
            logger.warning("Audio metadata refresh failed: %s", exc)
            raise DownloadFailure(download_error_message(exc)) from exc
    excluded = {str(original.get("format_id", ""))}
    # The file itself proved this selected format has no audio, even if its
    # metadata claimed otherwise. This permits merging it with a separate track.
    metadata_sets = copy.deepcopy(metadata_sets)
    for metadata in metadata_sets:
        for fmt in metadata.get("formats") or []:
            if fmt.get("format_id") == original.get("format_id"):
                fmt["acodec"] = "none"
        # Diagnostic codec flags only: never log signed CDN URLs or account cookies.
        logger.info("Audio recovery formats: id=%s formats=%s", metadata.get("id"), [
            {key: fmt.get(key) for key in ("format_id", "vcodec", "acodec", "protocol")}
            for fmt in (metadata.get("formats") or [])[:24]
        ])
    attempts = 0
    for metadata in metadata_sets:
        for selector in audio_fallback_selectors(metadata, mode, excluded):
            if attempts >= 2:
                return None
            attempts += 1
            excluded.add(selector)
            directory = out_dir / f"audio-fallback-{attempts}"
            directory.mkdir()
            logger.info("Trying audio recovery: mode=%s format=%s", mode, selector)
            try:
                path, info = download_media_attempt(
                    url, directory, mode, metadata=metadata, selector=selector,
                )
                validate_download_audio(path, mode)
                return path, info
            except MissingAudioStream:
                logger.warning("Alternate format still has no audio stream: %s", selector)
            except yt_dlp.utils.DownloadError as exc:
                raise DownloadFailure(download_error_message(exc)) from exc
    logger.warning("Audio recovery exhausted without a verified audio stream: attempts=%s", attempts)
    return None


def extract_mp3(source: Path) -> Path:
    duration, _ = probe_media(source, audio_only=True)
    if source.suffix.lower() == ".mp3":
        return source
    directory = Path(tempfile.mkdtemp(prefix="mp3-", dir=source.parent))
    output = directory / "audio.mp3"
    run_media_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "libmp3lame",
        "-b:a", "192k", "-threads", "2", "-map_metadata", "-1",
        "-map_chapters", "-1", str(output),
    ], COMPRESSION_TIMEOUT_SECONDS)
    converted_duration, _ = probe_media(output, audio_only=True)
    if abs(converted_duration - duration) > 1:
        raise CompressionError("ملف الصوت المحوّل غير مكتمل؛ لم يتم إرساله.")
    return output


def download_media(url: str, out_dir: Path, mode: str = "video") -> tuple[Path, str | None]:
    if extract_supported_url(url) != url:
        raise DownloadFailure("الروابط المدعومة: تويتر/X وإنستغرام وتيك توك فقط.")
    url = canonical_media_url(url)
    try:
        path, info = download_media_attempt(url, out_dir, mode)
        try:
            validate_download_audio(path, mode)
        except MissingAudioStream:
            logger.warning("Downloaded format has no audio stream: mode=%s format=%s",
                           mode, info.get("format_id"))
            recovered = recover_download_audio(url, out_dir, mode, info)
            if recovered:
                path, info = recovered
            elif mode == "audio":
                raise DownloadFailure(NO_AUDIO_MESSAGE)
            # A genuinely silent video is allowed, with an explicit warning caption.
        return (extract_mp3(path) if mode == "audio" else path), info.get("id")
    except yt_dlp.utils.DownloadError as exc:
        logger.warning("Download failed: %s", exc)
        if mode == "audio" and "requested format is not available" in str(exc).lower():
            recovered = recover_download_audio(url, out_dir, mode, {"formats": []})
            if recovered:
                path, info = recovered
                return extract_mp3(path), info.get("id")
            raise DownloadFailure(NO_AUDIO_MESSAGE) from exc
        raise DownloadFailure(download_error_message(exc)) from exc


def photo_post_url(url: str) -> str:
    """Accept single posts only, never crawl an entire profile or search page."""
    if extract_supported_url(url) != url:
        raise DownloadFailure("الروابط المدعومة: تويتر/X وإنستغرام وتيك توك فقط.")
    parsed = urlsplit(url)
    host, path = (parsed.hostname or "").lower(), parsed.path
    if host in ("instagram.com", "www.instagram.com"):
        if re.fullmatch(r"/(?:reels?|tv)/[A-Za-z0-9_-]+/?", path):
            raise DownloadFailure(
                "هذا رابط ريل/فيديو، وليس منشور صور. اختر 🎬 فيديو أو 🎵 صوت MP3."
            )
        match = re.fullmatch(r"/(p)/([A-Za-z0-9_-]+)/?", path)
        if match:
            return f"https://www.instagram.com/{match[1]}/{match[2]}/"
    elif host in ("x.com", "www.x.com", "mobile.x.com", "twitter.com",
                  "www.twitter.com", "mobile.twitter.com"):
        match = re.fullmatch(r"/([A-Za-z0-9_]+|i/web)/status/(\d+)(?:/(?:photo|video)/\d+)?/?", path)
        if match:
            return f"https://x.com/{match[1]}/status/{match[2]}"
    elif host in ("tiktok.com", "www.tiktok.com", "m.tiktok.com"):
        if re.fullmatch(r"/(?:@[\w.-]+|share)/(?:photo|video)/\d+/?", path):
            return f"https://www.tiktok.com{path}"
        if re.fullmatch(r"/t/[A-Za-z0-9]+/?", path):
            return f"https://www.tiktok.com{path}"
    elif host in ("vm.tiktok.com", "vt.tiktok.com"):
        if re.fullmatch(r"/[A-Za-z0-9]+/?", path):
            return f"https://{host}{path}"
    raise DownloadFailure("لتحميل الصور أرسل رابط المنشور نفسه، وليس رابط الحساب أو البحث.")


def build_gallery_command(url: str, out_dir: Path) -> list[str]:
    return [
        sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-input",
        "--no-colors", "--no-postprocessors", "--warning", "--cache-file", ":memory:",
        "--directory", str(out_dir.resolve()), "--filename", "{num:03}.{extension}",
        # Never sleep for minutes retrying blocked requests while holding the media queue.
        "--range", f"1-{MAX_PHOTOS}", "--post-range", "1", "--retries", "0",
        "--http-timeout", str(PHOTO_HTTP_TIMEOUT_SECONDS), "--filesize-max", "49M",
        "--filter", f"extension.lower() in {PHOTO_EXTENSIONS!r}",
        "--whitelist", "instagram:post,twitter:tweet,tiktok:post",
        "-o", "extractor.cookies=null", "-o", "extractor.cookies-update=false",
        "-o", "extractor.sleep-429=0", "-o", "downloader.http.sleep-429=0",
        "-o", "extractor.netrc=false", "-o", "extractor.videos=false",
        "-o", "extractor.audio=false", "-o", "extractor.previews=false",
        "-o", "extractor.instagram.static-videos=false",
        "-o", "extractor.twitter.conversations=false", "-o", "extractor.twitter.quoted=false",
        "-o", "extractor.tiktok.covers=false", url,
    ]


def is_instagram_image_url(url: str) -> bool:
    """Only accept HTTPS image links returned by Instagram's own CDN."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and any(host == domain or host.endswith("." + domain)
                        for domain in ("cdninstagram.com", "fbcdn.net")))
    except (ValueError, TypeError):
        return False


def select_instagram_photo_urls(product: dict) -> list[str]:
    """Select real photo items, never video thumbnails, in carousel order."""
    if isinstance(product, list):
        product = product[0] if product else None
    if not isinstance(product, dict):
        raise DownloadFailure("إنستغرام لم يُرجع بيانات صور صالحة لهذا المنشور.")
    items = product.get("carousel_media") if product.get("media_type") == 8 else [product]
    urls = []
    for item in items or []:
        if (not isinstance(item, dict) or item.get("media_type") != 1
                or item.get("video_versions") or item.get("video_dash_manifest")):
            continue
        candidates = (item.get("image_versions2") or {}).get("candidates") or []
        candidates = [c for c in candidates if isinstance(c, dict)
                      and isinstance(c.get("url"), str) and is_instagram_image_url(c["url"])]
        if not candidates:
            raise DownloadFailure("تعذّر الحصول على رابط صورة صالح من إنستغرام.")
        # Some logged-out responses omit dimensions; retain their first candidate.
        best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
        urls.append(best["url"])
        if len(urls) == MAX_PHOTOS:
            break
    if not urls:
        raise DownloadFailure("لا توجد صور قابلة للتنزيل في هذا المنشور؛ أغلفة الفيديو ليست صور منشور.")
    return urls


def extract_instagram_photo_urls(url: str) -> list[str]:
    # Use the pinned extractor's ungated/logged-out post response. This local
    # subclass intercepts photo metadata without changing the video/audio extractor.
    from yt_dlp.extractor.instagram import InstagramIE

    class PhotoMetadata(Exception):
        def __init__(self, product):
            self.product = product

    class PhotoExtractor(InstagramIE):
        def _extract_product(self, product_info, *args, **kwargs):
            raise PhotoMetadata(product_info)

    options = {
        "quiet": True, "skip_download": True, "logger": YTDLPLogger(),
        "socket_timeout": PHOTO_HTTP_TIMEOUT_SECONDS, "retries": 0,
        "extractor_retries": 0, "cookiefile": None, "cookiesfrombrowser": None,
        "usenetrc": False,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            PhotoExtractor(ydl).extract(url)
    except PhotoMetadata as metadata:
        return select_instagram_photo_urls(metadata.product)
    except yt_dlp.utils.ExtractorError as exc:
        raise DownloadFailure(download_error_message(exc)) from exc
    raise DownloadFailure("إنستغرام لم يُرجع صور هذا المنشور.")


def open_instagram_photo(url: str):
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    if not is_instagram_image_url(url):
        raise DownloadFailure("رابط الصورة ليس من خادم صور إنستغرام المسموح.")
    # No account cookies, automatic retries, or redirects to arbitrary hosts.
    return build_opener(NoRedirect()).open(
        Request(url, headers={"Referer": "https://www.instagram.com/"}),
        timeout=PHOTO_HTTP_TIMEOUT_SECONDS,
    )


def download_instagram_photos(url: str, out_dir: Path):
    url = photo_post_url(url)
    if (urlsplit(url).hostname or "").lower() not in ("instagram.com", "www.instagram.com"):
        raise DownloadFailure("أداة صور إنستغرام تقبل منشور إنستغرام فقط.")
    urls = extract_instagram_photo_urls(url)
    extensions = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "AVIF": "avif", "GIF": "gif"}
    for number, image_url in enumerate(urls, 1):
        partial = out_dir / f"{number:03}.image.part"
        try:
            with open_instagram_photo(image_url) as response, partial.open("wb") as output:
                declared = response.headers.get("Content-Length", "")
                if declared.isdecimal() and int(declared) >= MAX_FILE_SIZE_BYTES:
                    raise DownloadFailure("حجم الصورة يتجاوز الحد المسموح لإرسالها.")
                size = 0
                while chunk := response.read(64 * 1024):
                    size += len(chunk)
                    if size >= MAX_FILE_SIZE_BYTES:
                        raise DownloadFailure("حجم الصورة يتجاوز الحد المسموح لإرسالها.")
                    output.write(chunk)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(partial) as image:
                    extension = extensions.get(image.format)
                    if not extension:
                        raise DownloadFailure("صيغة الصورة غير مدعومة.")
                    image.verify()
            partial.replace(out_dir / f"{number:03}.{extension}")
        except DownloadFailure:
            raise
        except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise DownloadFailure(download_error_message(exc)) from exc
        finally:
            partial.unlink(missing_ok=True)
    logger.info("Instagram photos downloaded successfully: count=%s", len(urls))


def instagram_photo_worker_main(url: str, out_dir: Path) -> int:
    configure_logging()
    try:
        download_instagram_photos(url, out_dir)
    except DownloadFailure as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True))
        return 1
    except Exception as exc:
        logger.warning("Instagram photo worker failed: %s", type(exc).__name__)
        print(json.dumps({"error": "تعذّر تنزيل صور المنشور. جرّب لاحقًا."}, ensure_ascii=True))
        return 1
    return 0


def download_photos(url: str, out_dir: Path) -> list[Path]:
    url = photo_post_url(url)
    instagram = (urlsplit(url).hostname or "").lower() in ("instagram.com", "www.instagram.com")
    command = ([sys.executable, str(Path(__file__).resolve()), "--instagram-photos",
                url, str(out_dir.resolve())] if instagram else build_gallery_command(url, out_dir))
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PHOTO_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
            env={key: value for key, value in os.environ.items() if key != "BOT_TOKEN"},
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadFailure(
            "تجاوز تنزيل الصور مهلة دقيقة، فتم إيقافه حتى لا تتعطل الطلبات التالية. حاول لاحقًا."
        ) from exc
    except OSError as exc:
        raise DownloadFailure("تعذّر تشغيل أداة تحميل الصور على الخادم.") from exc
    if result.returncode:
        logger.warning("Photo download failed: %s", result.stderr[-2000:])
        if instagram:
            try:
                error = json.loads(getattr(result, "stdout", ""))["error"]
                if isinstance(error, str) and error and len(error) < 600:
                    raise DownloadFailure(error)
            except (ValueError, KeyError, TypeError):
                pass
        raise DownloadFailure(download_error_message(Exception(result.stderr)))
    photos = []
    for path in sorted(out_dir.iterdir()):
        if path.suffix.lower().lstrip(".") not in PHOTO_EXTENSIONS:
            continue
        if (path.is_symlink() or path.resolve().parent != out_dir.resolve()
                or not path.is_file() or not 0 < path.stat().st_size < MAX_FILE_SIZE_BYTES):
            raise DownloadFailure("لم ينتج التنزيل صورًا مكتملة بحجم مناسب.")
        photos.append(path)
    if not photos:
        raise DownloadFailure("لا توجد صور قابلة للتنزيل في هذا المنشور، أو أنها غير متاحة أو كبيرة جدًا.")
    if len(photos) > MAX_PHOTOS:
        raise DownloadFailure("تجاوز التنزيل الحد المسموح للصور.")
    return photos


def prepare_photo(source: Path) -> tuple[Path, bool]:
    """Normalize a preview for sendPhoto; preserve panoramas/large images as documents."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                width, height = image.size
                if width * height > 25_000_000 or max(width, height) > 20 * min(width, height):
                    image.verify()
                    return source, True
                image.draft("RGB", (3000, 3000))
                image = ImageOps.exif_transpose(image)
                image.thumbnail((3000, 3000))
                rgb = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    rgba = image.convert("RGBA")
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    rgb.paste(image.convert("RGB"))
                output_dir = source.parent / "prepared"
                output_dir.mkdir(exist_ok=True)
                output = output_dir / (source.stem + ".jpg")
                for quality in (90, 75, 60):
                    rgb.save(output, "JPEG", quality=quality)
                    if 0 < output.stat().st_size < 9_500_000:
                        return output, False
                return source, True
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise DownloadFailure("تعذّر قراءة إحدى الصور؛ لم يتم إرسال ملف غير صالح.") from exc


class CompressionError(RuntimeError):
    """رسالة آمنة يمكن عرضها للمستخدم عند فشل الضغط."""


class MissingAudioStream(CompressionError):
    pass


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
        raise CompressionError("انتهت مهلة معالجة الملف. جرّب مقطعًا أقصر.") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("Media command failed: %s", exc)
        raise CompressionError("تعذّر معالجة الملف بواسطة FFmpeg.") from exc


def probe_media(path: Path, *, audio_only: bool = False) -> tuple[float, bool]:
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
        required = "audio" if audio_only else "video"
        if not any(s["codec_type"] == required for s in streams):
            if audio_only and any(s["codec_type"] == "video" for s in streams):
                raise MissingAudioStream(NO_AUDIO_MESSAGE)
            raise ValueError("Missing required stream")
        return duration, any(s["codec_type"] == "audio" for s in streams)
    except (ValueError, KeyError, TypeError) as exc:
        raise CompressionError("تعذّر قراءة مدة الملف أو مسار الصوت/الصورة.") from exc


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


def compress_audio(source: Path) -> Path:
    require_media_tools()
    duration, _ = probe_media(source, audio_only=True)
    encode_dir = Path(tempfile.mkdtemp(prefix="audio-", dir=source.parent))
    output = encode_dir / "audio.mp3"
    target = COMPRESSION_TARGET_BYTES
    rates = (192_000, 160_000, 128_000, 112_000, 96_000, 80_000, 64_000,
             56_000, 48_000, 40_000, 32_000)
    for _ in range(2):
        bitrate = next((rate for rate in rates if rate <= target * 8 / duration), None)
        if bitrate is None:
            raise CompressionError("الصوت طويل جدًا لإرساله تحت 50MB دون تقطيعه.")
        run_media_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "libmp3lame",
            "-b:a", str(bitrate), "-ac", "2", "-map_metadata", "-1",
            "-map_chapters", "-1", str(output),
        ], COMPRESSION_TIMEOUT_SECONDS)
        size = output.stat().st_size if output.is_file() else 0
        if not size:
            raise CompressionError("لم تنتج المعالجة ملف صوت صالحًا.")
        if size < MAX_FILE_SIZE_BYTES:
            output_duration, _ = probe_media(output, audio_only=True)
            if abs(output_duration - duration) > max(1.0, duration * 0.01):
                raise CompressionError("ملف الصوت غير مكتمل؛ لم يتم إرساله.")
            return output
        target = min(int(target * COMPRESSION_TARGET_BYTES / size * 0.9),
                     int(bitrate * duration / 8 * 0.9))
    raise CompressionError("تعذّر تقليل حجم الصوت إلى أقل من 50MB.")


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
            "أهلًا! ابعت رابط منشور من تويتر/X أو إنستغرام أو تيك توك 👋\n"
            "ثم اختر: 🎬 فيديو، 🎵 صوت MP3، أو 🖼 صور.\n"
            "أضغط الفيديو أو الصوت الكبير تلقائيًا تحت 50MB."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "أرسل رابط منشور من تويتر/X أو إنستغرام أو تيك توك واختر نوع التحميل.\n"
            "🎵 الصوت: استخراج MP3 من رابط الفيديو، وليس رابط اسم أغنية.\n"
            "🖼 الصور: صور المنشور الأصلي، حتى 20 صورة؛ ليست لقطات من الفيديو.\n"
            "الأوامر المباشرة: /video أو /audio أو /photos ثم الرابط.\n"
            "التحميل بدون كوكيز حساب؛ المحتوى الذي تطلب المنصة تسجيل دخول له غير مدعوم.\n"
            "إذا احتوى المنشور عدة فيديوهات، يُحمّل أول فيديو فقط.\n"
            "الخيارات صالحة 15 دقيقة وتُلغى عند إعادة تشغيل البوت؛ أعد إرسال الرابط عند الحاجة.\n"
            "معالجة الملفات الكبيرة قد تستغرق عدة دقائق."
        )


async def edit_status(status, text):
    if status:
        try:
            await status.edit_text(text)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Could not update status message", exc_info=True)
        except Exception:
            logger.warning("Could not update status message", exc_info=True)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    url = extract_supported_url(update.message.text or "")
    if not url:
        await update.message.reply_text(
            "أرسل رابط منشور من تويتر/X أو إنستغرام أو تيك توك فقط."
        )
        return
    if not update.effective_user:
        return
    choices = context.user_data.setdefault("media_choices", {})
    now = time.monotonic()
    for key, item in list(choices.items()):
        if now - item["created"] >= CHOICE_TTL_SECONDS:
            choices.pop(key, None)
    while len(choices) >= 10:
        choices.pop(next(iter(choices)))
    request_id = secrets.token_hex(6)
    choices[request_id] = dict(url=url, created=now, chat_id=update.effective_chat.id,
                               user_id=update.effective_user.id)
    buttons = [
        InlineKeyboardButton("🎬 فيديو", callback_data=f"media:video:{request_id}"),
        InlineKeyboardButton("🎵 صوت MP3", callback_data=f"media:audio:{request_id}"),
    ]
    if not is_instagram_reel(url):
        buttons.append(InlineKeyboardButton("🖼 صور", callback_data=f"media:photos:{request_id}"))
    keyboard = InlineKeyboardMarkup([buttons])
    prompt = ("هذا رابط ريل/فيديو؛ اختر الفيديو أو الصوت. الصور تحتاج رابط منشور صور."
              if is_instagram_reel(url) else "شو بدك تحمّل من هذا الرابط؟")
    try:
        await update.message.reply_text(prompt, reply_markup=keyboard)
    except Exception:
        choices.pop(request_id, None)
        raise


async def media_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not isinstance(query.data, str):
        return
    match = re.fullmatch(r"media:(video|audio|photos):([0-9a-f]{12})", query.data)
    choices = context.user_data.get("media_choices", {})
    item = choices.get(match[2]) if match else None
    if (not item or not update.effective_chat or not update.effective_user
            or item["user_id"] != update.effective_user.id
            or item["chat_id"] != update.effective_chat.id
            or time.monotonic() - item["created"] >= CHOICE_TTL_SECONDS):
        await query.answer("الخيارات انتهت أو ليست لك. أعد إرسال الرابط.", show_alert=True)
        return
    # Consume before the first await: duplicate clicks cannot download twice.
    choices.pop(match[2])
    try:
        await query.answer("تم اختيار " + MEDIA_MODES[match[1]])
    except Exception:
        # Telegram can expire the callback while Render wakes from idle.
        logger.warning("Could not acknowledge media choice")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.warning("Could not remove media buttons")
    await process_media(update, context, item["url"], match[1])


async def media_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    mode = (update.message.text or "").split()[0].split("@")[0].lstrip("/").lower()
    url = extract_supported_url(" ".join(context.args or []))
    if mode not in MEDIA_MODES or not url:
        await update.message.reply_text("اكتب /video أو /audio أو /photos ثم رابط المنشور المدعوم.")
        return
    await process_media(update, context, url, mode)


async def send_photos(message, paths: list[Path]):
    # Prepare every image before any upload, so corrupt downloads are not a partial success.
    prepared = [await run_media_worker(prepare_photo, path) for path in paths]
    album = []

    async def flush():
        if len(album) == 1:
            with album[0].open("rb") as photo:
                await message.reply_photo(photo=photo, caption="✅ تفضل صورتك", **UPLOAD_TIMEOUTS)
        elif album:
            await message.reply_media_group(
                media=[InputMediaPhoto(path, caption="✅ تفضل صور المنشور" if i == 0 else None)
                       for i, path in enumerate(album)], **UPLOAD_TIMEOUTS,
            )
        album.clear()

    for path, as_document in prepared:
        if as_document:
            await flush()
            with path.open("rb") as document:
                await message.reply_document(
                    document=document, caption="🖼 الصورة الأصلية كملف بسبب أبعادها أو حجمها",
                    **UPLOAD_TIMEOUTS,
                )
        else:
            album.append(path)
            if len(album) == 10:
                await flush()
    await flush()


async def process_media(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, mode: str):
    message = update.effective_message
    if not message or mode not in MEDIA_MODES:
        return
    if mode == "photos":
        # Reject reel/profile requests BEFORE queue admission, even if another job is busy.
        try:
            url = photo_post_url(url)
        except DownloadFailure as exc:
            await message.reply_text(f"❌ {exc}")
            return
    state = context.bot_data
    # One expensive media worker on the Free instance, with at most three waiting requests.
    if state.get("media_jobs", 0) >= 4:
        await message.reply_text("البوت مشغول حاليًا. أعد إرسال الرابط بعد قليل.")
        return
    state["media_jobs"] = state.get("media_jobs", 0) + 1
    semaphore = state.setdefault("media_semaphore", asyncio.Semaphore(1))
    status = None
    request_dir = None
    try:
        status = await message.reply_text(
            "⏳ طلبك بانتظار انتهاء التحميل السابق..." if semaphore.locked()
            else f"⏳ جاري تنزيل {MEDIA_MODES[mode]}..."
        )
        async with semaphore:
            await edit_status(status, f"⏳ جاري تنزيل {MEDIA_MODES[mode]}...")
            request_dir = Path(tempfile.mkdtemp(prefix="request-", dir=DOWNLOAD_DIR))
            if mode == "photos":
                paths = await run_media_worker(download_photos, url, request_dir)
                await edit_status(status, f"📤 جاري تجهيز وإرسال {len(paths)} صورة (حتى {MAX_PHOTOS})...")
                await send_photos(message, paths)
            else:
                if mode == "audio":
                    file_path, _ = await run_media_worker(download_media, url, request_dir, "audio")
                    # A renamed/invalid file must not be sent as a playable audio track.
                    await run_media_worker(lambda path: probe_media(path, audio_only=True), file_path)
                else:
                    file_path, _ = await download_media_async(url, request_dir)
                if file_path.stat().st_size >= MAX_FILE_SIZE_BYTES:
                    await edit_status(status, f"🗜️ جاري ضغط {MEDIA_MODES[mode]} تحت 50MB؛ قد يستغرق دقائق...")
                    file_path = await run_media_worker(
                        compress_audio if mode == "audio" else compress_video, file_path,
                    )
                if not 0 < file_path.stat().st_size < MAX_FILE_SIZE_BYTES:
                    raise CompressionError("حجم الملف غير مناسب للرفع؛ لم يتم إرساله.")
                video_has_audio = True
                if mode == "video":
                    _, video_has_audio = await run_media_worker(probe_media, file_path)
                await edit_status(status, f"📤 جاري رفع {MEDIA_MODES[mode]} ({file_path.stat().st_size / 1_000_000:.1f}MB)...")
                try:
                    await context.bot.send_chat_action(
                        chat_id=update.effective_chat.id,
                        action=ChatAction.UPLOAD_VOICE if mode == "audio" else ChatAction.UPLOAD_VIDEO,
                    )
                except Exception:
                    logger.warning("Could not send chat action")
                with file_path.open("rb") as media_file:
                    if mode == "audio":
                        await message.reply_audio(audio=media_file, caption="✅ تفضل الصوت بصيغة MP3",
                                                  **UPLOAD_TIMEOUTS)
                    else:
                        caption = "✅ تفضل فيديوك" if video_has_audio else (
                            "⚠️ هذا الفيديو بلا مسار صوت في النسخ المتاحة من المصدر؛ "
                            "تعذّر استرجاع الصوت."
                        )
                        await message.reply_video(video=media_file, caption=caption,
                                                  supports_streaming=True, **UPLOAD_TIMEOUTS)
        if mode == "video":
            logger.info("Media sent successfully: mode=video has_audio=%s", video_has_audio)
        else:
            logger.info("Media sent successfully: mode=%s", mode)
        try:
            await status.delete()
        except Exception:
            pass
    except (DownloadFailure, CompressionError) as exc:
        await edit_status(status, f"❌ {exc}")
    except Exception:
        logger.exception("Download/upload request failed")
        text = "❌ تعذّر تجهيز الملفات أو إرسالها بالكامل. حاول لاحقًا."
        if mode == "photos":
            text += " قد تكون بعض الصور وصلت بالفعل."
        await edit_status(status, text)
    finally:
        state["media_jobs"] -= 1
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
                    allowed_updates=["message", "callback_query"],
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
    application.add_handler(CommandHandler(["video", "audio", "photos"], media_command, block=False))
    application.add_handler(CallbackQueryHandler(media_choice, pattern=r"^media:", block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.add_error_handler(on_error)
    return application


def main():
    configure_logging()
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير موجود.")
    require_media_tools()
    logger.info("Runtime: yt-dlp=%s; curl-cffi=%s; gallery-dl=%s",
                version("yt-dlp"), version("curl-cffi"), version("gallery-dl"))
    logger.info("Instagram strategy: complete MP4 first; verify actual audio streams")
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
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--instagram-photos"]:
        if len(sys.argv) != 4:
            raise SystemExit(2)
        raise SystemExit(instagram_photo_worker_main(sys.argv[2], Path(sys.argv[3])))
    main()
