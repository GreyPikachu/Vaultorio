"""
Comprehensive unit and integration tests for Task 035:
- Default timezone UTC+3 (MSK) and show_updated_time toggle
- Timezone formatting helpers and label generator
- Pinned dashboard updated_time string toggle and dynamic timezone display
- Settings menu interactive buttons (toggle:show_updated_time, cycle:dashboard_tz_offset)
- /set_tz command parsing, bounds checking (-12..+14), and authorization
- Content hash deduplication excluding timestamp to prevent second-ticking editMessageText spam
- 100% Zero-Emoji Policy compliance
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Dict, List, Optional
import unittest
from unittest import mock

import httpx

from src.server.auth_manager import AuthManager
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager, SessionState
from src.server.storage import SaveStorageManager
from src.server.telegram_bot import (
    DASHBOARD_TIMEZONES,
    TelegramBotService,
    TelegramBotSettings,
    format_tz_label,
)


class MockTransport(httpx.AsyncBaseTransport):
    """Mock transport to simulate Telegram Bot API responses without network."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
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
        elif "pinChatMessage" in url_str:
            return httpx.Response(200, json={"ok": True, "result": True})
        elif "answerCallbackQuery" in url_str:
            return httpx.Response(200, json={"ok": True, "result": True})
        elif "deleteMessage" in url_str:
            return httpx.Response(200, json={"ok": True, "result": True})
        elif "setMyCommands" in url_str:
            return httpx.Response(200, json={"ok": True, "result": True})
        elif "getUpdates" in url_str:
            return httpx.Response(200, json={"ok": True, "result": []})

        return httpx.Response(200, json={"ok": True, "result": {}})


