"""
Unit and integration tests for interactive connection guide in Telegram Bot (Task 039).
Verifies:
1. Dashboard keyboard layout includes 'Как войти в игру' button.
2. Formatted guide texts for menu, Windows, and macOS with accurate copyable code blocks.
3. Callback query navigation (guide:menu, guide:windows, guide:macos, guide:close).
4. Pinned dashboard isolation (guide sent as new message when clicked from pinned dashboard).
5. Bot commands (/join, /guide, /help) routing and register_commands listing.
6. Non-admin accessibility (guests/friends can use guide buttons and commands).
7. Strict Zero-Emoji compliance across all generated texts, buttons, and commands.
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
    """Mocks outgoing HTTP requests to Telegram Bot API."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

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

        if "deleteMessage" in url:
            return httpx.Response(200, json={"ok": True, "result": True})

        if "answerCallbackQuery" in url:
            return httpx.Response(200, json={"ok": True, "result": True})

        if "pinChatMessage" in url:
            return httpx.Response(200, json={"ok": True, "result": True})

        return httpx.Response(200, json={"ok": True, "result": {}})


class TestTelegramJoinGuide(unittest.IsolatedAsyncioTestCase):
    """Test suite for interactive connection guide in TelegramBotService."""

    EMOJI_PATTERN = re.compile(
        "[\U0001f300-\U0001f5ff\U0001f600-\U0001f64f\U0001f680-\U0001f6ff"
        "\U0001f700-\U0001f77f\U0001f780-\U0001f7ff\U0001f800-\U0001f8ff"
        "\U0001f900-\U0001f9ff\U0001fa00-\U0001faff\U00002702-\U000027b0"
        "\U000024c2-\U0001f251\U0000200d\U0000fe0f\U0000fe0e]"
    )

    async def asyncSetUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_join_guide_"))
        self.config = ServerConfig(storage_dir=self.temp_dir / "data")
        self.mock_transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.mock_transport)
        self.bot = TelegramBotService(config=self.config, client=self.mock_client)
        self.bot.settings.bot_token = "123456:TEST_TOKEN"
        self.bot.settings.chat_id = 12345
        self.bot.settings.admin_user_ids = [42]  # Admin is only user 42
        self.bot.settings.pinned_message_id = 777

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        await self.mock_client.aclose()

    def test_dashboard_keyboard_contains_guide_button(self):
        """Verifies dashboard keyboard has 'Как войти в игру' with callback 'guide:menu'."""
        kb = self.bot.build_dashboard_keyboard()
        buttons = {b["callback_data"]: b["text"] for row in kb["inline_keyboard"] for b in row}
        self.assertIn("guide:menu", buttons)
        self.assertEqual(buttons["guide:menu"], "Как войти в игру")

    def test_format_join_guide_html_content(self):
        """Verifies HTML formatted guide text includes copyable code blocks and key instructions."""
        # Menu
        text_menu = self.bot.format_join_guide_html(os_type="menu")
        self.assertIn("Как войти в совместную игру Factorio", text_menu)
        self.assertIn("Выберите вашу операционную систему", text_menu)

        # Windows
        text_win = self.bot.format_join_guide_html(os_type="windows")
        self.assertIn("Инструкция по входу в игру (Windows)", text_win)
        self.assertIn("PowerShell от имени Администратора", text_win)
        self.assertIn("<code>irm https://raw.githubusercontent.com/GreyPikachu/Vaultorio/main/deploy/install.ps1 | iex</code>", text_win)
        self.assertIn("Код Дружбы", text_win)
        self.assertIn("127.0.0.1:34198", text_win)
        self.assertIn("[Cloud]", text_win)

        # macOS
        text_mac = self.bot.format_join_guide_html(os_type="macos")
        self.assertIn("Инструкция по входу в игру (macOS)", text_mac)
        self.assertIn("Терминал", text_mac)
        self.assertIn("<code>curl -fsSL https://raw.githubusercontent.com/GreyPikachu/Vaultorio/main/deploy/install.sh | bash</code>", text_mac)
        self.assertIn("Код Дружбы", text_mac)
        self.assertIn("127.0.0.1:34198", text_mac)

    def test_build_join_guide_keyboard_structure(self):
        """Verifies keyboard buttons and callbacks for menu, windows, and macos screens."""
        # Menu keyboard
        kb_menu = self.bot.build_join_guide_keyboard(os_type="menu")
        btn_map_menu = {b["callback_data"]: b["text"] for row in kb_menu["inline_keyboard"] for b in row}
        self.assertIn("guide:windows", btn_map_menu)
        self.assertIn("guide:macos", btn_map_menu)
        self.assertIn("guide:close", btn_map_menu)

        # Windows keyboard
        kb_win = self.bot.build_join_guide_keyboard(os_type="windows")
        btn_map_win = {b["callback_data"]: b["text"] for row in kb_win["inline_keyboard"] for b in row}
        self.assertIn("guide:macos", btn_map_win)
        self.assertIn("guide:menu", btn_map_win)
        self.assertIn("guide:close", btn_map_win)

        # macOS keyboard
        kb_mac = self.bot.build_join_guide_keyboard(os_type="macos")
        btn_map_mac = {b["callback_data"]: b["text"] for row in kb_mac["inline_keyboard"] for b in row}
        self.assertIn("guide:windows", btn_map_mac)
        self.assertIn("guide:menu", btn_map_mac)
        self.assertIn("guide:close", btn_map_mac)

    async def test_callback_from_pinned_dashboard_sends_new_message(self):
        """Verifies clicking 'Как войти в игру' from pinned dashboard sends a NEW message, protecting the pin."""
        cb_query = {
            "id": "cb_pinned_guide",
            "from": {"id": 999},  # Guest user (not admin)
            "message": {"message_id": 777, "chat": {"id": 12345}},  # 777 is pinned_message_id!
            "data": "guide:menu",
        }
        res = await self.bot.handle_callback_query(cb_query)
        self.assertTrue(res)

        # Ensure sendMessage was used and NOT editMessageText
        send_reqs = [r for r in self.mock_transport.requests if "sendMessage" in str(r.url)]
        edit_reqs = [r for r in self.mock_transport.requests if "editMessageText" in str(r.url)]

        self.assertEqual(len(send_reqs), 1)
        self.assertEqual(len(edit_reqs), 0)

        body = send_reqs[0].read().decode("utf-8")
        self.assertIn("Как войти в совместную игру", body)

    async def test_callback_navigation_within_guide_edits_in_place(self):
        """Verifies switching between Windows, macOS, and Menu in an existing guide message edits in place."""
        cb_win = {
            "id": "cb_edit_win",
            "from": {"id": 999},
            "message": {"message_id": 888, "chat": {"id": 12345}},  # 888 is NOT pinned
            "data": "guide:windows",
        }
        res = await self.bot.handle_callback_query(cb_win)
        self.assertTrue(res)

        edit_reqs = [r for r in self.mock_transport.requests if "editMessageText" in str(r.url)]
        self.assertEqual(len(edit_reqs), 1)
        body = edit_reqs[0].read().decode("utf-8")
        self.assertIn("PowerShell", body)
        self.assertIn("irm https://raw.githubusercontent.com", body)

    async def test_callback_guide_close_deletes_message(self):
        """Verifies clicking 'Закрыть' calls deleteMessage."""
        cb_close = {
            "id": "cb_close_guide",
            "from": {"id": 999},
            "message": {"message_id": 888, "chat": {"id": 12345}},
            "data": "guide:close",
        }
        res = await self.bot.handle_callback_query(cb_close)
        self.assertTrue(res)

        del_reqs = [r for r in self.mock_transport.requests if "deleteMessage" in str(r.url)]
        self.assertEqual(len(del_reqs), 1)

    async def test_commands_routing_join_guide_help(self):
        """Verifies /join, /guide, and /help send the guide message for regular players."""
        for cmd in ("/join", "/guide", "/help"):
            msg = {
                "message_id": 100,
                "from": {"id": 555},  # Non-admin
                "chat": {"id": 12345},
                "text": cmd,
            }
            res = await self.bot.handle_message(msg)
            self.assertTrue(res)

        send_reqs = [r for r in self.mock_transport.requests if "sendMessage" in str(r.url)]
        self.assertEqual(len(send_reqs), 3)

    async def test_register_commands_includes_join_and_guide(self):
        """Verifies register_commands registers /join and /guide with descriptions."""
        res = await self.bot.register_commands()
        self.assertTrue(res)

        set_cmd_reqs = [r for r in self.mock_transport.requests if "setMyCommands" in str(r.url)]
        self.assertEqual(len(set_cmd_reqs), 1)
        body = set_cmd_reqs[0].read().decode("utf-8")
        self.assertIn('"command":"join"', body)
        self.assertIn('"command":"guide"', body)

    def test_strict_zero_emojis_across_guide(self):
        """Verifies strictly zero emojis in all guide texts and keyboards."""
        for os_type in ("menu", "windows", "macos"):
            text = self.bot.format_join_guide_html(os_type=os_type)
            self.assertFalse(self.EMOJI_PATTERN.search(text), f"Emoji found in guide text ({os_type}): {text}")

            kb = self.bot.build_join_guide_keyboard(os_type=os_type)
            for row in kb.get("inline_keyboard", []):
                for btn in row:
                    self.assertFalse(
                        self.EMOJI_PATTERN.search(btn["text"]),
                        f"Emoji found in guide button ({os_type}): {btn['text']}"
                    )

        kb_dash = self.bot.build_dashboard_keyboard()
        for row in kb_dash.get("inline_keyboard", []):
            for btn in row:
                self.assertFalse(
                    self.EMOJI_PATTERN.search(btn["text"]),
                    f"Emoji found in dash button: {btn['text']}"
                )


if __name__ == "__main__":
    unittest.main()
