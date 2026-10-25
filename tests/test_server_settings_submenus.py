"""
Unit and integration tests for Telegram Bot modular settings submenus (Task 038).
Verifies:
1. Main menu layout and category buttons.
2. Notifications, Dashboard, and AI Chronicle submenus.
3. Submenu navigation (menu:notifications, menu:dashboard, menu:chronicle, menu:main).
4. Submenu context retention when clicking toggle: or cycle: buttons.
5. get_setting_subpage helper routing.
6. Strict Zero-Emoji compliance across all generated texts and keyboards.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any, Dict, List
import tempfile

import httpx

from src.server.config import ServerConfig
from src.server.telegram_bot import (
    TelegramBotService,
    TelegramBotSettings,
)


class MockTransport(httpx.AsyncBaseTransport):
    """Mocks outgoing HTTP requests to the Telegram Bot API."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.responses: Dict[str, Any] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)

        if "getMe" in url:
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456, "is_bot": True, "username": "test_bot"}})

        if "setMyCommands" in url:
            return httpx.Response(200, json={"ok": True, "result": True})

        if "sendMessage" in url:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 999, "chat": {"id": 12345}}})

        if "editMessageText" in url:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 999, "chat": {"id": 12345}}})

        if "answerCallbackQuery" in url:
            return httpx.Response(200, json={"ok": True, "result": True})

        if "pinChatMessage" in url:
            return httpx.Response(200, json={"ok": True, "result": True})

        return httpx.Response(200, json={"ok": True, "result": {}})


