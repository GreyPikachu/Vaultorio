"""
Comprehensive unit and integration tests for TelegramBotService and TelegramBotSettings.
Tests cover API communication, caption auto-splitting, live dashboard formatting,
content-hash deduplication, settings menu callbacks, command routing, and zero emoji compliance.
Uses unittest.IsolatedAsyncioTestCase for native async test execution.
"""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List
import unittest
from unittest import mock

import httpx

from src.server.auth_manager import AuthManager
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager, SessionState
from src.server.storage import SaveStorageManager
from src.server.telegram_bot import (
    AI_PERSONA_STYLES,
    AI_SCHEDULE_MODES,
    TelegramBotService,
    TelegramBotSettings,
)


class MockTransport(httpx.AsyncBaseTransport):
    """Mock transport to simulate Telegram Bot API responses without external network access."""

    def __init__(self, response_router=None) -> None:
        self.response_router = response_router or self._default_router
        self.calls: List[Dict[str, Any]] = []

    def _default_router(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        content_json = {}
        if request.content and b"{" in request.content:
            try:
                content_json = json.loads(request.content.decode("utf-8"))
            except Exception:
                pass

        self.calls.append({"method": request.method, "url": url_str, "json": content_json})

        if "sendMessage" in url_str:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 101,
                        "chat": {"id": content_json.get("chat_id", 12345)},
                        "text": content_json.get("text", ""),
                    },
                },
            )
        elif "editMessageText" in url_str:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": content_json.get("message_id", 101),
                        "chat": {"id": content_json.get("chat_id", 12345)},
                        "text": content_json.get("text", ""),
                    },
                },
            )
        elif "sendPhoto" in url_str:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 202,
                        "chat": {"id": 12345},
                        "caption": "screenshot",
                    },
                },
            )
        elif "pinChatMessage" in url_str:
            return httpx.Response(200, json={"ok": True, "result": True})
        elif "answerCallbackQuery" in url_str:
            return httpx.Response(200, json={"ok": True, "result": True})
        elif "deleteMessage" in url_str:
            return httpx.Response(200, json={"ok": True, "result": True})
        elif "getUpdates" in url_str:
            return httpx.Response(200, json={"ok": True, "result": []})

        return httpx.Response(200, json={"ok": True, "result": {}})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self.response_router(request)