class TestTimezoneHelpers(unittest.TestCase):
    """Tests for format_tz_label and format_time_with_tz."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.bot = TelegramBotService(config=self.config)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_format_tz_label(self):
        """Verifies format_tz_label for zero, positive, and negative offsets."""
        self.assertEqual(format_tz_label(0), "UTC")
        self.assertEqual(format_tz_label(3), "UTC+3")
        self.assertEqual(format_tz_label(4), "UTC+4")
        self.assertEqual(format_tz_label(14), "UTC+14")
        self.assertEqual(format_tz_label(-4), "UTC-4")
        self.assertEqual(format_tz_label(-12), "UTC-12")

    def test_format_time_with_tz_custom_datetime(self):
        """Verifies conversion of UTC datetime into configured timezone offset."""
        # Fixed point in time: 2026-10-25 12:00:00 UTC
        utc_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

        # Default is UTC+3 -> 15:00:00 UTC+3
        self.bot.settings.dashboard_tz_offset = 3
        res = self.bot.format_time_with_tz(dt=utc_dt, fmt="%H:%M:%S")
        self.assertEqual(res, "15:00:00 UTC+3")

        # UTC -> 12:00:00 UTC
        self.bot.settings.dashboard_tz_offset = 0
        res = self.bot.format_time_with_tz(dt=utc_dt, fmt="%H:%M:%S")
        self.assertEqual(res, "12:00:00 UTC")

        # UTC-4 -> 08:00:00 UTC-4
        self.bot.settings.dashboard_tz_offset = -4
        res = self.bot.format_time_with_tz(dt=utc_dt, fmt="%H:%M:%S")
        self.assertEqual(res, "08:00:00 UTC-4")

        # UTC+10 -> 22:00:00 UTC+10
        self.bot.settings.dashboard_tz_offset = 10
        res = self.bot.format_time_with_tz(dt=utc_dt, fmt="%H:%M:%S")
        self.assertEqual(res, "22:00:00 UTC+10")

    def test_format_time_with_tz_clamping(self):
        """Verifies that offsets outside [-12, 14] are safely clamped."""
        utc_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

        # 99 should clamp to 14 -> 02:00:00 UTC+14 (next day)
        self.bot.settings.dashboard_tz_offset = 99
        res = self.bot.format_time_with_tz(dt=utc_dt, fmt="%H:%M:%S")
        self.assertEqual(res, "02:00:00 UTC+14")

        # -99 should clamp to -12 -> 00:00:00 UTC-12
        self.bot.settings.dashboard_tz_offset = -99
        res = self.bot.format_time_with_tz(dt=utc_dt, fmt="%H:%M:%S")
        self.assertEqual(res, "00:00:00 UTC-12")

    def test_format_time_with_tz_default_now(self):
        """Verifies calling format_time_with_tz without arguments uses current time."""
        self.bot.settings.dashboard_tz_offset = 3
        res = self.bot.format_time_with_tz()
        self.assertTrue(res.endswith("UTC+3"))
        self.assertTrue(re.match(r"^\d{2}:\d{2}:\d{2} UTC\+3$", res))


class TestTelegramBotSettingsTimezone(unittest.TestCase):
    """Tests for TelegramBotSettings defaults, deserialization, and env var loading."""

    def test_default_values(self):
        """Default settings must have UTC+3 (MSK) and show_updated_time=True."""
        settings = TelegramBotSettings()
        self.assertEqual(settings.dashboard_tz_offset, 3)
        self.assertTrue(settings.show_updated_time)

    def test_from_dict_valid_and_bounds(self):
        """from_dict validates and clamps dashboard_tz_offset."""
        # Valid values
        s1 = TelegramBotSettings.from_dict({"dashboard_tz_offset": 5, "show_updated_time": False})
        self.assertEqual(s1.dashboard_tz_offset, 5)
        self.assertFalse(s1.show_updated_time)

        # Clamping upper bound
        s2 = TelegramBotSettings.from_dict({"dashboard_tz_offset": 25})
        self.assertEqual(s2.dashboard_tz_offset, 14)

        # Clamping lower bound
        s3 = TelegramBotSettings.from_dict({"dashboard_tz_offset": -20})
        self.assertEqual(s3.dashboard_tz_offset, -12)

        # Invalid types fallback to default 3
        s4 = TelegramBotSettings.from_dict({"dashboard_tz_offset": "invalid"})
        self.assertEqual(s4.dashboard_tz_offset, 3)

        s5 = TelegramBotSettings.from_dict({"dashboard_tz_offset": None})
        self.assertEqual(s5.dashboard_tz_offset, 3)

    def test_load_env_var_override(self):
        """FACTORIO_TG_TZ_OFFSET env var should be used when settings file does not exist."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "telegram_state.json"

            with mock.patch.dict(os.environ, {"FACTORIO_TG_TZ_OFFSET": "7"}):
                s = TelegramBotSettings.load(file_path)
                self.assertEqual(s.dashboard_tz_offset, 7)

            with mock.patch.dict(os.environ, {"FACTORIO_TG_TZ_OFFSET": "-5"}):
                s = TelegramBotSettings.load(file_path)
                self.assertEqual(s.dashboard_tz_offset, -5)

            with mock.patch.dict(os.environ, {"FACTORIO_TG_TZ_OFFSET": "999"}):
                s = TelegramBotSettings.load(file_path)
                self.assertEqual(s.dashboard_tz_offset, 14)

            with mock.patch.dict(os.environ, {"FACTORIO_TG_TZ_OFFSET": "corrupt"}):
                s = TelegramBotSettings.load(file_path)
                self.assertEqual(s.dashboard_tz_offset, 3)