class TestTelegramSettingsSubmenus(unittest.IsolatedAsyncioTestCase):
    """Test suite for modular settings submenus in TelegramBotService."""

    EMOJI_PATTERN = re.compile(
        "[\U0001f300-\U0001f5ff\U0001f600-\U0001f64f\U0001f680-\U0001f6ff"
        "\U0001f700-\U0001f77f\U0001f780-\U0001f7ff\U0001f800-\U0001f8ff"
        "\U0001f900-\U0001f9ff\U0001fa00-\U0001faff\U00002702-\U000027b0"
        "\U000024c2-\U0001f251\U0000200d\U0000fe0f\U0000fe0e]"
    )

    async def asyncSetUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_settings_sub_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir / "data",
        )
        self.mock_transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.mock_transport)
        self.bot = TelegramBotService(config=self.config, client=self.mock_client)
        self.bot.settings.bot_token = "123456:TEST_TOKEN"
        self.bot.settings.chat_id = 99999
        self.bot.settings.admin_user_ids = [42]

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        await self.mock_client.aclose()

    def test_main_menu_structure_and_categories(self):
        """Verifies the main settings menu is compact and contains category buttons."""
        kb = self.bot.build_settings_keyboard(subpage="main")
        rows = kb.get("inline_keyboard", [])
        self.assertEqual(len(rows), 4)

        all_callbacks = [btn["callback_data"] for row in rows for btn in row]
        self.assertIn("menu:notifications", all_callbacks)
        self.assertIn("menu:dashboard", all_callbacks)
        self.assertIn("menu:chronicle", all_callbacks)
        self.assertIn("action:admin_list", all_callbacks)
        self.assertIn("menu:worlds", all_callbacks)
        self.assertIn("action:prompt_invite", all_callbacks)
        self.assertIn("menu:close", all_callbacks)

        text = self.bot.format_settings_menu_html(subpage="main")
        self.assertIn("Настройки Telegram-бота Factorio", text)
        self.assertIn("Выберите категорию", text)

    def test_notifications_submenu_structure_and_buttons(self):
        """Verifies notifications submenu contains all 6 notification toggles and back button."""
        kb = self.bot.build_settings_keyboard(subpage="notifications")
        rows = kb.get("inline_keyboard", [])
        self.assertEqual(len(rows), 4)

        buttons = {b["callback_data"]: b["text"] for row in rows for b in row}
        self.assertIn("toggle:notify_host_start", buttons)
        self.assertIn("toggle:notify_session_end", buttons)
        self.assertIn("toggle:notify_guest_join", buttons)
        self.assertIn("toggle:notify_guest_leave", buttons)
        self.assertIn("toggle:notify_rolling_save", buttons)
        self.assertIn("toggle:silent_notifications", buttons)
        self.assertIn("menu:main", buttons)
        self.assertEqual(buttons["menu:main"], "« Назад")

        text = self.bot.format_settings_menu_html(subpage="notifications")
        self.assertIn("Настройки уведомлений", text)

    def test_dashboard_submenu_structure_and_buttons(self):
        """Verifies dashboard submenu contains live dashboard, ping, time, timezone, repin, title, and back."""
        kb = self.bot.build_settings_keyboard(subpage="dashboard")
        rows = kb.get("inline_keyboard", [])
        self.assertEqual(len(rows), 5)

        buttons = {b["callback_data"]: b["text"] for row in rows for b in row}
        self.assertIn("toggle:live_dashboard_enabled", buttons)
        self.assertIn("toggle:show_ping_in_dashboard", buttons)
        self.assertIn("toggle:show_updated_time", buttons)
        self.assertIn("cycle:dashboard_tz_offset", buttons)
        self.assertIn("action:repin", buttons)
        self.assertIn("action:prompt_title", buttons)
        self.assertIn("menu:main", buttons)
        self.assertEqual(buttons["menu:main"], "« Назад")

        text = self.bot.format_settings_menu_html(subpage="dashboard")
        self.assertIn("Настройки закрепленного дашборда", text)

    def test_chronicle_submenu_structure_and_buttons(self):
        """Verifies AI chronicle submenu contains enable toggle, schedule, style, key prompt, and back."""
        kb = self.bot.build_settings_keyboard(subpage="chronicle")
        rows = kb.get("inline_keyboard", [])
        self.assertEqual(len(rows), 4)

        buttons = {b["callback_data"]: b["text"] for row in rows for b in row}
        self.assertIn("toggle:ai_chronicle_enabled", buttons)
        self.assertIn("cycle:ai_schedule_mode", buttons)
        self.assertIn("cycle:ai_persona_style", buttons)
        self.assertIn("action:prompt_openrouter_key", buttons)
        self.assertIn("menu:main", buttons)
        self.assertEqual(buttons["menu:main"], "« Назад")

        text = self.bot.format_settings_menu_html(subpage="chronicle")
        self.assertIn("Настройки ИИ-хроники Наувиса", text)

    def test_get_setting_subpage_helper(self):
        """Verifies get_setting_subpage properly maps settings fields to submenus."""
        self.assertEqual(TelegramBotService.get_setting_subpage("notify_host_start"), "notifications")
        self.assertEqual(TelegramBotService.get_setting_subpage("notify_guest_join"), "notifications")
        self.assertEqual(TelegramBotService.get_setting_subpage("silent_notifications"), "notifications")
        self.assertEqual(TelegramBotService.get_setting_subpage("live_dashboard_enabled"), "dashboard")
        self.assertEqual(TelegramBotService.get_setting_subpage("show_ping_in_dashboard"), "dashboard")
        self.assertEqual(TelegramBotService.get_setting_subpage("show_updated_time"), "dashboard")
        self.assertEqual(TelegramBotService.get_setting_subpage("dashboard_tz_offset"), "dashboard")
        self.assertEqual(TelegramBotService.get_setting_subpage("ai_chronicle_enabled"), "chronicle")
        self.assertEqual(TelegramBotService.get_setting_subpage("ai_schedule_mode"), "chronicle")
        self.assertEqual(TelegramBotService.get_setting_subpage("ai_persona_style"), "chronicle")
        self.assertEqual(TelegramBotService.get_setting_subpage("unknown_key"), "main")

    async def test_callback_navigation_to_submenus(self):
        """Verifies clicking menu:notifications, menu:dashboard, menu:chronicle navigates correctly."""
        # 1. Navigate to notifications
        cb_notif = {
            "id": "cb_nav_1",
            "from": {"id": 42},
            "message": {"message_id": 500, "chat": {"id": 99999}},
            "data": "menu:notifications",
        }
        res = await self.bot.handle_callback_query(cb_notif)
        self.assertTrue(res)

        edit_reqs = [r for r in self.mock_transport.requests if "editMessageText" in str(r.url)]
        self.assertGreater(len(edit_reqs), 0)
        last_body = edit_reqs[-1].read().decode("utf-8")
        self.assertIn("Настройки уведомлений", last_body)
        self.assertIn("toggle:notify_host_start", last_body)

        # 2. Navigate to dashboard
        cb_dash = {
            "id": "cb_nav_2",
            "from": {"id": 42},
            "message": {"message_id": 500, "chat": {"id": 99999}},
            "data": "menu:dashboard",
        }
        res = await self.bot.handle_callback_query(cb_dash)
        self.assertTrue(res)
        last_body = self.mock_transport.requests[-2].read().decode("utf-8")
        self.assertIn("Настройки закрепленного дашборда", last_body)

        # 3. Navigate back to main menu
        cb_back = {
            "id": "cb_nav_3",
            "from": {"id": 42},
            "message": {"message_id": 500, "chat": {"id": 99999}},
            "data": "menu:main",
        }
        res = await self.bot.handle_callback_query(cb_back)
        self.assertTrue(res)
        last_body = self.mock_transport.requests[-2].read().decode("utf-8")
        self.assertIn("Настройки Telegram-бота Factorio", last_body)

    async def test_callback_toggle_retains_submenu_context(self):
        """Verifies toggling a setting updates the message inside the submenu without ejecting user."""
        self.assertTrue(self.bot.settings.notify_host_start)

        cb_toggle = {
            "id": "cb_tog_1",
            "from": {"id": 42},
            "message": {"message_id": 500, "chat": {"id": 99999}},
            "data": "toggle:notify_host_start",
        }
        res = await self.bot.handle_callback_query(cb_toggle)
        self.assertTrue(res)
        self.assertFalse(self.bot.settings.notify_host_start)

        edit_reqs = [r for r in self.mock_transport.requests if "editMessageText" in str(r.url)]
        last_body = edit_reqs[-1].read().decode("utf-8")
        # Ensure user is still in the notifications submenu!
        self.assertIn("Настройки уведомлений", last_body)
        self.assertIn("Хост старт: [ВЫКЛ]", last_body)
        self.assertIn("« Назад", last_body)

    async def test_callback_cycle_retains_submenu_context(self):
        """Verifies cycling timezone stays inside dashboard submenu."""
        self.assertEqual(self.bot.settings.dashboard_tz_offset, 3)

        cb_cycle = {
            "id": "cb_cyc_1",
            "from": {"id": 42},
            "message": {"message_id": 500, "chat": {"id": 99999}},
            "data": "cycle:dashboard_tz_offset",
        }
        res = await self.bot.handle_callback_query(cb_cycle)
        self.assertTrue(res)

        edit_reqs = [r for r in self.mock_transport.requests if "editMessageText" in str(r.url)]
        last_body = edit_reqs[-1].read().decode("utf-8")
        # Ensure user is still in dashboard submenu!
        self.assertIn("Настройки закрепленного дашборда", last_body)
        self.assertIn("cycle:dashboard_tz_offset", last_body)
        self.assertIn("« Назад", last_body)

    def test_strict_zero_emojis_across_all_submenus(self):
        """Verifies strictly ZERO emojis exist in all headers, texts, and buttons across all submenus."""
        subpages = ["main", "notifications", "dashboard", "chronicle"]
        for sp in subpages:
            text = self.bot.format_settings_menu_html(subpage=sp)
            self.assertFalse(self.EMOJI_PATTERN.search(text), f"Emoji found in header of subpage {sp}: {text}")

            kb = self.bot.build_settings_keyboard(subpage=sp)
            for row in kb.get("inline_keyboard", []):
                for btn in row:
                    btn_text = btn.get("text", "")
                    self.assertFalse(
                        self.EMOJI_PATTERN.search(btn_text),
                        f"Emoji found in button on subpage {sp}: {btn_text}"
                    )


if __name__ == "__main__":
    unittest.main()