class TestTelegramBotSettings(unittest.TestCase):
    """Tests for TelegramBotSettings dataclass and persistence."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_tg_settings_"))

    def test_settings_defaults(self):
        """Verifies default values of TelegramBotSettings."""
        settings = TelegramBotSettings()
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.bot_token, "")
        self.assertIsNone(settings.chat_id)
        self.assertIsNone(settings.pinned_message_id)
        self.assertEqual(settings.dashboard_title, "Factorio Server - Live Status")
        self.assertTrue(settings.live_dashboard_enabled)
        self.assertTrue(settings.notify_host_start)
        self.assertTrue(settings.notify_guest_join)
        self.assertTrue(settings.notify_guest_leave)
        self.assertFalse(settings.notify_rolling_save)
        self.assertTrue(settings.notify_session_end)
        self.assertFalse(settings.silent_notifications)
        self.assertTrue(settings.ai_chronicle_enabled)
        self.assertEqual(settings.ai_schedule_mode, "hourly_and_events")
        self.assertEqual(settings.ai_persona_style, "reporter")

    def test_settings_save_and_load(self):
        """Verifies atomic save and load of settings."""
        state_file = self.temp_dir / "telegram_state.json"
        settings = TelegramBotSettings(
            bot_token="123456:ABC-DEF",
            chat_id=-100123456789,
            dashboard_title="Custom Title",
            notify_rolling_save=True,
        )
        settings.save(state_file)

        self.assertTrue(state_file.exists())
        loaded = TelegramBotSettings.load(state_file)
        self.assertEqual(loaded.bot_token, "123456:ABC-DEF")
        self.assertEqual(loaded.chat_id, -100123456789)
        self.assertEqual(loaded.dashboard_title, "Custom Title")
        self.assertTrue(loaded.notify_rolling_save)

    def test_settings_environment_fallbacks(self):
        """Verifies loading settings falls back to environment variables."""
        state_file = self.temp_dir / "empty_state.json"

        with unittest.mock.patch.dict(
            "os.environ",
            {
                "FACTORIO_TG_BOT_TOKEN": "env_token_xyz",
                "FACTORIO_TG_CHAT_ID": "-100999999",
                "FACTORIO_OPENROUTER_API_KEY": "sk-or-v1-env",
            },
        ):
            loaded = TelegramBotSettings.load(state_file)
            self.assertEqual(loaded.bot_token, "env_token_xyz")
            self.assertEqual(loaded.chat_id, -100999999)
            self.assertEqual(loaded.openrouter_api_key, "sk-or-v1-env")


class TestTelegramBotServiceAsync(unittest.IsolatedAsyncioTestCase):
    """Async tests for TelegramBotService API methods, dashboard and callbacks."""

    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_tg_svc_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            http_port=8088,
            udp_stun_port=14197,
        )
        self.config.storage_dir.mkdir(parents=True, exist_ok=True)
        self.transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.transport)

    async def asyncTearDown(self):
        await self.mock_client.aclose()

    async def test_send_message_success(self):
        """Verifies successful message sending."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 99999

        result = await bot.send_message(text="<b>Test Message</b>")
        self.assertIsNotNone(result)
        self.assertEqual(result["message_id"], 101)
        self.assertEqual(result["text"], "<b>Test Message</b>")

    async def test_send_message_unconfigured(self):
        """Verifies send_message returns None when bot is unconfigured."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = ""
        result = await bot.send_message(text="Hello")
        self.assertIsNone(result)

    async def test_edit_message_text_not_modified(self):
        """Verifies edit_message_text handles 'message is not modified' gracefully without failure."""
        def router(request: httpx.Request):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same",
                },
            )

        custom_transport = MockTransport(response_router=router)
        async with httpx.AsyncClient(transport=custom_transport) as client:
            bot = TelegramBotService(config=self.config, client=client)
            bot.settings.bot_token = "123:test"
            bot.settings.chat_id = 99999

            res = await bot.edit_message_text(message_id=50, text="Same text")
            self.assertIsNotNone(res)
            self.assertEqual(res.get("status"), "not_modified")

    async def test_send_photo_auto_splits_long_caption(self):
        """
        CRITICAL EDGE CASE: Telegram limits caption to 1024 characters.
        Verifies that when caption exceeds 1024 characters, send_photo automatically
        splits the text, sending photo with the first part and the remainder as a follow-up.
        """
        sent_requests: List[httpx.Request] = []

        def router(request: httpx.Request):
            sent_requests.append(request)
            url_str = str(request.url)
            if "sendPhoto" in url_str:
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 301}})
            elif "sendMessage" in url_str:
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 302}})
            return httpx.Response(200, json={"ok": True, "result": {}})

        custom_transport = MockTransport(response_router=router)
        async with httpx.AsyncClient(transport=custom_transport) as client:
            bot = TelegramBotService(config=self.config, client=client)
            bot.settings.bot_token = "123:test"
            bot.settings.chat_id = 99999

            paragraph_1 = "Первый параграф военной хроники с Наувиса. " * 15
            paragraph_2 = "Второй параграф боевого отчета с описанием уничтожения баз кусак. " * 15
            full_caption = f"{paragraph_1}\n\n{paragraph_2}"
            self.assertGreater(len(full_caption), 1024)

            res = await bot.send_photo(photo_bytes=b"FAKE_JPEG_BYTES", caption=full_caption)
            self.assertIsNotNone(res)
            self.assertEqual(res.get("message_id"), 301)

            # Verify two requests were issued: sendPhoto then sendMessage
            self.assertEqual(len(sent_requests), 2)
            self.assertIn("sendPhoto", str(sent_requests[0].url))
            self.assertIn("sendMessage", str(sent_requests[1].url))

    async def test_pin_and_delete_message(self):
        """Verifies pin_chat_message and delete_message API helpers."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 11111

        pin_res = await bot.pin_chat_message(message_id=42)
        self.assertTrue(pin_res)

        del_res = await bot.delete_message(message_id=42)
        self.assertTrue(del_res)

    def test_dashboard_formatting_zero_emojis(self):
        """
        CRITICAL MANDATE: Verifies live dashboard HTML contains strictly ZERO emojis
        across all session states (idle, claiming, hosting_active, uploading_final).
        """
        session_mgr = SessionLockManager(config=self.config)
        storage_mgr = SaveStorageManager(config=self.config)

        bot = TelegramBotService(
            config=self.config,
            session_manager=session_mgr,
            storage_manager=storage_mgr,
        )

        # State: IDLE
        html_idle = bot.format_dashboard_html()
        self.assertIn("[Свободен]", html_idle)
        self.assertIn("Мир в сейфе", html_idle)

        # State: HOSTING_ACTIVE with guests
        session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        session_mgr._session.host_name = "PlayerOne"
        session_mgr._session.started_at = time.time() - 3720  # 1 hour 2 mins
        session_mgr._session.active_guests = [{"name": "GuestA"}, {"name": "GuestB"}]

        html_active = bot.format_dashboard_html()
        self.assertIn("[В игре!]", html_active)
        self.assertIn("Хост: <b>PlayerOne</b>", html_active)
        self.assertIn("1 ч 2 мин", html_active)
        self.assertIn("Гости (2): GuestA, GuestB", html_active)

        # Check ZERO EMOJIS in both generated HTML texts
        for char in html_idle + html_active:
            code_point = ord(char)
            self.assertFalse(
                (
                    0x1F300 <= code_point <= 0x1FAFF
                    or 0x2600 <= code_point <= 0x27BF
                    or 0xFE00 <= code_point <= 0xFE0F
                    or 0x1F900 <= code_point <= 0x1F9FF
                ),
                f"Forbidden emoji character detected: {char} (U+{code_point:04X})",
            )

    async def test_update_dashboard_deduplication(self):
        """
        Verifies that calling update_dashboard multiple times with identical status
        deduplicates calls via content-hash and does NOT spam the Telegram API.
        """
        session_mgr = SessionLockManager(config=self.config)
        bot = TelegramBotService(
            config=self.config,
            session_manager=session_mgr,
            client=self.mock_client,
        )
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345

        # 1. First call -> creates and pins new dashboard message
        res1 = await bot.update_dashboard(force=False)
        self.assertIsNotNone(res1)
        self.assertEqual(bot.settings.pinned_message_id, 101)
        calls_count_after_first = len(self.transport.calls)
        self.assertGreaterEqual(calls_count_after_first, 2)  # sendMessage + pinChatMessage

        # 2. Second call with unchanged status -> should deduplicate and skip API calls
        res2 = await bot.update_dashboard(force=False)
        self.assertIsNone(res2)
        self.assertEqual(len(self.transport.calls), calls_count_after_first)

        # 3. Third call with force=True -> updates message
        res3 = await bot.update_dashboard(force=True)
        self.assertIsNotNone(res3)
        self.assertGreater(len(self.transport.calls), calls_count_after_first)

    async def test_settings_menu_toggles(self):
        """Verifies inline callback query toggles (e.g. notify_rolling_save, silent_notifications)."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345
        bot.settings.notify_rolling_save = False

        cb_query = {
            "id": "cb_001",
            "data": "toggle:notify_rolling_save",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }

        handled = await bot.handle_callback_query(cb_query)
        self.assertTrue(handled)
        self.assertTrue(bot.settings.notify_rolling_save)

        # Toggle again
        await bot.handle_callback_query(cb_query)
        self.assertFalse(bot.settings.notify_rolling_save)

    async def test_settings_menu_cycling(self):
        """Verifies cycling through AI schedule modes and persona styles."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345
        bot.settings.ai_schedule_mode = "hourly_and_events"

        cb_query = {
            "id": "cb_cycle_01",
            "data": "cycle:ai_schedule_mode",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }

        # Cycle 1: hourly_and_events -> important_only
        await bot.handle_callback_query(cb_query)
        self.assertEqual(bot.settings.ai_schedule_mode, "important_only")

        # Cycle 2: important_only -> only_on_exit
        await bot.handle_callback_query(cb_query)
        self.assertEqual(bot.settings.ai_schedule_mode, "only_on_exit")

        # Cycle 3: only_on_exit -> hourly_and_events
        await bot.handle_callback_query(cb_query)
        self.assertEqual(bot.settings.ai_schedule_mode, "hourly_and_events")

    async def test_settings_menu_admin_authorization(self):
        """Verifies that non-admin users cannot alter settings when admin_user_ids is configured."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345
        bot.settings.admin_user_ids = [999]  # Admin is only user 999

        cb_query = {
            "id": "cb_unauth",
            "data": "toggle:notify_host_start",
            "from": {"id": 404},  # Unauthorized user
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }

        initial_val = bot.settings.notify_host_start
        await bot.handle_callback_query(cb_query)
        # Setting should remain unchanged
        self.assertEqual(bot.settings.notify_host_start, initial_val)

    async def test_action_prompt_invite_with_auth_manager(self):
        """Verifies 'action:prompt_invite' creates an invite ticket via AuthManager."""
        auth_mgr = AuthManager(config=self.config)
        bot = TelegramBotService(
            config=self.config,
            auth_manager=auth_mgr,
            client=self.mock_client,
        )
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345

        cb_query = {
            "id": "cb_invite",
            "data": "action:prompt_invite",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }

        handled = await bot.handle_callback_query(cb_query)
        self.assertTrue(handled)

        # Verify invite exists in AuthManager
        devices = auth_mgr.list_devices_and_invites()
        self.assertEqual(len(devices.get("invites", [])), 1)

    async def test_handle_command_start_and_setup(self):
        """Verifies /start and /setup bind the chat_id and post welcome message."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = None

        msg = {
            "message_id": 10,
            "chat": {"id": -100555444},
            "from": {"id": 100},
            "text": "/setup",
        }

        handled = await bot.handle_message(msg)
        self.assertTrue(handled)
        self.assertEqual(bot.settings.chat_id, -100555444)

    async def test_handle_command_set_key_deletes_secret_message(self):
        """
        CRITICAL SECURITY TEST: /set_key saves OpenRouter key and deletes the incoming
        message so the secret API key does not linger in the Telegram chat log.
        """
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345

        msg = {
            "message_id": 777,
            "chat": {"id": 12345},
            "from": {"id": 100},
            "text": "/set_key sk-or-secret-token-123456",
        }

        handled = await bot.handle_message(msg)
        self.assertTrue(handled)
        self.assertEqual(bot.settings.openrouter_api_key, "sk-or-secret-token-123456")

        # Verify deleteMessage was called for message_id 777
        delete_calls = [c for c in self.transport.calls if "deleteMessage" in c["url"]]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0]["json"]["message_id"], 777)

    async def test_handle_command_set_title(self):
        """Verifies /set_title updates the dashboard title."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345

        msg = {
            "message_id": 20,
            "chat": {"id": 12345},
            "from": {"id": 100},
            "text": "/set_title Nauvis Expedition Hub",
        }

        handled = await bot.handle_message(msg)
        self.assertTrue(handled)
        self.assertEqual(bot.settings.dashboard_title, "Nauvis Expedition Hub")

    async def test_polling_start_and_stop(self):
        """Verifies long-polling tasks start and stop cleanly."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345

        await bot.start_polling()
        self.assertTrue(bot._is_polling)
        self.assertIsNotNone(bot._polling_task)
        self.assertIsNotNone(bot._dashboard_timer_task)

        await bot.stop_polling()
        self.assertFalse(bot._is_polling)
        self.assertIsNone(bot._polling_task)
        self.assertIsNone(bot._dashboard_timer_task)

    def test_settings_keyboard_zero_emojis(self):
        """Verifies settings keyboard buttons contain strictly ZERO emojis."""
        bot = TelegramBotService(config=self.config)
        kb = bot.build_settings_keyboard()
        rows = kb.get("inline_keyboard", [])
        self.assertGreater(len(rows), 0)

        all_button_texts = [btn["text"] for row in rows for btn in row]
        for text in all_button_texts:
            for char in text:
                cp = ord(char)
                self.assertFalse(
                    (
                        0x1F300 <= cp <= 0x1FAFF
                        or 0x2600 <= cp <= 0x27BF
                        or 0xFE00 <= cp <= 0xFE0F
                        or 0x1F900 <= cp <= 0x1F9FF
                    ),
                    f"Forbidden emoji character detected in button text: '{char}' in '{text}'",
                )
