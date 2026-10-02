import asyncio
import io
import logging
import tempfile
import unittest
import subprocess
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import bot


class BotTests(unittest.TestCase):
    def test_allowed_platforms(self):
        for host in ("x.com", "twitter.com", "www.instagram.com", "tiktok.com", "vt.tiktok.com", "vm.tiktok.com"):
            url = f"https://{host}/video"
            self.assertEqual(bot.extract_supported_url(f"شوف {url}،"), url)

    def test_removed_and_fake_domains(self):
        for url in ("https://youtube.com/watch?v=1", "https://youtu.be/1",
                    "https://facebook.com/video", "https://fb.watch/1",
                    "https://box.com/video", "https://x.com.evil.test/video",
                    "https://x.com@evil.test/video", "https://evil.test/?next=https://x.com/1",
                    "https://[broken", "https://x.com:bad/video"):
            with self.subTest(url=url):
                self.assertIsNone(bot.extract_supported_url(url))

    def test_no_account_cookies_or_forced_clients(self):
        options = bot.build_ydl_options(Path("."), "https://tiktok.com/video")
        self.assertIsNone(options["cookiefile"])
        self.assertIsNone(options["cookiesfrombrowser"])
        self.assertFalse(options["usenetrc"])
        self.assertNotIn("user_agent", options)
        self.assertNotIn("extractor_args", options)
        self.assertEqual(options["playlist_items"], "1")

    def test_redaction_covers_formatted_arguments_and_tracebacks(self):
        token = "123456789:" + "A" * 35
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(bot.SafeFormatter("%(message)s"))
        log = logging.getLogger("redaction-test")
        log.handlers = [handler]
        log.propagate = False
        log.setLevel(logging.INFO)
        with patch.object(bot, "BOT_TOKEN", token):
            log.info("POST https://api.telegram.org/bot%s/getUpdates", token)
            try:
                raise RuntimeError("request " + token)
            except RuntimeError:
                log.exception("request failed")
        self.assertNotIn(token, output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())

    def test_error_classification(self):
        cases = [
            ("[twitter] Suspended", "موقوف"),
            ("Sign in to confirm", "تسجيل دخول"),
            ("login required", "تسجيل دخول"),
            ("429 Too many requests", "عدد الطلبات"),
            ("Unexpected response from webpage request", "رد المنصة"),
            ("Connection timed out", "الاتصال"),
            ("404 Not found", "غير متاح"),
            ("unknown error", "فشل تنزيل"),
        ]
        for error, expected in cases:
            self.assertIn(expected, bot.download_error_message(Exception(error)))

    def test_final_file_not_partial_or_outside_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "video.part").touch()
            (root / "video.f137.mp4").touch()
            (root / "video.fdash-182222.mp4").touch()
            (root / "video.fhls-1200.mp4").touch()
            with self.assertRaises(bot.DownloadFailure):
                bot.find_downloaded_file({}, root, str(root / "video.part"))
            (root / "video.mp4").write_bytes(b"complete")
            self.assertEqual(
                bot.find_downloaded_file({}, root, str(root / "video.webm")),
                (root / "video.mp4").resolve(),
            )

    def test_playlist_uses_downloaded_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "video.mp4"
            path.write_bytes(b"video")
            with patch.object(bot.yt_dlp, "YoutubeDL") as factory:
                downloader = factory.return_value.__enter__.return_value
                downloader.extract_info.return_value = {
                    "_type": "playlist", "entries": [None, {"id": "video"}],
                }
                downloader.prepare_filename.return_value = str(path)
                result, video_id = bot.download_media("https://instagram.com/p/example", root)
                self.assertEqual(result, path.resolve())
                self.assertEqual(video_id, "video")

    def test_removed_platform_never_downloads(self):
        with patch.object(bot.yt_dlp, "YoutubeDL") as factory:
            with self.assertRaises(bot.DownloadFailure):
                bot.download_media("https://youtube.com/watch?v=1", Path("."))
            factory.assert_not_called()

    def test_upstream_error_is_converted(self):
        with patch.object(bot.yt_dlp, "YoutubeDL") as factory:
            factory.return_value.__enter__.return_value.extract_info.side_effect = (
                bot.yt_dlp.utils.DownloadError("Suspended")
            )
            with self.assertRaisesRegex(bot.DownloadFailure, "موقوف"):
                bot.download_media("https://x.com/user/status/1", Path("."))


