"""
Comprehensive unit and integration tests for Task 034:
- Server guest ping metrics storage in session manager and /heartbeat API
- Backward compatibility and Pydantic validation (422)
- Telegram pinned dashboard guest list formatting and toggle in /settings
- Anti-jitter throttling for ping fluctuations
- /ping command output and Zero-Emoji Policy compliance
"""

import hashlib
import html
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List
import unittest
from unittest import mock

from fastapi.testclient import TestClient
import httpx

from src.server.api import create_app
from src.server.auth_manager import AuthManager
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager, SessionState
from src.server.storage import SaveStorageManager
from src.server.telegram_bot import (
    TelegramBotService,
    TelegramBotSettings,
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


class TestSessionManagerPingMetrics(unittest.TestCase):
    """Tests for SessionLockManager handling guests_metrics in heartbeat and register_guest."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.session_mgr = SessionLockManager(config=self.config)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_register_guest_default_metrics(self):
        """Verifies register_guest initializes default ping_ms=None, direct_p2p=True, mode='P2P'."""
        res = self.session_mgr.register_guest("Bob", "1.2.3.4", 34197)
        self.assertTrue(res.get("success"))
        guests = res.get("active_guests", [])
        self.assertEqual(len(guests), 1)
        self.assertEqual(guests[0]["name"], "Bob")
        self.assertIsNone(guests[0]["ping_ms"])
        self.assertTrue(guests[0]["direct_p2p"])
        self.assertEqual(guests[0]["mode"], "P2P")

    def test_heartbeat_updates_existing_guest_metrics(self):
        """Verifies heartbeat enriches existing registered guests with ping_ms, direct_p2p, mode."""
        claim_res = self.session_mgr.claim_host("Alice", "1.1.1.1")
        token = claim_res["session_token"]
        self.session_mgr.confirm_hosting(token, 34197)

        # Register guest Bob
        self.session_mgr.register_guest("Bob", "1.2.3.4", 34197)

        # Send heartbeat with Bob's metrics
        hb_res = self.session_mgr.heartbeat(
            session_token=token,
            guests_metrics=[
                {"name": "Bob", "ping_ms": 18.4, "direct_p2p": True, "mode": "P2P"},
            ],
        )
        self.assertTrue(hb_res.get("success"))
        guests = hb_res.get("active_guests", [])
        self.assertEqual(len(guests), 1)
        self.assertEqual(guests[0]["name"], "Bob")
        self.assertEqual(guests[0]["ping_ms"], 18.4)
        self.assertTrue(guests[0]["direct_p2p"])
        self.assertEqual(guests[0]["mode"], "P2P")

    def test_heartbeat_adds_new_guest_reported_by_bridge(self):
        """Verifies heartbeat adds guest discovered via P2P bridge if not already in active_guests."""
        claim_res = self.session_mgr.claim_host("Alice", "1.1.1.1")
        token = claim_res["session_token"]
        self.session_mgr.confirm_hosting(token, 34197)

        # Send heartbeat with Charlie (Relay mode)
        hb_res = self.session_mgr.heartbeat(
            session_token=token,
            guests_metrics=[
                {"name": "Charlie", "ping_ms": 64.2, "direct_p2p": False, "mode": "Relay"},
            ],
        )
        self.assertTrue(hb_res.get("success"))
        guests = hb_res.get("active_guests", [])
        self.assertEqual(len(guests), 1)
        self.assertEqual(guests[0]["name"], "Charlie")
        self.assertEqual(guests[0]["ping_ms"], 64.2)
        self.assertFalse(guests[0]["direct_p2p"])
        self.assertEqual(guests[0]["mode"], "Relay")

    def test_heartbeat_without_guests_metrics_preserves_guests(self):
        """Verifies heartbeat with guests_metrics=None does not wipe or mutate active_guests."""
        claim_res = self.session_mgr.claim_host("Alice", "1.1.1.1")
        token = claim_res["session_token"]
        self.session_mgr.confirm_hosting(token, 34197)
        self.session_mgr.register_guest("Bob", "1.2.3.4", 34197)

        hb_res = self.session_mgr.heartbeat(session_token=token, guests_metrics=None)
        self.assertTrue(hb_res.get("success"))
        guests = hb_res.get("active_guests", [])
        self.assertEqual(len(guests), 1)
        self.assertEqual(guests[0]["name"], "Bob")


class TestApiHeartbeatEndpoint(unittest.TestCase):
    """Tests for FastAPI /heartbeat endpoint schema validation and processing."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.auth_mgr = AuthManager(config=self.config)
        self.session_mgr = SessionLockManager(config=self.config)
        self.storage_mgr = SaveStorageManager(config=self.config)

        # Create device token via invite
        invite = self.auth_mgr.create_invite(ttl_seconds=3600)
        redeem = self.auth_mgr.redeem_invite(ticket=invite["ticket"], player_name="HostPlayer")
        self.device_token = redeem["device_token"]

        self.app = create_app(
            config=self.config,
            session_manager=self.session_mgr,
            storage=self.storage_mgr,
            auth_manager=self.auth_mgr,
        )
        self.client = TestClient(self.app)
        self.headers = {"X-Device-Token": self.device_token}

        # Host claims session
        claim_resp = self.client.post(
            "/claim",
            json={"player_name": "HostPlayer"},
            headers=self.headers,
        )
        self.assertEqual(claim_resp.status_code, 200)
        self.session_token = claim_resp.json()["session_token"]
        confirm_resp = self.client.post(
            "/confirm-hosting",
            json={"session_token": self.session_token, "udp_port": 34197},
            headers=self.headers,
        )
        self.assertEqual(confirm_resp.status_code, 200)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_heartbeat_with_valid_metrics(self):
        """Verifies valid guests_metrics are accepted and reflected in /status."""
        payload = {
            "session_token": self.session_token,
            "udp_port": 34197,
            "guests_metrics": [
                {
                    "name": "Alex",
                    "ping_ms": 17.8,
                    "direct_p2p": True,
                    "mode": "P2P",
                },
                {
                    "name": "Max",
                    "ping_ms": 63.5,
                    "direct_p2p": False,
                    "mode": "Relay",
                },
            ],
        }
        hb_resp = self.client.post("/heartbeat", json=payload, headers=self.headers)
        self.assertEqual(hb_resp.status_code, 200)

        # Check /status
        st_resp = self.client.get("/status", headers=self.headers)
        self.assertEqual(st_resp.status_code, 200)
        guests = st_resp.json()["active_guests"]
        self.assertEqual(len(guests), 2)
        names = {g["name"]: g for g in guests}
        self.assertIn("Alex", names)
        self.assertIn("Max", names)
        self.assertEqual(names["Alex"]["ping_ms"], 17.8)
        self.assertTrue(names["Alex"]["direct_p2p"])
        self.assertEqual(names["Alex"]["mode"], "P2P")
        self.assertEqual(names["Max"]["ping_ms"], 63.5)
        self.assertFalse(names["Max"]["direct_p2p"])
        self.assertEqual(names["Max"]["mode"], "Relay")

    def test_heartbeat_without_metrics_backward_compatible(self):
        """Verifies omitting guests_metrics field is 100% backward compatible."""
        payload = {
            "session_token": self.session_token,
            "udp_port": 34197,
        }
        hb_resp = self.client.post("/heartbeat", json=payload, headers=self.headers)
        self.assertEqual(hb_resp.status_code, 200)

    def test_heartbeat_rejects_negative_ping(self):
        """EDGE CASE: Negative ping_ms must fail Pydantic validation (422)."""
        payload = {
            "session_token": self.session_token,
            "guests_metrics": [
                {"name": "BadPing", "ping_ms": -5.0},
            ],
        }
        hb_resp = self.client.post("/heartbeat", json=payload, headers=self.headers)
        self.assertEqual(hb_resp.status_code, 422)

    def test_heartbeat_rejects_excessive_ping(self):
        """EDGE CASE: ping_ms > 9999.0 must fail Pydantic validation (422)."""
        payload = {
            "session_token": self.session_token,
            "guests_metrics": [
                {"name": "BadPing", "ping_ms": 50000.0},
            ],
        }
        hb_resp = self.client.post("/heartbeat", json=payload, headers=self.headers)
        self.assertEqual(hb_resp.status_code, 422)


class TestTelegramDashboardAndSettings(unittest.IsolatedAsyncioTestCase):
    """Tests for Telegram live dashboard formatting, settings toggles, and zero-emoji compliance."""

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

    async def asyncTearDown(self):
        await self.mock_client.aclose()
        self.tmp_dir.cleanup()

    def test_format_dashboard_with_ping_metrics_enabled(self):
        """Verifies multi-line format • Alex — 18 мс [P2P] and • Max — 64 мс [Relay]."""
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"
        self.session_mgr._session.active_guests = [
            {"name": "Alex", "ping_ms": 18.0, "direct_p2p": True, "mode": "P2P"},
            {"name": "Max", "ping_ms": 64.0, "direct_p2p": False, "mode": "Relay"},
            {"name": "Newbie", "ping_ms": None, "direct_p2p": True, "mode": "P2P"},
        ]
        self.bot.settings.show_ping_in_dashboard = True

        dashboard_html = self.bot.format_dashboard_html()
        self.assertIn("Гости (3):", dashboard_html)
        self.assertIn("• Alex — 18 мс [P2P]", dashboard_html)
        self.assertIn("• Max — 64 мс [Relay]", dashboard_html)
        self.assertIn("• Newbie — замер... [P2P]", dashboard_html)

    def test_format_dashboard_with_ping_disabled(self):
        """Verifies compact format 'Гости (2): Alex, Max' when show_ping_in_dashboard=False."""
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"
        self.session_mgr._session.active_guests = [
            {"name": "Alex", "ping_ms": 18.0, "direct_p2p": True, "mode": "P2P"},
            {"name": "Max", "ping_ms": 64.0, "direct_p2p": False, "mode": "Relay"},
        ]
        self.bot.settings.show_ping_in_dashboard = False

        dashboard_html = self.bot.format_dashboard_html()
        self.assertIn("Гости (2): Alex, Max", dashboard_html)
        self.assertNotIn("• Alex — 18 мс", dashboard_html)

    def test_format_dashboard_legacy_guests_backward_compatible(self):
        """Verifies compact format when active_guests have no ping/mode keys (old tests compatibility)."""
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"
        self.session_mgr._session.active_guests = [
            {"name": "LegacyBob"},
            {"name": "LegacyCharley"},
        ]
        self.bot.settings.show_ping_in_dashboard = True

        dashboard_html = self.bot.format_dashboard_html()
        self.assertIn("Гости (2): LegacyBob, LegacyCharley", dashboard_html)

    def test_format_dashboard_html_escapes_names(self):
        """EDGE CASE: HTML special characters in player nicknames must be safely escaped."""
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "Host<Safe>"
        self.session_mgr._session.active_guests = [
            {"name": "<script>alert(1)</script>", "ping_ms": 25.0, "direct_p2p": True, "mode": "P2P"},
        ]
        self.bot.settings.show_ping_in_dashboard = True

        dashboard_html = self.bot.format_dashboard_html()
        self.assertNotIn("<script>", dashboard_html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", dashboard_html)
        self.assertIn("Host&lt;Safe&gt;", dashboard_html)

    def test_zero_emoji_policy(self):
        """
        CRITICAL MANDATE: Zero emojis in dashboard, settings keyboard, and /ping output.
        """
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "Alice"
        self.session_mgr._session.active_guests = [
            {"name": "Bob", "ping_ms": 20.0, "direct_p2p": True, "mode": "P2P"},
        ]

        texts_to_check = [
            self.bot.format_dashboard_html(),
            self.bot.format_ping_report_html(),
            self.bot.format_settings_menu_html(),
        ]
        kb = self.bot.build_settings_keyboard()
        for row in kb.get("inline_keyboard", []):
            for btn in row:
                texts_to_check.append(btn.get("text", ""))

        for text in texts_to_check:
            for char in text:
                cp = ord(char)
                self.assertFalse(
                    (
                        0x1F300 <= cp <= 0x1FAFF
                        or 0x2600 <= cp <= 0x27BF
                        or 0xFE00 <= cp <= 0xFE0F
                        or 0x1F900 <= cp <= 0x1F9FF
                    ),
                    f"Forbidden emoji character detected: '{char}' (U+{cp:04X}) in text: {text[:50]}...",
                )

    async def test_settings_toggle_show_ping_in_dashboard(self):
        """Verifies callback query toggle:show_ping_in_dashboard flips value and updates dashboard."""
        self.bot.settings.show_ping_in_dashboard = True

        cb_query = {
            "id": "cb_toggle_ping",
            "data": "toggle:show_ping_in_dashboard",
            "from": {"id": 111},
            "message": {"message_id": 99, "chat": {"id": 99999}},
        }
        with mock.patch.object(self.bot, "update_dashboard", new_callable=mock.AsyncMock) as mock_dash:
            mock_dash.return_value = {"status": "ok"}
            handled = await self.bot.handle_callback_query(cb_query)
            self.assertTrue(handled)
            self.assertFalse(self.bot.settings.show_ping_in_dashboard)
            mock_dash.assert_called_once_with(force=True)


class TestTelegramPingCommand(unittest.IsolatedAsyncioTestCase):
    """Tests for the /ping diagnostic command."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.transport)
        self.session_mgr = SessionLockManager(config=self.config)
        self.bot = TelegramBotService(
            config=self.config,
            session_manager=self.session_mgr,
            client=self.mock_client,
        )
        self.bot.settings.bot_token = "123:test"
        self.bot.settings.chat_id = 99999

    async def asyncTearDown(self):
        await self.mock_client.aclose()
        self.tmp_dir.cleanup()

    async def test_ping_command_in_idle_state(self):
        """Verifies /ping report when server is idle."""
        self.session_mgr._session.state = SessionState.IDLE.value

        msg = {
            "message_id": 50,
            "chat": {"id": 99999},
            "from": {"id": 123},
            "text": "/ping",
        }
        handled = await self.bot.handle_message(msg)
        self.assertTrue(handled)

        # Check sent message content
        self.assertEqual(len(self.transport.calls), 1)
        sent_text = self.transport.calls[0]["json"]["text"]
        self.assertIn("Сетевая диагностика (P2P / Relay)", sent_text)
        self.assertIn("[Свободен]", sent_text)
        self.assertIn("Координатор VPS: <b>Онлайн (&lt;1 мс)</b>", sent_text)

    async def test_ping_command_in_hosting_active_state(self):
        """Verifies /ping report during active game with connected guests."""
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "Leader"
        self.session_mgr._session.host_udp_port = 34197
        self.session_mgr._session.active_guests = [
            {"name": "Bob", "ping_ms": 15.2, "direct_p2p": True, "mode": "P2P"},
            {"name": "Max", "ping_ms": 72.8, "direct_p2p": False, "mode": "Relay"},
        ]

        msg = {
            "message_id": 51,
            "chat": {"id": 99999},
            "from": {"id": 123},
            "text": "/ping",
        }
        handled = await self.bot.handle_message(msg)
        self.assertTrue(handled)

        sent_text = self.transport.calls[0]["json"]["text"]
        self.assertIn("Хост лобби: <b>Leader</b> (UDP порт: 34197)", sent_text)
        self.assertIn("Подключенные гости (2):", sent_text)
        self.assertIn("• <b>Bob</b> — 15 мс [P2P]", sent_text)
        self.assertIn("• <b>Max</b> — 73 мс [Relay]", sent_text)
        self.assertIn("[P2P] — прямое соединение", sent_text)


class TestAntiJitterThrottling(unittest.IsolatedAsyncioTestCase):
    """Tests for anti-flood jitter throttling in update_dashboard."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.transport)
        self.session_mgr = SessionLockManager(config=self.config)
        self.bot = TelegramBotService(
            config=self.config,
            session_manager=self.session_mgr,
            client=self.mock_client,
        )
        self.bot.settings.bot_token = "123:test"
        self.bot.settings.chat_id = 99999
        self.bot.settings.pinned_message_id = 101

    async def asyncTearDown(self):
        await self.mock_client.aclose()
        self.tmp_dir.cleanup()

    async def test_ping_jitter_is_throttled_within_25_seconds(self):
        """
        CRITICAL REQUIREMENT:
        Two consecutive calls to update_dashboard(force=False) where only ping ms changed
        (e.g., 18 ms -> 19 ms) within < 25s must issue ONLY ONE editMessageText call.
        """
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"
        self.session_mgr._session.active_guests = [
            {"name": "Alex", "ping_ms": 18.0, "direct_p2p": True, "mode": "P2P"},
        ]

        # 1. First call -> edits message
        res1 = await self.bot.update_dashboard(force=False)
        self.assertIsNotNone(res1)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertIn("18 мс", self.transport.calls[0]["json"]["text"])

        # 2. Ping jiggles to 19 ms 1 second later
        self.session_mgr._session.active_guests[0]["ping_ms"] = 19.0
        res2 = await self.bot.update_dashboard(force=False)
        # Should be throttled and return None, making NO network call
        self.assertIsNone(res2)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_structural_change_bypasses_jitter_throttling(self):
        """
        When structural content changes (new player joins or tunnel switches to Relay),
        update_dashboard must update immediately even if < 25s elapsed.
        """
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"
        self.session_mgr._session.active_guests = [
            {"name": "Alex", "ping_ms": 18.0, "direct_p2p": True, "mode": "P2P"},
        ]

        # 1. Initial edit
        res1 = await self.bot.update_dashboard(force=False)
        self.assertIsNotNone(res1)
        self.assertEqual(len(self.transport.calls), 1)

        # 2. Player joins
        self.session_mgr._session.active_guests.append(
            {"name": "Max", "ping_ms": 60.0, "direct_p2p": False, "mode": "Relay"}
        )
        res2 = await self.bot.update_dashboard(force=False)
        self.assertIsNotNone(res2)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertIn("Max", self.transport.calls[1]["json"]["text"])

    async def test_force_bypasses_jitter_throttling(self):
        """Verifies force=True always executes editMessageText regardless of elapsed time."""
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"
        self.session_mgr._session.active_guests = [
            {"name": "Alex", "ping_ms": 18.0, "direct_p2p": True, "mode": "P2P"},
        ]

        await self.bot.update_dashboard(force=False)
        self.assertEqual(len(self.transport.calls), 1)

        # Force update even with identical content
        res_forced = await self.bot.update_dashboard(force=True)
        self.assertIsNotNone(res_forced)
        self.assertEqual(len(self.transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
