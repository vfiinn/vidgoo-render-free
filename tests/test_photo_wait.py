"""Regression tests for blocked photo downloads holding up the media queue."""
import asyncio
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


POST = "https://www.instagram.com/p/example/"


class FastPhotoTests(unittest.TestCase):
    def test_reels_tv_and_tracking_links_never_launch_downloader(self):
        for path in ("reel/example/", "reels/example", "tv/example/?igsh=tracking"):
            with self.subTest(path=path), patch.object(bot.subprocess, "run") as run:
                with self.assertRaisesRegex(bot.DownloadFailure, "اختر.*فيديو.*MP3"):
                    bot.download_photos("https://www.instagram.com/" + path, Path("."))
                run.assert_not_called()

    def test_photo_timeout_is_bounded_and_has_clear_message(self):
        self.assertEqual(bot.PHOTO_TIMEOUT_SECONDS, 60)
        self.assertEqual(bot.PHOTO_HTTP_TIMEOUT_SECONDS, 12)
        with patch.object(bot.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 60)) as run:
            with self.assertRaisesRegex(bot.DownloadFailure, "مهلة دقيقة"):
                bot.download_photos(POST, Path("."))
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--retries") + 1], "0")

    def test_empty_instagram_response_does_not_blame_telegram_upload(self):
        text = bot.download_error_message(Exception("Instagram sent an empty media response"))
        self.assertIn("لم يُرجع بيانات", text)
        self.assertIn("قد يكون", text)
        self.assertIn("بدون كوكيز", text)


class FastQueueTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=status))
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=1))
        context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_chat_action=AsyncMock()))
        return update, context, status

    async def test_reel_rejected_immediately_even_with_a_full_busy_queue(self):
        update, context, _ = self.fixture()
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        context.bot_data.update(media_jobs=4, media_semaphore=semaphore)
        with patch.object(bot, "download_photos") as download, patch.object(bot.tempfile, "mkdtemp") as mkdir:
            await asyncio.wait_for(bot.process_media(
                update, context, "https://instagram.com/reel/example/", "photos"
            ), timeout=1)
            download.assert_not_called()
            mkdir.assert_not_called()
        self.assertIn("رابط ريل", update.effective_message.reply_text.call_args.args[0])
        self.assertEqual(context.bot_data["media_jobs"], 4)
        self.assertTrue(semaphore.locked())
        semaphore.release()

    async def test_failed_photo_releases_slot_and_removes_download_directory(self):
        for error in (bot.DownloadFailure("429 Too Many Requests"), bot.DownloadFailure("مهلة دقيقة")):
            update, context, status = self.fixture()
            with tempfile.TemporaryDirectory() as folder, patch.object(
                bot, "DOWNLOAD_DIR", Path(folder)
            ), patch.object(bot, "download_photos", side_effect=error):
                await bot.process_media(update, context, POST, "photos")
                self.assertEqual(list(Path(folder).iterdir()), [])
            self.assertEqual(context.bot_data["media_jobs"], 0)
            self.assertFalse(context.bot_data["media_semaphore"].locked())
            self.assertIn(str(error), status.edit_text.call_args.args[0])

    async def test_unchanged_status_no_long_traceback_but_other_api_errors_logged(self):
        for error, logged in ((bot.BadRequest("Message is not modified"), False),
                              (bot.BadRequest("Message to edit not found"), True)):
            status = SimpleNamespace(edit_text=AsyncMock(side_effect=error))
            with patch.object(bot.logger, "warning") as warning:
                await bot.edit_status(status, "same content")
            self.assertEqual(warning.called, logged)


class BlockedHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.attempts.append(self.path)
        body = b"fixture blocked response"
        self.send_response(self.server.status_code)
        self.send_header("Retry-After", "120")
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealRateLimitTests(unittest.TestCase):
    def test_gallery_metadata_and_download_do_not_retry_429_or_403(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), BlockedHTTPHandler)
        server.status_code = 429
        server.attempts = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for status in (429, 403):
                for mode in ("metadata-error", "download-error"):
                    with self.subTest(status=status, mode=mode), tempfile.TemporaryDirectory() as folder:
                        server.status_code = status
                        server.attempts.clear()
                        started = time.monotonic()
                        result = subprocess.run([
                            sys.executable, str(Path(__file__).with_name("gallery_fixture.py")),
                            folder, f"http://127.0.0.1:{server.server_port}", mode,
                        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn(str(status), result.stderr)
                        self.assertEqual(server.attempts, ["/blocked"])
                        self.assertLess(time.monotonic() - started, 8)
                        self.assertFalse(any(p.suffix == ".png" for p in Path(folder).iterdir()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
