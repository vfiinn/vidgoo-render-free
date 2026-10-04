import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs

import httpx
from aiohttp.test_utils import AioHTTPTestCase

import bot


TOKEN = "123456789:" + "A" * 35  # Fictitious test token; never used for network calls.


class FakeApplication:
    def __init__(self):
        self.running = False
        self.update_queue = asyncio.Queue(maxsize=32)
        self.bot = SimpleNamespace(set_webhook=AsyncMock(), delete_webhook=AsyncMock())
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False


class WebhookTests(AioHTTPTestCase):
    async def get_application(self):
        self.telegram_application = FakeApplication()
        return bot.create_web_app(
            self.telegram_application, "https://fixture.onrender.com", TOKEN
        )

    def headers(self):
        return {"X-Telegram-Bot-Api-Secret-Token": bot.webhook_secret(TOKEN)}

    def update(self, update_id=42):
        return {
            "update_id": update_id,
            "message": {
                "message_id": 1, "date": 1700000000,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "Test"},
                "text": "/start",
            },
        }

    async def test_registered_https_url_and_health(self):
        self.telegram_application.bot.set_webhook.assert_awaited_once_with(
            url="https://fixture.onrender.com/telegram/webhook",
            secret_token=bot.webhook_secret(TOKEN),
            allowed_updates=["message", "callback_query"], max_connections=4,
            drop_pending_updates=False,
        )
        for path in ("/", "/health"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"status": "ok"})

    async def test_rejects_missing_and_wrong_secret(self):
        for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"},
                        {"X-Telegram-Bot-Api-Secret-Token": "غير صحيح"}):
            response = await self.client.post(
                bot.WEBHOOK_PATH, json=self.update(), headers=headers
            )
            self.assertEqual(response.status, 401)
        self.assertTrue(self.telegram_application.update_queue.empty())

    async def test_valid_message_queued_and_duplicate_ignored(self):
        for _ in range(2):
            response = await self.client.post(
                bot.WEBHOOK_PATH, json=self.update(), headers=self.headers()
            )
            self.assertEqual(response.status, 200)
        self.assertEqual(self.telegram_application.update_queue.qsize(), 1)
        update = self.telegram_application.update_queue.get_nowait()
        self.assertEqual(update.message.text, "/start")

    async def test_full_queue_retries_without_losing_update(self):
        for _ in range(32):
            self.telegram_application.update_queue.put_nowait(object())
        response = await self.client.post(
            bot.WEBHOOK_PATH, json=self.update(), headers=self.headers()
        )
        self.assertEqual(response.status, 503)
        self.telegram_application.update_queue.get_nowait()
        response = await self.client.post(
            bot.WEBHOOK_PATH, json=self.update(), headers=self.headers()
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.telegram_application.update_queue.qsize(), 32)

    async def test_bad_json_and_update_rejected(self):
        for payload in ("not-json", "{}", "[]", '{"update_id":true}'):
            response = await self.client.post(
                bot.WEBHOOK_PATH, data=payload,
                headers={**self.headers(), "Content-Type": "application/json"},
            )
            self.assertEqual(response.status, 400)

    async def test_shutdown_keeps_webhook(self):
        await self.client.close()
        self.assertFalse(self.telegram_application.running)
        self.assertTrue(self.telegram_application.closed)
        self.telegram_application.bot.delete_webhook.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_base_url_must_be_plain_https(self):
        for url in ("http://fixture.test", "https://fixture.test/path",
                    "https://fixture.test?token=bad", "https://u:p@fixture.test",
                    "https:///missing-host"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                bot.create_web_app(FakeApplication(), url, TOKEN)

    def test_secret_is_stable_and_not_the_bot_token(self):
        self.assertEqual(bot.webhook_secret(TOKEN), bot.webhook_secret(TOKEN))
        self.assertNotEqual(bot.webhook_secret(TOKEN), bot.webhook_secret(TOKEN + "B"))
        self.assertEqual(len(bot.webhook_secret(TOKEN)), 64)
        self.assertNotIn(TOKEN, bot.webhook_secret(TOKEN))

    def test_render_main_opens_port_and_does_not_poll(self):
        application = Mock()
        with patch.object(bot, "BOT_TOKEN", TOKEN), patch.object(
            bot, "configure_logging"
        ), patch.object(bot, "require_media_tools"), patch.object(
            bot, "version", return_value="fixture"
        ), patch.dict(bot.os.environ, {
            "RENDER_EXTERNAL_URL": "https://fixture.onrender.com",
            "WEBHOOK_URL": "", "PORT": "12345", "RENDER": "true",
        }), patch.object(bot, "create_application", return_value=application) as create, patch.object(
            bot, "create_web_app", return_value="web-app"
        ) as web_app, patch.object(bot.web, "run_app") as run:
            bot.main()
        create.assert_called_once_with(TOKEN, webhook=True)
        web_app.assert_called_once_with(application, "https://fixture.onrender.com", TOKEN)
        run.assert_called_once_with("web-app", host="0.0.0.0", port=12345, access_log=None)
        application.run_polling.assert_not_called()


class RealDispatchTests(AioHTTPTestCase):
    async def get_application(self):
        self.replied = asyncio.Event()
        self.api_calls = []
        self.api_params = {}

        def fake_api(request):
            method = request.url.path.rsplit("/", 1)[-1]
            self.api_calls.append(method)
            self.api_params[method] = parse_qs(request.content.decode())
            if method == "getMe":
                result = {"id": 123456789, "is_bot": True,
                          "first_name": "Test", "username": "fixture_bot"}
            elif method in ("setWebhook", "answerCallbackQuery"):
                result = True
            elif method == "editMessageReplyMarkup":
                result = {"message_id": 2, "date": 1700000000,
                          "chat": {"id": 1, "type": "private"}, "text": "Choose"}
            elif method == "sendMessage":
                params = parse_qs(request.content.decode())
                self.reply_text = params["text"][0]
                result = {"message_id": 2, "date": 1700000000,
                          "chat": {"id": 1, "type": "private"},
                          "text": self.reply_text}
                self.replied.set()
            else:
                raise AssertionError(f"Unexpected Telegram API method: {method}")
            return httpx.Response(200, json={"ok": True, "result": result})

        transport = httpx.MockTransport(fake_api)
        original_client = httpx.AsyncClient
        with patch("telegram.request._httpxrequest.httpx.AsyncClient", side_effect=(
            lambda **kwargs: original_client(**{**kwargs, "transport": transport})
        )):
            self.real_application = bot.create_application(TOKEN, webhook=True)
        return bot.create_web_app(
            self.real_application, "https://fixture.onrender.com", TOKEN
        )

    async def test_start_flows_from_http_through_queue_to_telegram_reply(self):
        payload = {
            "update_id": 99,
            "message": {
                "message_id": 1, "date": 1700000000,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "Test"},
                "text": "/start",
                "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
            },
        }
        response = await self.client.post(
            bot.WEBHOOK_PATH, json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": bot.webhook_secret(TOKEN)},
        )
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(self.replied.wait(), timeout=3)
        self.assertIn("ابعت رابط منشور", self.reply_text)
        self.assertEqual(self.api_calls, ["getMe", "setWebhook", "sendMessage"])
        await self.client.close()
        self.assertFalse(self.real_application.running)
        self.assertNotIn("deleteWebhook", self.api_calls)

    async def test_removed_photos_command_replies_without_starting_media_job(self):
        payload = {
            "update_id": 102,
            "message": {
                "message_id": 1, "date": 1700000000,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "Test"},
                "text": "/photos https://www.instagram.com/p/example/",
                "entities": [{"offset": 0, "length": 7, "type": "bot_command"}],
            },
        }
        with patch.object(bot, "process_media", new_callable=AsyncMock) as process:
            response = await self.client.post(
                bot.WEBHOOK_PATH, json=payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": bot.webhook_secret(TOKEN)},
            )
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(self.replied.wait(), timeout=3)
        self.assertEqual(self.reply_text, bot.IMAGES_UNSUPPORTED_MESSAGE)
        process.assert_not_awaited()
        self.assertNotIn("media_jobs", self.real_application.bot_data)
        self.assertEqual(self.api_calls, ["getMe", "setWebhook", "sendMessage"])

    async def test_link_buttons_and_callback_dispatch_through_real_application(self):
        message = {
            "message_id": 1, "date": 1700000000,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "Test"},
            "text": "https://www.instagram.com/p/example/",
        }
        headers = {"X-Telegram-Bot-Api-Secret-Token": bot.webhook_secret(TOKEN)}
        response = await self.client.post(bot.WEBHOOK_PATH, json={
            "update_id": 100, "message": message,
        }, headers=headers)
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(self.replied.wait(), timeout=3)
        keyboard = json.loads(self.api_params["sendMessage"]["reply_markup"][0])["inline_keyboard"]
        self.assertEqual(len(keyboard), 1)
        self.assertEqual(len(keyboard[0]), 2)
        self.assertEqual([button["callback_data"].split(":")[1] for button in keyboard[0]],
                         ["video", "audio"])
        selected = keyboard[0][1]["callback_data"]
        processed = asyncio.Event()
        calls = []

        async def process(update, context, url, mode):
            calls.append((url, mode))
            self.assertIn("answerCallbackQuery", self.api_calls)
            processed.set()

        with patch.object(bot, "process_media", side_effect=process):
            response = await self.client.post(bot.WEBHOOK_PATH, json={
                "update_id": 101, "callback_query": {
                    "id": "fixture-query", "from": message["from"],
                    "chat_instance": "fixture-chat", "message": {**message, "message_id": 2},
                    "data": selected,
                },
            }, headers=headers)
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(processed.wait(), timeout=3)
        self.assertEqual(calls, [(message["text"], "audio")])
        self.assertIn("editMessageReplyMarkup", self.api_calls)


if __name__ == "__main__":
    unittest.main()