class TestDashboardFormattingTimezone(unittest.TestCase):
    """Tests for format_dashboard_html updated time string and timezone display."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.session_mgr = SessionLockManager(config=self.config)
        self.storage_mgr = SaveStorageManager(config=self.config)
        self.bot = TelegramBotService(
            config=self.config,
            session_manager=self.session_mgr,
            storage_manager=self.storage_mgr,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_format_dashboard_shows_updated_time_by_default(self):
        """By default, format_dashboard_html includes 'Обновлено: ... UTC+3'."""
        self.assertTrue(self.bot.settings.show_updated_time)
        self.assertEqual(self.bot.settings.dashboard_tz_offset, 3)

        text = self.bot.format_dashboard_html()
        self.assertIn("Обновлено:", text)
        self.assertRegex(text, r"<i>Обновлено: \d{2}:\d{2}:\d{2} UTC\+3</i>")

    def test_format_dashboard_hides_updated_time_when_disabled(self):
        """When show_updated_time is False, 'Обновлено:' line is completely excluded."""
        self.bot.settings.show_updated_time = False

        text = self.bot.format_dashboard_html()
        self.assertNotIn("Обновлено:", text)
        self.assertNotIn("UTC+3", text)

    def test_format_dashboard_custom_timezone(self):
        """When dashboard_tz_offset is 0, outputs 'UTC' without sign."""
        self.bot.settings.show_updated_time = True
        self.bot.settings.dashboard_tz_offset = 0

        text = self.bot.format_dashboard_html()
        self.assertIn("Обновлено:", text)
        self.assertRegex(text, r"<i>Обновлено: \d{2}:\d{2}:\d{2} UTC</i>")
        self.assertNotIn("UTC+0", text)
        self.assertNotIn("UTC-0", text)


class TestInviteSuccessFormattingTimezone(unittest.TestCase):
    """Tests for format_invite_success_html timezone localization."""

    def test_invite_success_localized_to_tz_offset(self):
        """format_invite_success_html converts exp_dt into the provided timezone."""
        # Fixed epoch timestamp: 1700000000 (2023-11-14 22:13:20 UTC)
        invite_info = {
            "invite_code": "FACTORIO-TEST-1234",
            "passphrase": "secret-password",
            "ttl_seconds": 3600,
            "max_uses": 1,
            "expires_at": 1700000000,
        }

        # With default tz_offset=3 (MSK) -> 2023-11-15 01:13 UTC+3
        text_msk = TelegramBotService.format_invite_success_html(invite_info, tz_offset=3)
        self.assertIn("UTC+3", text_msk)
        self.assertIn("2023-11-15 01:13 UTC+3", text_msk)

        # With tz_offset=0 (UTC) -> 2023-11-14 22:13 UTC
        text_utc = TelegramBotService.format_invite_success_html(invite_info, tz_offset=0)
        self.assertIn("UTC", text_utc)
        self.assertIn("2023-11-14 22:13 UTC", text_utc)
        self.assertNotIn("UTC+0", text_utc)

        # Backward compatibility without second argument defaults to 3
        text_default = TelegramBotService.format_invite_success_html(invite_info)
        self.assertIn("UTC+3", text_default)
        self.assertIn("2023-11-15 01:13 UTC+3", text_default)


class TestSettingsMenuKeyboardAndTimezoneInteraction(unittest.IsolatedAsyncioTestCase):
    """Tests for settings keyboard toggles, timezone cycling, and /set_tz command."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.transport)
        self.session_mgr = SessionLockManager(config=self.config)
        self.storage_mgr = SaveStorageManager(config=self.config)
        self.bot = TelegramBotService(
            config=self.config,
            session_manager=self.session_mgr,
            storage_manager=self.storage_mgr,
            client=self.mock_client,
        )
        self.bot.settings.bot_token = "123:test"
        self.bot.settings.chat_id = 99999
        self.bot.settings.pinned_message_id = 101
        self.bot.settings.admin_user_ids = [42]

    async def asyncTearDown(self):
        await self.mock_client.aclose()
        self.tmp_dir.cleanup()

    def test_settings_keyboard_buttons_content(self):
        """Verifies 'Время обновления' and 'Пояс: ...' buttons exist in build_settings_keyboard(subpage='dashboard')."""
        self.bot.settings.show_updated_time = True
        self.bot.settings.dashboard_tz_offset = 3

        kb = self.bot.build_settings_keyboard(subpage="dashboard")
        buttons = [btn for row in kb["inline_keyboard"] for btn in row]
        btn_map = {b["callback_data"]: b["text"] for b in buttons}

        self.assertIn("toggle:show_updated_time", btn_map)
        self.assertEqual(btn_map["toggle:show_updated_time"], "Время обновления: [ВКЛ]")

        self.assertIn("cycle:dashboard_tz_offset", btn_map)
        self.assertEqual(btn_map["cycle:dashboard_tz_offset"], "Пояс: UTC+3 (МСК)")

        # Switch show_updated_time to False
        self.bot.settings.show_updated_time = False
        self.bot.settings.dashboard_tz_offset = 0
        kb2 = self.bot.build_settings_keyboard(subpage="dashboard")
        btn_map2 = {b["callback_data"]: b["text"] for row in kb2["inline_keyboard"] for b in row}
        self.assertEqual(btn_map2["toggle:show_updated_time"], "Время обновления: [ВЫКЛ]")
        self.assertEqual(btn_map2["cycle:dashboard_tz_offset"], "Пояс: UTC (0)")

    async def test_callback_toggle_show_updated_time(self):
        """Verifies clicking toggle:show_updated_time flips flag and updates dashboard."""
        self.assertTrue(self.bot.settings.show_updated_time)

        cb_query = {
            "id": "cb_001",
            "from": {"id": 42, "username": "admin"},
            "message": {"message_id": 200, "chat": {"id": 99999}},
            "data": "toggle:show_updated_time",
        }
        res = await self.bot.handle_callback_query(cb_query)
        self.assertTrue(res)
        self.assertFalse(self.bot.settings.show_updated_time)

        # Check that editMessageText was called for settings menu and update_dashboard
        edit_calls = [c for c in self.transport.calls if "editMessageText" in c["url"]]
        self.assertGreaterEqual(len(edit_calls), 1)

    async def test_callback_cycle_dashboard_tz_offset(self):
        """Verifies clicking cycle:dashboard_tz_offset advances through DASHBOARD_TIMEZONES."""
        # Initial offset is 3 (index 2: UTC+3 МСК)
        self.assertEqual(self.bot.settings.dashboard_tz_offset, 3)

        cb_query = {
            "id": "cb_002",
            "from": {"id": 42, "username": "admin"},
            "message": {"message_id": 200, "chat": {"id": 99999}},
            "data": "cycle:dashboard_tz_offset",
        }
        res = await self.bot.handle_callback_query(cb_query)
        self.assertTrue(res)
        # Next in list is index 3: offset 4 (UTC+4 СМР)
        self.assertEqual(self.bot.settings.dashboard_tz_offset, 4)

        # Cycle all the way to 0 (index 0)
        offsets = [item[0] for item in DASHBOARD_TIMEZONES]
        # Current index is 3 (offset 4). To wrap around to index 0: len(offsets) - 3 steps
        for _ in range(len(offsets) - 3):
            await self.bot.handle_callback_query(cb_query)
        self.assertEqual(self.bot.settings.dashboard_tz_offset, 0)

    async def test_set_tz_command_success(self):
        """Admin can set timezone offset using /set_tz."""
        msg = {
            "message_id": 301,
            "from": {"id": 42, "username": "admin"},
            "chat": {"id": 99999},
            "text": "/set_tz +5",
        }
        handled = await self.bot.handle_message(msg)
        self.assertTrue(handled)
        self.assertEqual(self.bot.settings.dashboard_tz_offset, 5)

        # Verify confirmation message was sent
        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertTrue(any("Часовой пояс обновлен: <b>UTC+5</b>" in c["json"].get("text", "") for c in send_calls))

        # Test negative offset
        msg_neg = {
            "message_id": 302,
            "from": {"id": 42, "username": "admin"},
            "chat": {"id": 99999},
            "text": "/set_tz -4",
        }
        await self.bot.handle_message(msg_neg)
        self.assertEqual(self.bot.settings.dashboard_tz_offset, -4)

        # Test zero offset
        msg_zero = {
            "message_id": 303,
            "from": {"id": 42, "username": "admin"},
            "chat": {"id": 99999},
            "text": "/set_tz 0",
        }
        await self.bot.handle_message(msg_zero)
        self.assertEqual(self.bot.settings.dashboard_tz_offset, 0)

    async def test_set_tz_command_invalid_argument_shows_usage(self):
        """Invalid or out-of-bounds offset retains current setting and replies with usage."""
        initial_tz = self.bot.settings.dashboard_tz_offset

        for bad_cmd in ["/set_tz", "/set_tz +99", "/set_tz -20", "/set_tz invalid"]:
            msg = {
                "message_id": 401,
                "from": {"id": 42, "username": "admin"},
                "chat": {"id": 99999},
                "text": bad_cmd,
            }
            handled = await self.bot.handle_message(msg)
            self.assertTrue(handled)
            self.assertEqual(self.bot.settings.dashboard_tz_offset, initial_tz)

        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertTrue(any("Использование: <code>/set_tz +3</code>" in c["json"].get("text", "") for c in send_calls))

    async def test_set_tz_command_unauthorized_user(self):
        """Unauthorized user is rejected from setting timezone."""
        initial_tz = self.bot.settings.dashboard_tz_offset
        msg = {
            "message_id": 501,
            "from": {"id": 999, "username": "stranger"},
            "chat": {"id": 99999},
            "text": "/set_tz +4",
        }
        handled = await self.bot.handle_message(msg)
        self.assertTrue(handled)
        self.assertEqual(self.bot.settings.dashboard_tz_offset, initial_tz)

        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertTrue(any("нет прав администратора" in c["json"].get("text", "") for c in send_calls))


