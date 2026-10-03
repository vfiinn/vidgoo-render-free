"""Offline regressions for the current Instagram photo worker."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from yt_dlp.extractor.instagram import InstagramIE

import bot


POST = "https://www.instagram.com/p/fixture/"


class InstagramPhotoTests(unittest.TestCase):
    @staticmethod
    def photo(url):
        return {
            "media_type": 1,
            "image_versions2": {"candidates": [{"url": url}]},
        }

    def test_carousel_keeps_photo_order_and_best_candidate_without_video(self):
        first = "https://scontent.cdninstagram.com/first.jpg"
        last = "https://scontent.fbcdn.net/last.jpg"
        product = {
            "media_type": 8,
            "carousel_media": [
                {
                    "media_type": 1,
                    "image_versions2": {
                        "candidates": [
                            {
                                "url": "https://cdninstagram.com/small.jpg",
                                "width": 100,
                                "height": 100,
                            },
                            {"url": first, "width": 800, "height": 600},
                            {
                                "url": "https://evil.test/larger.jpg",
                                "width": 2000,
                                "height": 2000,
                            },
                        ],
                    },
                },
                {**self.photo("https://cdninstagram.com/thumbnail.jpg"),
                 "media_type": 2},
                {**self.photo("https://cdninstagram.com/video.jpg"),
                 "video_versions": [{"url": "https://cdninstagram.com/video.mp4"}]},
                {**self.photo("https://cdninstagram.com/dash.jpg"),
                 "video_dash_manifest": "<MPD/>"},
                self.photo(last),
            ],
        }
        self.assertEqual(bot.select_instagram_photo_urls(product), [first, last])

    def test_carousel_stops_after_twenty_photos(self):
        urls = [f"https://cdninstagram.com/{index}.jpg" for index in range(25)]
        product = {
            "media_type": 8,
            "carousel_media": [self.photo(url) for url in urls],
        }
        self.assertEqual(bot.select_instagram_photo_urls(product), urls[:20])

    def test_untrusted_image_urls_are_rejected(self):
        for url in (
            "http://cdninstagram.com/photo.jpg",
            "https://cdninstagram.com.evil.test/photo.jpg",
            "https://evil.test/photo.jpg",
            "https://user:password@cdninstagram.com/photo.jpg",
            "https://cdninstagram.com:8443/photo.jpg",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(bot.DownloadFailure, "رابط صورة صالح"):
                    bot.select_instagram_photo_urls(self.photo(url))

    def test_metadata_extraction_has_bounded_requests_without_account_cookies(self):
        photo_url = "https://cdninstagram.com/photo.jpg"
        product = self.photo(photo_url)

        def extract_fixture(extractor, url):
            self.assertEqual(url, POST)
            extractor._extract_product(product)

        with patch.object(bot.yt_dlp, "YoutubeDL") as factory, patch.object(
            InstagramIE, "extract", autospec=True, side_effect=extract_fixture
        ) as extract:
            self.assertEqual(bot.extract_instagram_photo_urls(POST), [photo_url])

        extract.assert_called_once()
        factory.assert_called_once()
        options = factory.call_args.args[0]
        self.assertTrue(options["skip_download"])
        self.assertEqual(options["socket_timeout"], 12)
        self.assertEqual(options["retries"], 0)
        self.assertEqual(options["extractor_retries"], 0)
        self.assertIsNone(options["cookiefile"])
        self.assertIsNone(options["cookiesfrombrowser"])
        self.assertFalse(options["usenetrc"])

    def test_worker_has_sixty_second_timeout_and_removes_only_child_token(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photo = root / "001.jpg"
            photo.write_bytes(b"completed fixture photo")
            with patch.dict(bot.os.environ, {
                "BOT_TOKEN": "fixture-token-not-real",
                "VIDGOO_TEST_ENV": "retained",
            }), patch.object(bot.subprocess, "run", return_value=completed) as run:
                self.assertEqual(
                    bot.download_photos(POST + "?igsh=fixture", root), [photo]
                )
                self.assertEqual(bot.os.environ["BOT_TOKEN"], "fixture-token-not-real")
                self.assertEqual(bot.os.environ["VIDGOO_TEST_ENV"], "retained")

                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], [
                    sys.executable,
                    str(Path(bot.__file__).resolve()),
                    "--instagram-photos",
                    POST,
                    str(root.resolve()),
                ])
                options = run.call_args.kwargs
                self.assertEqual(options["timeout"], 60)
                self.assertEqual(options["stdin"], subprocess.DEVNULL)
                self.assertNotIn("BOT_TOKEN", options["env"])
                self.assertEqual(options["env"]["VIDGOO_TEST_ENV"], "retained")

    def test_worker_json_error_is_reported_without_returning_partial_photos(self):
        error = "تعذّر تنزيل صور المنشور بسبب حظر الطلب."
        completed = SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"error": error}, ensure_ascii=False),
            stderr="fixture worker failure",
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "001.jpg").write_bytes(b"partial collection")
            with patch.object(bot.subprocess, "run", return_value=completed):
                with self.assertRaises(bot.DownloadFailure) as raised:
                    bot.download_photos(POST, root)
        self.assertEqual(str(raised.exception), error)

    def test_metadata_failure_logs_original_cause_and_keeps_friendly_json(self):
        detail = "Unexpected Instagram metadata fixture response"
        with patch.object(bot, "configure_logging"), patch.object(
            InstagramIE, "extract",
            side_effect=bot.yt_dlp.utils.ExtractorError(detail),
        ), patch("sys.stdout", new_callable=io.StringIO) as output, self.assertLogs(
            bot.logger, level="WARNING"
        ) as logs:
            status = bot.instagram_photo_worker_main(POST, Path("unused"))

        self.assertEqual(status, 1)
        self.assertIn(detail, "\n".join(logs.output))
        self.assertEqual(json.loads(output.getvalue()), {
            "error": bot.download_error_message(Exception(detail)),
        })
        self.assertNotIn(detail, output.getvalue())

    def test_image_failure_logs_http_cause_and_keeps_friendly_json(self):
        image_url = "https://cdninstagram.com/photo.jpg"
        detail = "Fixture image access denied"
        failure = HTTPError(image_url, 403, detail, {}, None)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(bot, "configure_logging"), patch.object(
                bot, "extract_instagram_photo_urls", return_value=[image_url],
            ), patch.object(
                bot, "open_instagram_photo", side_effect=failure,
            ), patch("sys.stdout", new_callable=io.StringIO) as output, self.assertLogs(
                bot.logger, level="WARNING"
            ) as logs:
                status = bot.instagram_photo_worker_main(POST, root)
            self.assertEqual(list(root.iterdir()), [])

        self.assertEqual(status, 1)
        self.assertIn(detail, "\n".join(logs.output))
        self.assertIn("403", "\n".join(logs.output))
        self.assertEqual(json.loads(output.getvalue()), {
            "error": bot.download_error_message(failure),
        })
        self.assertNotIn(detail, output.getvalue())


if __name__ == "__main__":
    unittest.main()
