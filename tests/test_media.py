"""Offline media/handler tests; Telegram calls and social-site requests are mocked."""
import asyncio
import functools
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from gallery_dl import option

import bot


URL = "https://www.instagram.com/p/example/"


class PhotoTests(unittest.TestCase):
    def test_single_posts_and_tracking_query(self):
        cases = {
            URL + "?igsh=tracking": URL,
            "https://mobile.twitter.com/user/status/123/photo/2?s=20": "https://x.com/user/status/123",
            "https://www.tiktok.com/@user/photo/123": "https://www.tiktok.com/@user/photo/123",
            "https://vt.tiktok.com/ABCDE/": "https://vt.tiktok.com/ABCDE/",
        }
        for url, expected in cases.items():
            self.assertEqual(bot.photo_post_url(url), expected)

    def test_profiles_search_and_deceptive_hosts_rejected_before_process(self):
        for url in ("https://instagram.com/user", "https://x.com/search?q=test",
                    "https://tiktok.com/@user", "https://instagram.com.evil.test/p/1",
                    "https://x.com/user/status/123/quotes", "https://vm.tiktok.com/../"):
            with self.subTest(url=url), patch.object(bot.subprocess, "run") as run:
                with self.assertRaises(bot.DownloadFailure):
                    bot.download_photos(url, Path("."))
                run.assert_not_called()

    def test_installed_gallery_cli_accepts_flags_and_filter(self):
        args = option.build_parser().parse_args(bot.build_gallery_command(URL, Path("."))[3:])
        self.assertFalse(args.config_load)
        self.assertEqual(args.cache_file, ":memory:")
        options = {(tuple(path), key): value for path, key, value in args.options}
        self.assertEqual(options[((), "file-range")], "1-20")
        self.assertEqual(options[((), "post-range")], "1")
        self.assertEqual(options[((), "retries")], 0)
        self.assertEqual(options[((), "timeout")], bot.PHOTO_HTTP_TIMEOUT_SECONDS)
        self.assertFalse(options[(("extractor",), "videos")])
        self.assertFalse(options[(("extractor",), "audio")])
        expression = options[((), "file-filter")]
        self.assertTrue(eval(expression, {}, {"extension": "JPG"}))
        self.assertFalse(eval(expression, {}, {"extension": "mp4"}))

    def test_only_completed_photos_returned_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("002.png", "001.jpg", "003.jpg.part", "video.mp4"):
                (root / name).write_bytes(b"fixture")
            with patch.object(bot.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")):
                self.assertEqual([p.name for p in bot.download_photos(URL, root)], ["001.jpg", "002.png"])

    def test_upstream_failure_does_not_send_partial_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "001.jpg").write_bytes(b"partial collection")
            with patch.object(bot.subprocess, "run", return_value=SimpleNamespace(
                returncode=4, stderr="Authentication required: authenticated cookies"
            )), self.assertRaisesRegex(bot.DownloadFailure, "تسجيل دخول"):
                bot.download_photos(URL, root)

    def test_empty_or_oversized_photo_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "001.jpg"
            path.touch()
            with patch.object(bot.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")):
                with self.assertRaises(bot.DownloadFailure):
                    bot.download_photos(URL, root)
                with path.open("wb") as stream:
                    stream.truncate(bot.MAX_FILE_SIZE_BYTES)
                with self.assertRaises(bot.DownloadFailure):
                    bot.download_photos(URL, root)

    def test_rgba_and_webp_become_telegram_jpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "001.webp"
            Image.new("RGBA", (320, 200), (255, 0, 0, 128)).save(source)
            output, document = bot.prepare_photo(source)
            self.assertFalse(document)
            with Image.open(output) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (320, 200))
            self.assertTrue(source.exists())
            self.assertLess(output.stat().st_size, 9_500_000)

    def test_panorama_sent_as_original_document(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "001.png"
            Image.new("RGB", (2100, 100), "blue").save(source)
            self.assertEqual(bot.prepare_photo(source), (source, True))

    def test_corrupt_photo_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "001.jpg"
            source.write_bytes(b"not an image")
            with self.assertRaises(bot.DownloadFailure):
                bot.prepare_photo(source)


class AudioTests(unittest.TestCase):
    def test_audio_options_and_final_mp3_only(self):
        options = bot.build_ydl_options(Path("."), URL, "audio")
        self.assertEqual(
            options["format"],
            f"bestaudio/{bot.INSTAGRAM_COMPLETE_FORMAT}/best*[acodec!=none]",
        )
        self.assertNotIn("merge_output_format", options)
        self.assertEqual(options["postprocessors"], [])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "track.m4a"
            original.write_bytes(b"unprocessed audio")
            with self.assertRaises(bot.DownloadFailure):
                bot.find_downloaded_file({}, root, str(original), "audio")
            final = root / "track.mp3"
            final.write_bytes(b"processed audio")
            self.assertEqual(bot.find_downloaded_file({}, root, str(original), "audio"), final.resolve())

    def test_probe_requires_real_audio_stream(self):
        with patch.object(bot, "run_media_command", return_value=SimpleNamespace(stdout=json.dumps({
            "format": {"duration": "12"}, "streams": [{"codec_type": "video"}],
        }))), self.assertRaises(bot.CompressionError):
            bot.probe_media(Path("silent.mp4"), audio_only=True)

    def test_compression_checks_duration_and_never_truncates(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "track.mp3"
            source.touch()
            commands = []

            def encode(command, timeout):
                commands.append(command)
                Path(command[-1]).write_bytes(b"encoded")

            with patch.object(bot, "require_media_tools"), patch.object(
                bot, "probe_media", side_effect=[(600.0, True), (10.0, True)]
            ), patch.object(bot, "run_media_command", side_effect=encode):
                with self.assertRaisesRegex(bot.CompressionError, "غير مكتمل"):
                    bot.compress_audio(source)
            self.assertNotIn("-fs", commands[0])
            self.assertNotIn("-t", commands[0])


class QuietHTTPHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class RealGalleryTests(unittest.TestCase):
    def test_real_gallery_cli_downloads_first_twenty_photos_and_no_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            Image.new("RGB", (40, 30), "blue").save(root / "photo.png")
            server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHTTPHandler, directory=folder))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                destination = root / "download"
                destination.mkdir()
                result = subprocess.run([
                    sys.executable, str(Path(__file__).with_name("gallery_fixture.py")),
                    str(destination), f"http://127.0.0.1:{server.server_port}",
                ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                files = sorted(destination.iterdir())
                self.assertEqual(len(files), 20)
                self.assertEqual([p.name for p in files], [f"{i:03}.png" for i in range(1, 21)])
                for path in files:
                    self.assertEqual(path.read_bytes(), (root / "photo.png").read_bytes())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe required")
class RealAudioTests(unittest.TestCase):
    def test_real_http_download_mp3_extraction_and_full_length_compression(self):
        # Real FFmpeg + yt-dlp with a local media server, no social-site dependency.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "fixture.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                "color=c=blue:s=160x120:d=8", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=8", "-c:v", "libx264", "-c:a", "aac",
                "-shortest", str(source),
            ], check=True, capture_output=True, timeout=30)
            server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHTTPHandler, directory=folder))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                out_dir = root / "download"
                out_dir.mkdir()
                url = f"http://127.0.0.1:{server.server_port}/fixture.mp4"
                # Explicit test-only allowlist bypass; production accepts only social domains.
                with patch.object(bot, "extract_supported_url", side_effect=lambda value: value):
                    audio, _ = bot.download_media(url, out_dir, "audio")
                self.assertEqual(audio.suffix, ".mp3")
                duration, has_audio = bot.probe_media(audio, audio_only=True)
                self.assertTrue(has_audio)
                self.assertAlmostEqual(duration, 8, delta=0.2)
                with self.assertRaises(bot.CompressionError):
                    bot.probe_media(audio)
                with patch.object(bot, "MAX_FILE_SIZE_BYTES", 50_000), patch.object(
                    bot, "COMPRESSION_TARGET_BYTES", 40_000
                ):
                    compressed = bot.compress_audio(audio)
                self.assertLess(compressed.stat().st_size, 50_000)
                self.assertAlmostEqual(bot.probe_media(compressed, audio_only=True)[0], duration, delta=0.2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class ChoiceTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, user=1, chat=1):
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        message = SimpleNamespace(text=URL, reply_text=AsyncMock(return_value=status),
                                  reply_audio=AsyncMock(), reply_video=AsyncMock(),
                                  reply_photo=AsyncMock(), reply_media_group=AsyncMock(),
                                  reply_document=AsyncMock())
        query = SimpleNamespace(data="", answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        update = SimpleNamespace(message=message, effective_message=message, callback_query=query,
                                 effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=chat))
        context = SimpleNamespace(user_data={}, bot_data={}, args=[],
                                  bot=SimpleNamespace(send_chat_action=AsyncMock()))
        return update, context, status

    async def test_link_shows_all_choices_without_downloading(self):
        update, context, _ = self.fixture()
        with patch.object(bot, "download_media") as download:
            await bot.handle_link(update, context)
        download.assert_not_called()
        buttons = update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual([b.callback_data.split(":")[1] for b in buttons], ["video", "audio", "photos"])
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for b in buttons))

    async def test_callback_acknowledged_and_single_use(self):
        update, context, _ = self.fixture()
        await bot.handle_link(update, context)
        key = next(iter(context.user_data["media_choices"]))
        update.callback_query.data = f"media:audio:{key}"
        calls = []

        async def process(*args):
            self.assertTrue(update.callback_query.answer.await_count)
            calls.append(args[-1])

        with patch.object(bot, "process_media", side_effect=process):
            await bot.media_choice(update, context)
            await bot.media_choice(update, context)
        self.assertEqual(calls, ["audio"])
        self.assertTrue(update.callback_query.answer.call_args.kwargs["show_alert"])

    async def test_other_user_chat_and_expired_buttons_rejected(self):
        for field in ("user", "chat", "expired", "restart"):
            update, context, _ = self.fixture()
            await bot.handle_link(update, context)
            key = next(iter(context.user_data["media_choices"]))
            update.callback_query.data = f"media:photos:{key}"
            if field == "user":
                update.effective_user.id = 2
            elif field == "chat":
                update.effective_chat.id = 2
            elif field == "restart":
                context.user_data.clear()
            else:
                context.user_data["media_choices"][key]["created"] = time.monotonic() - bot.CHOICE_TTL_SECONDS
            with patch.object(bot, "process_media", new_callable=AsyncMock) as process:
                await bot.media_choice(update, context)
                process.assert_not_called()
            self.assertTrue(update.callback_query.answer.call_args.kwargs["show_alert"])

    async def test_choice_storage_bounded_and_stale_choices_pruned(self):
        update, context, _ = self.fixture()
        for _ in range(12):
            await bot.handle_link(update, context)
        self.assertEqual(len(context.user_data["media_choices"]), 10)
        for item in context.user_data["media_choices"].values():
            item["created"] = time.monotonic() - bot.CHOICE_TTL_SECONDS
        await bot.handle_link(update, context)
        self.assertEqual(len(context.user_data["media_choices"]), 1)

    async def test_direct_audio_command(self):
        update, context, _ = self.fixture()
        update.message.text = "/audio@fixture_bot " + URL
        context.args = [URL]
        with patch.object(bot, "process_media", new_callable=AsyncMock) as process:
            await bot.media_command(update, context)
            process.assert_awaited_once_with(update, context, URL, "audio")

    async def test_audio_sent_as_audio_and_cleaned_on_upload_failure(self):
        for failed in (False, True):
            update, context, status = self.fixture()
            with tempfile.TemporaryDirectory() as folder:
                def download(url, out_dir, mode):
                    self.assertEqual(mode, "audio")
                    path = out_dir / "track.mp3"
                    path.write_bytes(b"test audio")
                    return path, "track"
                if failed:
                    update.message.reply_audio.side_effect = RuntimeError("test upload failure")
                with patch.object(bot, "DOWNLOAD_DIR", Path(folder)), patch.object(
                    bot, "download_media", side_effect=download
                ), patch.object(bot, "probe_media", return_value=(12.0, True)):
                    await bot.process_media(update, context, URL, "audio")
                update.message.reply_audio.assert_awaited_once()
                update.message.reply_video.assert_not_called()
                self.assertEqual(list(Path(folder).iterdir()), [])
                self.assertEqual(context.bot_data["media_jobs"], 0)
                if failed:
                    status.delete.assert_not_called()

    async def test_twenty_photos_sent_as_two_albums_and_cleaned(self):
        update, context, _ = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            def download(url, out_dir):
                paths = []
                for i in range(20):
                    path = out_dir / f"{i:03}.png"
                    Image.new("RGB", (20, 20), "red").save(path)
                    paths.append(path)
                return paths
            with patch.object(bot, "DOWNLOAD_DIR", Path(folder)), patch.object(
                bot, "download_photos", side_effect=download
            ):
                await bot.process_media(update, context, URL, "photos")
            self.assertEqual(update.message.reply_media_group.await_count, 2)
            self.assertEqual([len(c.kwargs["media"]) for c in update.message.reply_media_group.call_args_list], [10, 10])
            self.assertEqual(list(Path(folder).iterdir()), [])

    async def test_corrupt_collection_sends_nothing(self):
        update, _, _ = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "001.jpg", root / "002.jpg"
            Image.new("RGB", (10, 10), "blue").save(first)
            second.write_bytes(b"bad image")
            with self.assertRaises(bot.DownloadFailure):
                await bot.send_photos(update.message, [first, second])
            update.message.reply_photo.assert_not_called()
            update.message.reply_media_group.assert_not_called()

    async def test_album_document_and_single_photo_keep_order(self):
        update, _, _ = self.fixture()
        calls = []
        for kind in ("photo", "document", "media_group"):
            async def sent(kind=kind, **kwargs):
                calls.append(kind)
            getattr(update.message, "reply_" + kind).side_effect = sent
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for i, size in enumerate(((20, 20), (20, 20), (2100, 100), (20, 20))):
                path = Path(folder) / f"{i:03}.png"
                Image.new("RGB", size, "blue").save(path)
                paths.append(path)
            await bot.send_photos(update.message, paths)
        self.assertEqual(calls, ["media_group", "document", "photo"])

    async def test_overloaded_bot_rejects_without_creating_download(self):
        update, context, _ = self.fixture()
        context.bot_data["media_jobs"] = 4
        with patch.object(bot, "download_media") as download:
            await bot.process_media(update, context, URL, "audio")
            download.assert_not_called()
        self.assertEqual(context.bot_data["media_jobs"], 4)


if __name__ == "__main__":
    unittest.main()