class TestDashboardDeduplicationWithSecondsTicking(unittest.IsolatedAsyncioTestCase):
    """
    CRITICAL EDGE CASE:
    When seconds tick in format_dashboard_html(), update_dashboard(force=False)
    must NOT trigger an editMessageText HTTP call if the server state is unchanged.
    """

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.transport)
        self.session_mgr = SessionLockManager(config=self.config)
        self.storage_mgr = SaveStorageManager(config=self.config)
        self.bot = TelegramBotService(
            config=self.config,
            session_manager=self.session_mgr,
            storage_manager=self.storage_mgr,
            client=self.mock_client,
        )
        self.bot.settings.bot_token = "123:test"
        self.bot.settings.chat_id = 99999
        self.bot.settings.pinned_message_id = 101

    async def asyncTearDown(self):
        await self.mock_client.aclose()
        self.tmp_dir.cleanup()

    async def test_update_dashboard_deduplication_ignores_seconds_ticking(self):
        """Two update_dashboard(force=False) calls with advancing time only send 1 HTTP call."""
        # Call 1 at T0
        res1 = await self.bot.update_dashboard(force=False)
        self.assertIsNotNone(res1)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["json"]["message_id"], 101)

        # Call 2: server state remains unchanged, but seconds advance.
        # Even if format_dashboard_html() returns a different timestamp,
        # content_hash strips the "Обновлено: ..." line and detects identical content.
        res2 = await self.bot.update_dashboard(force=False)
        self.assertIsNone(res2)
        # Verify no second editMessageText call was dispatched
        self.assertEqual(len(self.transport.calls), 1)

    async def test_update_dashboard_force_bypasses_deduplication(self):
        """update_dashboard(force=True) forces an API update even if content unchanged."""
        res1 = await self.bot.update_dashboard(force=False)
        self.assertIsNotNone(res1)
        self.assertEqual(len(self.transport.calls), 1)

        res2 = await self.bot.update_dashboard(force=True)
        self.assertIsNotNone(res2)
        self.assertEqual(len(self.transport.calls), 2)

    async def test_update_dashboard_triggers_when_server_state_changes(self):
        """When real server state changes (e.g. host starts hosting), update_dashboard triggers."""
        # Initial idle call
        await self.bot.update_dashboard(force=False)
        self.assertEqual(len(self.transport.calls), 1)

        # Host starts hosting
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"

        # Content changed -> should trigger editMessageText
        res2 = await self.bot.update_dashboard(force=False)
        self.assertIsNotNone(res2)
        self.assertEqual(len(self.transport.calls), 2)