class CompressionTests(unittest.TestCase):
    def test_download_allows_large_files(self):
        self.assertNotIn("max_filesize", bot.build_ydl_options(Path(".")))

    def test_invalid_duration(self):
        for value in ("NaN", "inf", "0", "-1", "N/A"):
            with self.subTest(value=value), patch.object(
                bot, "run_media_command",
                return_value=SimpleNamespace(stdout=json.dumps({
                    "format": {"duration": value},
                    "streams": [{"codec_type": "video"}],
                })),
            ):
                with self.assertRaises(bot.CompressionError):
                    bot.probe_media(Path("video.mp4"))

    def test_missing_tools(self):
        with patch.object(bot.shutil, "which", return_value=None):
            with self.assertRaises(bot.CompressionError):
                bot.require_media_tools()

    def test_process_errors(self):
        for error in (OSError(), subprocess.TimeoutExpired("ffmpeg", 1),
                      subprocess.CalledProcessError(1, "ffmpeg")):
            with self.subTest(error=error), patch.object(
                bot.subprocess, "run", side_effect=error
            ):
                with self.assertRaises(bot.CompressionError):
                    bot.run_media_command(["ffmpeg"], 1)

    def test_size_retry_and_failure(self):
        for sizes, succeeds in (([50_000_000, 46_000_000], True),
                                ([50_000_000, 50_000_000], False)):
            with self.subTest(sizes=sizes), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "source.mp4"
                source.touch()
                commands = []
                remaining = iter(sizes)

                def encode(command, timeout):
                    commands.append(command)
                    if command[-1].endswith(".mp4"):
                        with open(command[-1], "wb") as file:
                            file.truncate(next(remaining))

                with patch.object(bot, "require_media_tools"), patch.object(
                    bot, "probe_media", return_value=(600.0, True)
                ), patch.object(bot, "run_media_command", side_effect=encode):
                    if succeeds:
                        self.assertLess(bot.compress_video(source).stat().st_size,
                                        bot.MAX_FILE_SIZE_BYTES)
                    else:
                        with self.assertRaises(bot.CompressionError):
                            bot.compress_video(source)
                self.assertEqual(len(commands), 4)
                rates = [int(c[c.index("-b:v") + 1]) for c in commands]
                self.assertLess(rates[2], rates[0])
                self.assertNotIn("-fs", commands[1])



class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, large=False, failure=None):
        with tempfile.TemporaryDirectory() as folder:
            status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
            message = SimpleNamespace(text="https://x.com/user/status/1",
                                      reply_text=AsyncMock(return_value=status),
                                      reply_video=AsyncMock())
            update = SimpleNamespace(message=message, effective_message=message,
                                     effective_chat=SimpleNamespace(id=1))
            context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()), bot_data={})
            sent = []

            async def download(url, directory):
                (directory / "partial.part").touch()
                if failure == "download":
                    raise bot.DownloadFailure("download failed")
                path = directory / "video.mp4"
                with path.open("wb") as file:
                    file.truncate(bot.MAX_FILE_SIZE_BYTES if large else 100)
                return path, "video"

            def compress(path):
                output = path.parent / "compressed.mp4"
                output.write_bytes(b"compressed")
                if failure == "compress":
                    raise bot.CompressionError("compression failed")
                return output

            async def upload(**kwargs):
                sent.append(Path(kwargs["video"].name).name)
                if failure == "upload":
                    raise RuntimeError("upload failed")

            message.reply_video.side_effect = upload
            with patch.object(bot, "DOWNLOAD_DIR", Path(folder)), patch.object(
                bot, "download_media_async", side_effect=download
            ), patch.object(bot, "compress_video", side_effect=compress):
                await bot.process_media(update, context, message.text, "video")
            self.assertEqual(list(Path(folder).iterdir()), [])
            self.assertEqual(sent, [] if failure in ("download", "compress")
                             else ["compressed.mp4" if large else "video.mp4"])

    async def test_small(self):
        await self.run_case()

    async def test_large(self):
        await self.run_case(large=True)

    async def test_download_failure(self):
        await self.run_case(failure="download")

    async def test_compression_failure(self):
        await self.run_case(large=True, failure="compress")

    async def test_upload_failure(self):
        await self.run_case(failure="upload")

    async def test_status_failure_does_not_abort(self):
        status = SimpleNamespace(edit_text=AsyncMock(side_effect=RuntimeError("edit failed")))
        await bot.edit_status(status, "working")

    async def test_conflict_is_handled(self):
        with patch.object(bot.logger, "error") as log:
            await bot.on_error(None, SimpleNamespace(error=bot.Conflict("another poll")))
            self.assertIn("another instance", log.call_args.args[0])

    async def test_cancellation_waits_for_worker(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def worker():
            started.set()
            release.wait(5)
            finished.set()

        task = asyncio.create_task(bot.run_media_worker(worker))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())


if __name__ == "__main__":
    unittest.main()
