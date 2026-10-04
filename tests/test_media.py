"""Offline media/handler tests; Telegram calls and social-site requests are mocked."""
import functools
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


URL = "https://www.instagram.com/p/example/"


class MediaModeTests(unittest.TestCase):
    def test_image_files_never_selected_for_video_audio_or_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for extension in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
                image = root / ("image" + extension)
                image.write_bytes(b"image fixture")
                info = {"filepath": str(image), "requested_downloads": [{"filepath": str(image)}]}
                for mode in ("video", "audio", "source"):
                    with self.subTest(extension=extension, mode=mode), self.assertRaises(bot.DownloadFailure):
                        bot.find_downloaded_file(info, root, str(image), mode)

    def test_image_metadata_does_not_hide_completed_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "thumbnail.jpg"
            image.write_bytes(b"image fixture")
            video = root / "clip.mp4"
            video.write_bytes(b"video fixture")
            result = bot.find_downloaded_file(
                {"filepath": str(image)}, root, str(image), "video",
            )
            self.assertEqual(result, video.resolve())

    def test_removed_modes_rejected_before_url_resolution_or_download(self):
        for mode in ("photos", "photo", "image", ""):
            with self.subTest(mode=mode), patch.object(bot, "extract_supported_url") as parse, patch.object(
                bot.yt_dlp, "YoutubeDL",
            ) as factory:
                with self.assertRaises(bot.DownloadFailure):
                    bot.download_media(URL, Path("."), mode)
                parse.assert_not_called()
                factory.assert_not_called()

    def test_ydl_builder_rejects_removed_modes(self):
        with self.assertRaises(ValueError):
            bot.build_ydl_options(Path("."), URL, "photos")


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

    async def test_link_shows_only_video_audio_without_downloading(self):
        update, context, _ = self.fixture()
        with patch.object(bot, "download_media") as download:
            await bot.handle_link(update, context)
        download.assert_not_called()
        buttons = update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual([b.callback_data.split(":")[1] for b in buttons], ["video", "audio"])
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
            update.callback_query.data = f"media:audio:{key}"
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

    async def test_legacy_photos_callback_rejected_without_consuming_choice(self):
        update, context, _ = self.fixture()
        await bot.handle_link(update, context)
        key = next(iter(context.user_data["media_choices"]))
        stored = context.user_data["media_choices"][key].copy()
        update.callback_query.data = f"media:photos:{key}"
        with patch.object(bot, "process_media", new_callable=AsyncMock) as process:
            await bot.media_choice(update, context)
            process.assert_not_called()
        update.callback_query.answer.assert_awaited_once_with(
            bot.IMAGES_UNSUPPORTED_MESSAGE, show_alert=True,
        )
        update.callback_query.edit_message_reply_markup.assert_not_called()
        self.assertEqual(context.user_data["media_choices"][key], stored)
        self.assertEqual(context.bot_data, {})

    async def test_malformed_choices_rejected_without_downloading(self):
        for data in ("media:video:bad", "media:image:000000000000", "media:audio:"):
            with self.subTest(data=data):
                update, context, _ = self.fixture()
                update.callback_query.data = data
                with patch.object(bot, "process_media", new_callable=AsyncMock) as process:
                    await bot.media_choice(update, context)
                    process.assert_not_called()
                self.assertTrue(update.callback_query.answer.call_args.kwargs["show_alert"])

    async def test_photos_command_explains_removal_without_parsing_link(self):
        update, context, _ = self.fixture()
        update.message.text = "/photos@fixture_bot " + URL
        context.args = [URL]
        with patch.object(bot, "extract_supported_url") as parse, patch.object(
            bot, "process_media", new_callable=AsyncMock,
        ) as process:
            await bot.unsupported_photos(update, context)
            parse.assert_not_called()
            process.assert_not_called()
        update.message.reply_text.assert_awaited_once_with(bot.IMAGES_UNSUPPORTED_MESSAGE)
        self.assertEqual(context.bot_data, {})

    async def test_direct_photos_request_rejected_before_queue_and_worker(self):
        update, context, _ = self.fixture()
        context.bot_data["media_jobs"] = 4
        with patch.object(bot.tempfile, "mkdtemp") as create, patch.object(
            bot, "run_media_worker", new_callable=AsyncMock,
        ) as worker, patch.object(
            bot, "download_media_async", new_callable=AsyncMock,
        ) as download:
            await bot.process_media(update, context, URL, "photos")
            create.assert_not_called()
            worker.assert_not_called()
            download.assert_not_called()
        update.message.reply_text.assert_awaited_once_with(bot.IMAGES_UNSUPPORTED_MESSAGE)
        self.assertEqual(context.bot_data, {"media_jobs": 4})
        context.bot.send_chat_action.assert_not_called()
        for name in ("reply_audio", "reply_video", "reply_photo", "reply_media_group", "reply_document"):
            getattr(update.message, name).assert_not_called()

    async def test_overloaded_bot_rejects_without_creating_download(self):
        update, context, _ = self.fixture()
        context.bot_data["media_jobs"] = 4
        with patch.object(bot, "download_media") as download:
            await bot.process_media(update, context, URL, "audio")
            download.assert_not_called()
        self.assertEqual(context.bot_data["media_jobs"], 4)


if __name__ == "__main__":
    unittest.main()

