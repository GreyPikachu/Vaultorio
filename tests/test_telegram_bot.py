"""
Comprehensive unit and integration tests for TelegramBotService and TelegramBotSettings.
Tests cover API communication, caption auto-splitting, live dashboard formatting,
content-hash deduplication, settings menu callbacks, command routing, and zero emoji compliance.
Uses unittest.IsolatedAsyncioTestCase for native async test execution.
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List
import unittest
from unittest import mock
import zipfile

from fastapi.testclient import TestClient
import httpx

from src.server.admin_cli import main as admin_cli_main
from src.server.api import create_app
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
        """Verifies 'action:prompt_invite' opens wizard and 'wiz:g:...' creates encrypted FCT1 code."""
        from src.common.invite_codec import decode_invite_code

        auth_mgr = AuthManager(config=self.config)
        bot = TelegramBotService(
            config=self.config,
            auth_manager=auth_mgr,
            client=self.mock_client,
        )
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345

        # 1. Opening the wizard via action:prompt_invite
        cb_query = {
            "id": "cb_invite",
            "data": "action:prompt_invite",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }
        handled = await bot.handle_callback_query(cb_query)
        self.assertTrue(handled)

        # 2. Selecting a parameter in wizard (wiz:s:7200:2)
        cb_switch = {
            "id": "cb_switch",
            "data": "wiz:s:7200:2",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }
        handled_switch = await bot.handle_callback_query(cb_switch)
        self.assertTrue(handled_switch)

        # 3. Generating the code (wiz:g:7200:2)
        cb_gen = {
            "id": "cb_gen",
            "data": "wiz:g:7200:2",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }
        handled_gen = await bot.handle_callback_query(cb_gen)
        self.assertTrue(handled_gen)

        # Verify invite exists in AuthManager
        devices = auth_mgr.list_devices_and_invites()
        invites = devices.get("invites", [])
        self.assertEqual(len(invites), 1)
        self.assertEqual(invites[0]["max_uses"], 2)

        # 4. Verify packaged code generation and decryptability
        pkg = await bot.generate_packaged_invite(ttl_seconds=3600, max_uses=1)
        self.assertTrue(pkg["invite_code"].startswith("FCT1-"))
        self.assertTrue(len(pkg["passphrase"]) >= 6)

        decoded = decode_invite_code(pkg["invite_code"], pkg["passphrase"])
        self.assertEqual(decoded["ticket"], pkg["ticket"])
        self.assertEqual(decoded["hub_http_url"], pkg["hub_url"])
        self.assertEqual(decoded["hub_stun_port"], pkg["stun_port"])

    async def test_invite_custom_passphrase_flow(self):
        """Verifies custom passphrase prompt, text input handling, and encrypted generation."""
        from src.common.invite_codec import decode_invite_code

        auth_mgr = AuthManager(config=self.config)
        bot = TelegramBotService(
            config=self.config,
            auth_manager=auth_mgr,
            client=self.mock_client,
        )
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345
        bot.settings.admin_user_ids = [111]

        # 1. User opens custom passphrase prompt
        cb_prompt = {
            "id": "cb_prompt",
            "data": "wiz:prompt_pass",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }
        await bot.handle_callback_query(cb_prompt)
        state = bot._get_wizard_state(111)
        self.assertTrue(state["awaiting_passphrase"])

        # 2. User sends text message with custom passphrase
        text_msg = {
            "message_id": 100,
            "chat": {"id": 12345},
            "from": {"id": 111},
            "text": "nauvis-secret-base",
        }
        handled = await bot.handle_message(text_msg)
        self.assertTrue(handled)
        self.assertFalse(state["awaiting_passphrase"])
        self.assertEqual(state["passphrase"], "nauvis-secret-base")
        self.assertTrue(state["is_custom"])

        # 3. User generates invite code with this custom passphrase
        cb_gen = {
            "id": "cb_gen",
            "data": "wiz:g:86400:1",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 12345}},
        }
        await bot.handle_callback_query(cb_gen)

        # Retrieve created invite from AuthManager
        devices = auth_mgr.list_devices_and_invites()
        invites = devices.get("invites", [])
        self.assertEqual(len(invites), 1)

        # Verify decode with custom passphrase works
        pkg = await bot.generate_packaged_invite(ttl_seconds=86400, max_uses=1, passphrase="nauvis-secret-base")
        dec = decode_invite_code(pkg["invite_code"], "nauvis-secret-base")
        self.assertEqual(dec["ticket"], pkg["ticket"])

    async def test_admin_commands_and_id_resolution(self):
        """Verifies /myid, /admin list, /admin add, /admin remove, and reply-to promotion."""
        bot = TelegramBotService(config=self.config, client=self.mock_client)
        bot.settings.bot_token = "123:test"
        bot.settings.chat_id = 12345
        bot.settings.admin_user_ids = [100]  # Primary owner

        # 1. /myid command
        id_msg = {
            "message_id": 20,
            "chat": {"id": 12345},
            "from": {"id": 777, "username": "TestFriend", "first_name": "Friend"},
            "text": "/myid",
        }
        res_id = await bot.handle_message(id_msg)
        self.assertTrue(res_id)
        # Verify user 777 was cached in known_users
        self.assertEqual(bot.settings.known_users.get("testfriend"), 777)

        # 2. Unauthorized user tries /admin list -> denied
        unauth_admin = {
            "message_id": 21,
            "chat": {"id": 12345},
            "from": {"id": 777, "username": "TestFriend"},
            "text": "/admin list",
        }
        res_unauth = await bot.handle_message(unauth_admin)
        self.assertTrue(res_unauth)

        # 3. Owner runs /admin list
        owner_admin = {
            "message_id": 22,
            "chat": {"id": 12345},
            "from": {"id": 100, "username": "Owner"},
            "text": "/admin list",
        }
        res_owner = await bot.handle_message(owner_admin)
        self.assertTrue(res_owner)

        # 4. Owner adds admin by @username
        add_by_tag = {
            "message_id": 23,
            "chat": {"id": 12345},
            "from": {"id": 100},
            "text": "/admin add @TestFriend",
        }
        await bot.handle_message(add_by_tag)
        self.assertIn(777, bot.settings.admin_user_ids)

        # 5. Owner adds another admin via reply
        add_by_reply = {
            "message_id": 24,
            "chat": {"id": 12345},
            "from": {"id": 100},
            "text": "/admin add",
            "reply_to_message": {
                "message_id": 15,
                "from": {"id": 888, "username": "ReplyUser"},
                "text": "Hello world",
            },
        }
        await bot.handle_message(add_by_reply)
        self.assertIn(888, bot.settings.admin_user_ids)

        # 6. Try removing primary owner -> denied
        rm_owner = {
            "message_id": 25,
            "chat": {"id": 12345},
            "from": {"id": 100},
            "text": "/admin remove 100",
        }
        await bot.handle_message(rm_owner)
        self.assertIn(100, bot.settings.admin_user_ids)

        # 7. Remove admin 777
        rm_friend = {
            "message_id": 26,
            "chat": {"id": 12345},
            "from": {"id": 100},
            "text": "/admin remove @TestFriend",
        }
        await bot.handle_message(rm_friend)
        self.assertNotIn(777, bot.settings.admin_user_ids)

    def test_admin_cli_commands(self):
        """Verifies admin-list, admin-add, and admin-remove via admin_cli."""
        import io
        from contextlib import redirect_stdout, redirect_stderr

        # 1. admin-list
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = admin_cli_main(["--storage-dir", str(self.config.storage_dir), "admin-list"])
        self.assertEqual(code, 0)

        # 2. admin-add
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = admin_cli_main([
                "--storage-dir", str(self.config.storage_dir),
                "admin-add", "--user-id", "9999", "--username", "@clibot",
            ])
        self.assertEqual(code, 0)
        self.assertIn("9999", buf.getvalue())

        # Verify added to file
        settings = TelegramBotSettings.load(self.config.telegram_state_path)
        self.assertIn(9999, settings.admin_user_ids)

        # 3. admin-remove with --force
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = admin_cli_main([
                "--storage-dir", str(self.config.storage_dir),
                "admin-remove", "--user-id", "9999", "--force",
            ])
        self.assertEqual(code, 0)
        settings = TelegramBotSettings.load(self.config.telegram_state_path)
        self.assertNotIn(9999, settings.admin_user_ids)

    def test_invite_wizard_zero_emojis(self):
        """CRITICAL: Verifies invite wizard keyboards and templates contain strictly ZERO emojis."""
        kb_wiz = TelegramBotService.build_invite_wizard_keyboard(selected_ttl=86400, selected_uses=1, is_custom=True)
        kb_succ = TelegramBotService.build_invite_success_keyboard(invite_code="FCT1-TEST")

        for kb in (kb_wiz, kb_succ):
            for row in kb.get("inline_keyboard", []):
                for btn in row:
                    text = btn.get("text", "")
                    for char in text:
                        cp = ord(char)
                        self.assertFalse(
                            0x1F300 <= cp <= 0x1FAFF
                            or 0x2600 <= cp <= 0x27BF
                            or 0xFE00 <= cp <= 0xFE0F
                            or 0x1F900 <= cp <= 0x1F9FF,
                            f"Forbidden emoji detected in wizard button: '{char}' in '{text}'",
                        )

        html_wiz = TelegramBotService.format_invite_wizard_html(86400, 1, passphrase="secret", is_custom=True)
        for char in html_wiz:
            cp = ord(char)
            self.assertFalse(
                0x1F300 <= cp <= 0x1FAFF
                or 0x2600 <= cp <= 0x27BF
                or 0xFE00 <= cp <= 0xFE0F
                or 0x1F900 <= cp <= 0x1F9FF,
                f"Forbidden emoji detected in wizard HTML: '{char}'",
            )

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

        # Non-admin cannot hijack and rebind via /setup
        msg_hijack = {
            "message_id": 11,
            "chat": {"id": -100999888},
            "from": {"id": 999},  # not an admin
            "text": "/setup",
        }
        handled_hijack = await bot.handle_message(msg_hijack)
        self.assertTrue(handled_hijack)
        self.assertEqual(bot.settings.chat_id, -100555444)  # Still bound to original chat

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


class TestTelegramBotNotifyHelpers(unittest.IsolatedAsyncioTestCase):
    """Tests for TelegramBotService event notification dispatchers."""

    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_tg_notify_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            http_port=8088,
            udp_stun_port=14197,
        )
        self.transport = MockTransport()
        self.client = httpx.AsyncClient(transport=self.transport)
        self.bot = TelegramBotService(config=self.config, client=self.client)
        self.bot.settings.bot_token = "123:test"
        self.bot.settings.chat_id = 99999

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_notify_host_started(self):
        """Verifies notify_host_started posts message and refreshes dashboard."""
        self.bot.settings.notify_host_start = True
        await self.bot.notify_host_started("Alex")

        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertGreaterEqual(len(send_calls), 1)
        sent_text = send_calls[0]["json"]["text"]
        self.assertIn("[СЕРВЕР]", sent_text)
        self.assertIn("Alex", sent_text)
        self.assertIn("запустил игру", sent_text)

        # Suppression test
        self.transport.calls.clear()
        self.bot.settings.notify_host_start = False
        await self.bot.notify_host_started("Alex")
        # Should not send message, but update dashboard
        send_msg_calls = [c for c in self.transport.calls if "sendMessage" in c["url"] and "[СЕРВЕР]" in c["json"].get("text", "")]
        self.assertEqual(len(send_msg_calls), 0)

    async def test_notify_guest_joined_and_left(self):
        """Verifies notify_guest_joined and notify_guest_left dispatchers."""
        self.bot.settings.notify_guest_join = True
        self.bot.settings.notify_guest_leave = True

        await self.bot.notify_guest_joined("Bob")
        join_calls = [c for c in self.transport.calls if "sendMessage" in c["url"] and "подключился" in c["json"].get("text", "")]
        self.assertEqual(len(join_calls), 1)
        self.assertIn("Bob", join_calls[0]["json"]["text"])

        await self.bot.notify_guest_left("Bob")
        leave_calls = [c for c in self.transport.calls if "sendMessage" in c["url"] and "покинул" in c["json"].get("text", "")]
        self.assertEqual(len(leave_calls), 1)
        self.assertIn("Bob", leave_calls[0]["json"]["text"])

    async def test_notify_rolling_save_saved(self):
        """Verifies notify_rolling_save_saved dispatches formatted autosave alerts."""
        self.bot.settings.notify_rolling_save = True
        await self.bot.notify_rolling_save_saved(version=5, size_mb=12.4, uploaded_by="Charlie")

        save_calls = [c for c in self.transport.calls if "sendMessage" in c["url"] and "[СЕЙВ]" in c["json"].get("text", "")]
        self.assertEqual(len(save_calls), 1)
        text = save_calls[0]["json"]["text"]
        self.assertIn("v5", text)
        self.assertIn("12.4 МБ", text)
        self.assertIn("Charlie", text)

    async def test_notify_session_ended(self):
        """Verifies notify_session_ended dispatches final session summary."""
        self.bot.settings.notify_session_end = True
        await self.bot.notify_session_ended(
            host_name="Alice",
            duration_str="2 ч 15 мин",
            version=6,
            size_mb=14.8,
        )

        end_calls = [c for c in self.transport.calls if "sendMessage" in c["url"] and "Сессия завершена" in c["json"].get("text", "")]
        self.assertEqual(len(end_calls), 1)
        text = end_calls[0]["json"]["text"]
        self.assertIn("Alice", text)
        self.assertIn("2 ч 15 мин", text)
        self.assertIn("v6", text)
        self.assertIn("14.8 МБ", text)


def _create_valid_factorio_zip(file_path: Path, marker: str = "world_v1") -> Path:
    """Creates a valid Factorio save archive for testing."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(file_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test_world/level.dat", f"Binary Level Data: {marker}".encode("utf-8") * 5)
        zf.writestr("test_world/info.json", json.dumps({"name": marker, "version": "2.0"}))
        zf.writestr("test_world/control.lua", "-- Lua script logic\n")
    return file_path


