"""
Unit and integration tests for AI Chronicle event cooldown settings and filtering (Task 042).
Verifies:
1. TelegramBotSettings serialization and deserialization of ai_event_cooldown_sec.
2. Chronicle submenu inline keyboard includes cycle:ai_event_cooldown_sec button.
3. Cycling ai_event_cooldown_sec via callback query (300 -> 900 -> 1800 -> 3600 -> 300).
4. should_process_report enforces cooldown for important_event while allowing hourly and exit summaries.
5. OpenRouterChronicleService tracks last_event_post_ts and records posts.
6. Strict Zero-Emoji compliance across all generated texts and keyboards.
"""

from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

import httpx

from src.server.config import ServerConfig
from src.server.ai_chronicle import OpenRouterChronicleService, should_process_report
from src.server.telegram_bot import (
    AI_EVENT_COOLDOWN_CHOICES,
    TelegramBotService,
    TelegramBotSettings,
)


class MockTransport(httpx.AsyncBaseTransport):
    """Captures HTTP requests and returns mock Telegram responses."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url_str = str(request.url)

        if "editMessageText" in url_str:
            return httpx.Response(
                status_code=200,
                json={"ok": True, "result": {"message_id": 500, "text": "edited"}},
            )
        if "answerCallbackQuery" in url_str:
            return httpx.Response(status_code=200, json={"ok": True, "result": True})
        if "sendMessage" in url_str:
            return httpx.Response(
                status_code=200,
                json={"ok": True, "result": {"message_id": 999, "text": "sent"}},
            )

        return httpx.Response(status_code=200, json={"ok": True, "result": {}})


class TestChronicleCooldownSettings(unittest.TestCase):
    """Synchronous test cases for serialization and cooldown logic."""

    EMOJI_PATTERN = re.compile(
        r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]"
    )

    def test_settings_ai_event_cooldown_serialization(self):
        """Verifies default, serialization, and deserialization of ai_event_cooldown_sec."""
        settings = TelegramBotSettings()
        self.assertEqual(settings.ai_event_cooldown_sec, 1800)

        # Test valid choices
        for sec, _ in AI_EVENT_COOLDOWN_CHOICES:
            s = TelegramBotSettings.from_dict({"ai_event_cooldown_sec": sec})
            self.assertEqual(s.ai_event_cooldown_sec, sec)

        # Test fallback on invalid value
        s_invalid = TelegramBotSettings.from_dict({"ai_event_cooldown_sec": 42})
        self.assertEqual(s_invalid.ai_event_cooldown_sec, 1800)

        s_str_invalid = TelegramBotSettings.from_dict({"ai_event_cooldown_sec": "garbage"})
        self.assertEqual(s_str_invalid.ai_event_cooldown_sec, 1800)

    def test_should_process_report_with_cooldown(self):
        """Verifies cooldown filtering in should_process_report."""
        settings = TelegramBotSettings(
            ai_chronicle_enabled=True,
            ai_schedule_mode="hourly_and_events",
            ai_event_cooldown_sec=300,  # 5 minutes
        )

        now = 10000.0

        # Case 1: First event ever (last_event_ts is None or 0) -> Allowed
        self.assertTrue(
            should_process_report(
                "important_event",
                settings=settings,
                last_event_ts=None,
                current_ts=now,
            )
        )
        self.assertTrue(
            should_process_report(
                "important_event",
                settings=settings,
                last_event_ts=0.0,
                current_ts=now,
            )
        )

        # Case 2: Event arrived too quickly (200s < 300s) -> Rejected
        self.assertFalse(
            should_process_report(
                "important_event",
                settings=settings,
                last_event_ts=now - 200.0,
                current_ts=now,
            )
        )

        # Case 3: Event arrived after cooldown expired (301s >= 300s) -> Allowed
        self.assertTrue(
            should_process_report(
                "important_event",
                settings=settings,
                last_event_ts=now - 301.0,
                current_ts=now,
            )
        )

        # Case 4: Cooldown does not affect hourly_summary or session_summary
        self.assertTrue(
            should_process_report(
                "hourly_summary",
                settings=settings,
                last_event_ts=now - 50.0,
                current_ts=now,
            )
        )
        self.assertTrue(
            should_process_report(
                "session_summary",
                settings=settings,
                last_event_ts=now - 10.0,
                current_ts=now,
            )
        )

        # Case 5: When schedule mode is hourly_and_exit, important_event is rejected regardless of cooldown
        settings.ai_schedule_mode = "hourly_and_exit"
        self.assertFalse(
            should_process_report(
                "important_event",
                settings=settings,
                last_event_ts=now - 1000.0,
                current_ts=now,
            )
        )

    def test_chronicle_service_tracks_last_event_post_ts(self):
        """Verifies OpenRouterChronicleService tracks and updates last_event_post_ts."""
        service = OpenRouterChronicleService()
        self.assertEqual(service.last_event_post_ts, 0.0)

        ts = 12345.67
        service.record_event_post(ts)
        self.assertEqual(service.last_event_post_ts, ts)

        now_before = time.time()
        service.record_event_post()
        now_after = time.time()
        self.assertTrue(now_before <= service.last_event_post_ts <= now_after)


class TestChronicleCooldownAsync(unittest.IsolatedAsyncioTestCase):
    """Asynchronous tests for TelegramBotService and menu interaction."""

    EMOJI_PATTERN = re.compile(
        r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]"
    )

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_path = Path(self.temp_dir.name)
        self.config = ServerConfig(storage_dir=self.storage_path)
        self.mock_transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.mock_transport)

        self.bot = TelegramBotService(
            config=self.config,
            client=self.mock_client,
        )
        self.bot.settings.bot_token = "test_token"
        self.bot.settings.chat_id = "12345"
        self.bot.settings.admin_user_ids = [42]
        self.bot.settings.ai_chronicle_enabled = True
        self.bot.settings.ai_schedule_mode = "hourly_and_events"
        self.bot.settings.ai_event_cooldown_sec = 1800

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        await self.mock_client.aclose()
        self.temp_dir.cleanup()

    def test_chronicle_keyboard_has_cooldown_button(self):
        """Verifies build_settings_keyboard contains cycle:ai_event_cooldown_sec with correct label."""
        kb = self.bot.build_settings_keyboard(subpage="chronicle")
        rows = kb.get("inline_keyboard", [])
        buttons = {b["callback_data"]: b["text"] for row in rows for b in row}

        self.assertIn("cycle:ai_event_cooldown_sec", buttons)
        self.assertEqual(buttons["cycle:ai_event_cooldown_sec"], "Кулдаун: 30 мин")

        # Zero emoji check
        for btn_text in buttons.values():
            self.assertFalse(self.EMOJI_PATTERN.search(btn_text))

        text = self.bot.format_settings_menu_html(subpage="chronicle")
        self.assertIn("Кулдаун событий", text)
        self.assertFalse(self.EMOJI_PATTERN.search(text))

    async def test_cycle_ai_event_cooldown_transitions(self):
        """Verifies cycling through 300 -> 900 -> 1800 -> 3600 -> 300 via callback query."""
        cb_query = {
            "id": "cb_cd_cycle_1",
            "from": {"id": 42},
            "data": "cycle:ai_event_cooldown_sec",
            "message": {"message_id": 500, "chat": {"id": 12345}},
        }

        # Initial: 1800 -> next should be 3600
        await self.bot.handle_callback_query(cb_query)
        self.assertEqual(self.bot.settings.ai_event_cooldown_sec, 3600)

        # 3600 -> next should be 300 (5 min)
        await self.bot.handle_callback_query(cb_query)
        self.assertEqual(self.bot.settings.ai_event_cooldown_sec, 300)

        # 300 -> next should be 900 (15 min)
        await self.bot.handle_callback_query(cb_query)
        self.assertEqual(self.bot.settings.ai_event_cooldown_sec, 900)

        # 900 -> next should be 1800 (30 min)
        await self.bot.handle_callback_query(cb_query)
        self.assertEqual(self.bot.settings.ai_event_cooldown_sec, 1800)

        # Verify submenu retention (user remains in chronicle submenu)
        edit_reqs = [r for r in self.mock_transport.requests if "editMessageText" in str(r.url)]
        self.assertGreater(len(edit_reqs), 0)
        last_body = edit_reqs[-1].read().decode("utf-8")
        self.assertIn("cycle:ai_event_cooldown_sec", last_body)
        self.assertIn("Настройки ИИ-хроники", last_body)


if __name__ == "__main__":
    unittest.main()