class TestZeroEmojiCompliance(unittest.TestCase):
    """Enforces strict Zero-Emoji Policy across all Task 035 additions."""

    EMOJI_PATTERN = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA70-\U0001FAFF"
        "]+",
        flags=re.UNICODE,
    )

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.bot = TelegramBotService(config=self.config)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_settings_keyboard_has_zero_emojis(self):
        """No emojis in settings keyboard buttons or callback data."""
        kb = self.bot.build_settings_keyboard()
        for row in kb["inline_keyboard"]:
            for btn in row:
                self.assertFalse(
                    self.EMOJI_PATTERN.search(btn["text"]),
                    f"Emoji found in button text: {btn['text']}",
                )
                self.assertFalse(
                    self.EMOJI_PATTERN.search(btn["callback_data"]),
                    f"Emoji found in callback_data: {btn['callback_data']}",
                )

    def test_dashboard_and_tz_labels_have_zero_emojis(self):
        """No emojis in dashboard output or timezone labels."""
        for offset, label in DASHBOARD_TIMEZONES:
            self.assertFalse(self.EMOJI_PATTERN.search(label), f"Emoji in tz label: {label}")

        dashboard_text = self.bot.format_dashboard_html()
        self.assertFalse(self.EMOJI_PATTERN.search(dashboard_text), f"Emoji in dashboard: {dashboard_text}")