class TestApiTelegramHooksIntegration(unittest.TestCase):
    """Tests FastAPI HTTP REST endpoints triggering Telegram Bot hooks."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_api_tg_hooks_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            http_port=8088,
            udp_stun_port=14197,
        )
        self.mock_tg_bot = mock.MagicMock(spec=TelegramBotService)
        self.mock_tg_bot.is_configured = True
        self.mock_tg_bot.update_dashboard = mock.AsyncMock()
        self.mock_tg_bot.notify_host_started = mock.AsyncMock()
        self.mock_tg_bot.notify_guest_joined = mock.AsyncMock()
        self.mock_tg_bot.notify_guest_left = mock.AsyncMock()
        self.mock_tg_bot.notify_rolling_save_saved = mock.AsyncMock()
        self.mock_tg_bot.notify_session_ended = mock.AsyncMock()

        self.app = create_app(config=self.config, telegram_bot=self.mock_tg_bot)
        self.client = TestClient(self.app)

    def test_claim_and_confirm_hosting_hooks(self):
        """Verifies POST /claim and /confirm-hosting trigger telegram hooks."""
        # 1. Claim host
        res_claim = self.client.post("/claim", json={"player_name": "Alice"})
        self.assertEqual(res_claim.status_code, 200)
        self.assertTrue(res_claim.json()["success"])
        token = res_claim.json()["session_token"]

        self.mock_tg_bot.update_dashboard.assert_called_with(force=True)

        # 2. Confirm hosting
        res_confirm = self.client.post("/confirm-hosting", json={"session_token": token, "udp_port": 34197})
        self.assertEqual(res_confirm.status_code, 200)
        self.assertTrue(res_confirm.json()["success"])

        self.mock_tg_bot.notify_host_started.assert_called_with("Alice")

    def test_guest_register_and_deregister_hooks(self):
        """Verifies POST /guests/register and /guests/deregister trigger telegram hooks."""
        # Set up active hosting
        res_claim = self.client.post("/claim", json={"player_name": "Alice"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token})

        # Register guest
        res_reg = self.client.post("/guests/register", json={"guest_name": "Bob", "udp_port": 12345})
        self.assertEqual(res_reg.status_code, 200)
        self.mock_tg_bot.notify_guest_joined.assert_called_with("Bob")

        # Deregister guest
        res_dereg = self.client.post("/guests/deregister", json={"guest_name": "Bob"})
        self.assertEqual(res_dereg.status_code, 200)
        self.mock_tg_bot.notify_guest_left.assert_called_with("Bob")

    def test_begin_final_upload_and_release_hooks(self):
        """Verifies POST /begin-final-upload and /release trigger dashboard updates."""
        res_claim = self.client.post("/claim", json={"player_name": "Alice"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token})

        # Begin final upload
        res_begin = self.client.post("/begin-final-upload", json={"session_token": token})
        self.assertEqual(res_begin.status_code, 200)
        self.mock_tg_bot.update_dashboard.assert_called_with(force=True)

        # Release host
        res_release = self.client.post("/release", json={"session_token": token, "reason": "test_exit"})
        self.assertEqual(res_release.status_code, 200)
        self.mock_tg_bot.update_dashboard.assert_called_with(force=True)

    def test_upload_save_hooks(self):
        """Verifies POST /upload-save calls notify_session_ended or notify_rolling_save_saved."""
        res_claim = self.client.post("/claim", json={"player_name": "Alice"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token})

        save_zip = self.temp_dir / "valid_save.zip"
        _create_valid_factorio_zip(save_zip, marker="v1")

        # 1. Rolling autosave upload (release_on_success=False)
        with open(save_zip, "rb") as f:
            res_rolling = self.client.post(
                "/upload-save",
                data={"session_token": token, "release_on_success": "false"},
                files={"file": ("cloud_world.zip", f, "application/zip")},
            )
        self.assertEqual(res_rolling.status_code, 200)
        self.mock_tg_bot.notify_rolling_save_saved.assert_called_once()

        # 2. Final save upload (release_on_success=True)
        with open(save_zip, "rb") as f:
            res_final = self.client.post(
                "/upload-save",
                data={"session_token": token, "release_on_success": "true"},
                files={"file": ("cloud_world.zip", f, "application/zip")},
            )
        self.assertEqual(res_final.status_code, 200)
        self.mock_tg_bot.notify_session_ended.assert_called_once()

    def test_api_fault_tolerance_when_telegram_hook_fails(self):
        """CRITICAL: Verifies API endpoints do not crash if Telegram Bot throws network errors."""
        self.mock_tg_bot.update_dashboard.side_effect = Exception("Telegram API Down")

        res_claim = self.client.post("/claim", json={"player_name": "Alice"})
        self.assertEqual(res_claim.status_code, 200)
        self.assertTrue(res_claim.json()["success"])


class TestAdminCliTelegram(unittest.TestCase):
    """Tests for setup-telegram CLI subcommand in admin_cli.py."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_admin_tg_"))

    def test_setup_telegram_command_success(self):
        """Verifies setup-telegram updates and saves settings in telegram_state.json."""
        ret = admin_cli_main([
            "--storage-dir", str(self.temp_dir),
            "setup-telegram",
            "--bot-token", "999888:TEST_TOKEN",
            "--chat-id", "-100777888",
            "--openrouter-key", "sk-or-cli-key",
            "--openrouter-model", "google/gemini-2.0-flash-exp:free",
            "--dashboard-title", "Custom Expeditions Live Hub",
        ])
        self.assertEqual(ret, 0)

        # Verify persisted state
        state_path = self.temp_dir / "telegram_state.json"
        self.assertTrue(state_path.exists())

        settings = TelegramBotSettings.load(state_path)
        self.assertEqual(settings.bot_token, "999888:TEST_TOKEN")
        self.assertEqual(settings.chat_id, -100777888)
        self.assertEqual(settings.openrouter_api_key, "sk-or-cli-key")
        self.assertEqual(settings.openrouter_model, "google/gemini-2.0-flash-exp:free")
        self.assertEqual(settings.dashboard_title, "Custom Expeditions Live Hub")

    def test_setup_telegram_missing_token(self):
        """Verifies setup-telegram fails when --bot-token is missing."""
        with self.assertRaises(SystemExit):
            admin_cli_main([
                "--storage-dir", str(self.temp_dir),
                "setup-telegram",
            ])


class TestVpsServiceLimits(unittest.TestCase):
    """Tests VPS systemd service memory limits and configuration."""

    def test_vps_service_limits(self):
        """Verifies memory limits in deploy/vps/factorio-hub.service are set to 96M / 128M."""
        service_file = Path("deploy/vps/factorio-hub.service")
        self.assertTrue(service_file.exists())

        content = service_file.read_text(encoding="utf-8")
        self.assertIn("MemoryHigh=96M", content)
        self.assertIn("MemoryMax=128M", content)

        install_file = Path("deploy/vps/install_vps.sh")
        self.assertTrue(install_file.exists())
        install_content = install_file.read_text(encoding="utf-8")
        self.assertIn("128 MB", install_content)

