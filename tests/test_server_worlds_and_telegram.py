"""
Comprehensive unit and integration tests for Task 036:
- World names ([Cloud]), display label helper, and sanitization
- Inheritance of world_name across autosaves and update on unchanged SHA
- Server world archive management (archive_and_remove_current_world, list_saved_worlds, activate_archived_world)
- Auto-archiving active world before activating a new one to prevent data loss
- HTTP REST API endpoints (GET /status, POST /upload-save, GET /worlds, POST /worlds/activate, POST /reset-world)
- Telegram bot worlds menu (format_worlds_menu_html, build_worlds_menu_keyboard, /worlds command)
- Pinned live dashboard protection (never overwritten by worlds menu callback)
- State checks (idle vs busy) for world archiving and activation
- Strict Zero-Emoji Policy compliance
"""

from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional
import unittest
from unittest import mock
import zipfile

from fastapi.testclient import TestClient
import httpx

from src.server.api import create_app
from src.server.auth_manager import AuthManager
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager, SessionState
from src.server.storage import (
    InvalidSaveArchiveError,
    SaveStorageManager,
    format_cloud_world_label,
    sanitize_world_name,
)
from src.server.telegram_bot import TelegramBotService, TelegramBotSettings


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


def create_dummy_factorio_zip(file_path: Path, marker: str = "world_v1") -> Path:
    """Helper creating a syntactically valid Factorio save zip archive."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(file_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test_world/level.dat", f"Binary Level Data: {marker}".encode("utf-8") * 5)
        zf.writestr("test_world/info.json", json.dumps({"name": marker, "version": "2.0"}))
        zf.writestr("test_world/control.lua", "-- Lua script logic\n")
    return file_path


class TestStorageWorldNamesAndArchive(unittest.TestCase):
    """Unit tests for SaveStorageManager world naming, archives index, and activation."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.storage = SaveStorageManager(self.config)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_sanitize_world_name_and_format_label(self):
        """Verifies normalization of world names and [Cloud] labels."""
        self.assertEqual(sanitize_world_name("Nauvis_Base.zip"), "Nauvis_Base")
        self.assertEqual(sanitize_world_name("Nauvis_Base.ZIP"), "Nauvis_Base")
        self.assertEqual(sanitize_world_name("Nauvis_Base [Cloud]"), "Nauvis_Base")
        self.assertEqual(sanitize_world_name("  Nauvis_Base  "), "Nauvis_Base")
        self.assertEqual(sanitize_world_name(""), "")
        self.assertEqual(sanitize_world_name(None), "")

        # Length clamping to 48 characters
        long_name = "A" * 60
        self.assertEqual(len(sanitize_world_name(long_name)), 48)

        # format_cloud_world_label
        self.assertEqual(format_cloud_world_label("Nauvis_Base"), "Nauvis_Base [Cloud]")
        self.assertEqual(format_cloud_world_label("Nauvis_Base [Cloud]"), "Nauvis_Base [Cloud]")
        self.assertEqual(format_cloud_world_label(""), "cloud_world [Cloud]")
        self.assertEqual(format_cloud_world_label(None), "cloud_world [Cloud]")

    def test_save_new_world_atomic_with_world_name(self):
        """Initial upload saves custom world_name and display_world_name in metadata."""
        temp_zip = Path(self.tmp_dir.name) / "temp_upload.zip"
        create_dummy_factorio_zip(temp_zip, marker="Nauvis_1")

        meta = self.storage.save_new_world_atomic(
            temp_file_path=temp_zip,
            uploaded_by="Alice",
            world_name="Nauvis_Base",
        )
        self.assertTrue(meta["exists"])
        self.assertEqual(meta["world_name"], "Nauvis_Base")
        self.assertEqual(meta["display_world_name"], "Nauvis_Base [Cloud]")
        self.assertEqual(meta["uploaded_by"], "Alice")
        self.assertEqual(meta["version_counter"], 1)

    def test_save_new_world_atomic_autosave_inherits_world_name(self):
        """Subsequent autosave without world_name inherits world_name from prev_metadata."""
        # 1. Initial world save
        temp1 = Path(self.tmp_dir.name) / "temp1.zip"
        create_dummy_factorio_zip(temp1, marker="Nauvis_1")
        self.storage.save_new_world_atomic(
            temp_file_path=temp1,
            uploaded_by="Alice",
            world_name="SpaceAge_Base",
        )

        # 2. Autosave with different data and no world_name parameter
        temp2 = Path(self.tmp_dir.name) / "temp2.zip"
        create_dummy_factorio_zip(temp2, marker="Nauvis_2_autosave")
        meta2 = self.storage.save_new_world_atomic(
            temp_file_path=temp2,
            uploaded_by="Alice",
            world_name=None,
        )
        self.assertEqual(meta2["world_name"], "SpaceAge_Base")
        self.assertEqual(meta2["display_world_name"], "SpaceAge_Base [Cloud]")
        self.assertEqual(meta2["version_counter"], 2)

    def test_save_new_world_atomic_unchanged_sha_updates_world_name(self):
        """When SHA-256 matches but world_name is renamed, metadata updates."""
        temp1 = Path(self.tmp_dir.name) / "temp1.zip"
        create_dummy_factorio_zip(temp1, marker="SameData")
        self.storage.save_new_world_atomic(
            temp_file_path=temp1,
            uploaded_by="Alice",
            world_name="Old_Name",
        )

        # Upload same content but rename to New_Name
        temp2 = Path(self.tmp_dir.name) / "temp2.zip"
        create_dummy_factorio_zip(temp2, marker="SameData")
        meta2 = self.storage.save_new_world_atomic(
            temp_file_path=temp2,
            uploaded_by="Alice",
            world_name="New_Name",
        )
        self.assertEqual(meta2["status"], "unchanged")
        self.assertEqual(meta2["world_name"], "New_Name")
        self.assertEqual(meta2["display_world_name"], "New_Name [Cloud]")

    def test_archive_and_remove_current_world(self):
        """Archives active world with custom name into backups and clears safe."""
        temp_zip = Path(self.tmp_dir.name) / "temp_upload.zip"
        create_dummy_factorio_zip(temp_zip, marker="WorldData")
        self.storage.save_new_world_atomic(
            temp_file_path=temp_zip,
            uploaded_by="Bob",
            world_name="Gleba_Outpost",
        )

        # Archive world
        res = self.storage.archive_and_remove_current_world(deleted_by="Bob")
        self.assertTrue(res["success"])
        self.assertFalse(res["exists"])
        self.assertEqual(res["world_name"], "Gleba_Outpost")
        self.assertIn("Gleba_Outpost [Cloud]", res["display_world_name"])
        self.assertIn("Gleba_Outpost", res["archive_name"])

        # Check that current save is unlinked
        self.assertFalse(self.config.current_save_path.exists())

        # Check metadata
        meta = self.storage._read_metadata_file_locked()
        self.assertFalse(meta.get("exists"))
        self.assertEqual(meta.get("last_archived_world_name"), "Gleba_Outpost")

        # Check archives_index.json
        index_file = self.config.backups_dir / "archives_index.json"
        self.assertTrue(index_file.exists())
        with open(index_file, "r", encoding="utf-8") as f:
            index_data = json.load(f)
        self.assertIn(res["archive_name"], index_data)
        self.assertEqual(index_data[res["archive_name"]]["world_name"], "Gleba_Outpost")

    def test_list_saved_worlds_and_activate_archived_world(self):
        """Tests full lifecycle: active world, archive list, and activating archived world."""
        # 1. Create World A and archive it
        temp_a = Path(self.tmp_dir.name) / "world_a.zip"
        create_dummy_factorio_zip(temp_a, marker="WorldA")
        self.storage.save_new_world_atomic(temp_a, uploaded_by="Alice", world_name="World_Alpha")
        res_a = self.storage.archive_and_remove_current_world(deleted_by="Alice")
        arch_a_name = res_a["archive_name"]

        # 2. Check list_saved_worlds when empty
        worlds_empty = self.storage.list_saved_worlds()
        self.assertIsNone(worlds_empty["active_world"])
        self.assertEqual(len(worlds_empty["archived_worlds"]), 1)
        self.assertEqual(worlds_empty["archived_worlds"][0]["world_name"], "World_Alpha")

        # 3. Create World B as active
        temp_b = Path(self.tmp_dir.name) / "world_b.zip"
        create_dummy_factorio_zip(temp_b, marker="WorldB")
        self.storage.save_new_world_atomic(temp_b, uploaded_by="Bob", world_name="World_Beta")

        worlds_with_b = self.storage.list_saved_worlds()
        self.assertIsNotNone(worlds_with_b["active_world"])
        self.assertEqual(worlds_with_b["active_world"]["world_name"], "World_Beta")
        self.assertEqual(len(worlds_with_b["archived_worlds"]), 1)

        # 4. Activate World A while World B is currently active
        # ZERO DATA LOSS: World B should be automatically archived first!
        new_active = self.storage.activate_archived_world(archive_name=arch_a_name, activated_by="Admin")
        self.assertTrue(new_active["exists"])
        self.assertEqual(new_active["world_name"], "World_Alpha")
        self.assertEqual(new_active["display_world_name"], "World_Alpha [Cloud]")

        # Verify current active world on disk is World A
        self.assertTrue(self.config.current_save_path.exists())

        # Verify World B was auto-archived and is now in archived_worlds
        worlds_after = self.storage.list_saved_worlds()
        self.assertEqual(worlds_after["active_world"]["world_name"], "World_Alpha")
        arch_names = [w["world_name"] for w in worlds_after["archived_worlds"]]
        self.assertIn("World_Beta", arch_names)
        # World A should no longer be in archived_worlds since it is now active
        self.assertNotIn("World_Alpha", arch_names)

    def test_activate_archived_world_validation_errors(self):
        """Verifies validation of archive_name: path traversal, missing file, corrupt file."""
        # Missing file
        with self.assertRaises(FileNotFoundError):
            self.storage.activate_archived_world("non_existent_archive.zip")

        # Path traversal
        with self.assertRaises(ValueError):
            self.storage.activate_archived_world("../outside.zip")

        with self.assertRaises(ValueError):
            self.storage.activate_archived_world("dir/archive.zip")

        # Corrupt file
        bad_archive = self.config.backups_dir / "archived_corrupt.zip"
        bad_archive.write_bytes(b"not a valid zip file")
        with self.assertRaises(InvalidSaveArchiveError):
            self.storage.activate_archived_world("archived_corrupt.zip")


class TestAPIWorldEndpoints(unittest.TestCase):
    """Integration tests for FastAPI endpoints: /status, /upload-save, /worlds, /worlds/activate, /reset-world."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(
            storage_dir=Path(self.tmp_dir.name),
            max_backups=3,
            max_upload_size_bytes=5 * 1024 * 1024,
        )
        self.storage = SaveStorageManager(self.config)
        self.session_mgr = SessionLockManager(self.config)
        self.transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.transport)
        self.bot = TelegramBotService(
            config=self.config,
            session_manager=self.session_mgr,
            storage_manager=self.storage,
            client=self.mock_client,
        )
        self.bot.settings.bot_token = "123:test"
        self.bot.settings.chat_id = 99999

        self.app = create_app(
            config=self.config,
            storage=self.storage,
            session_manager=self.session_mgr,
            telegram_bot=self.bot,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_get_status_contains_world_name(self):
        """GET /status includes world_name and display_world_name."""
        temp_zip = Path(self.tmp_dir.name) / "status_test.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Alice", world_name="StatusWorld")

        resp = self.client.get("/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["world_name"], "StatusWorld")
        self.assertEqual(data["display_world_name"], "StatusWorld [Cloud]")
        self.assertTrue(data["save_exists"])

    def test_upload_save_with_world_name_form_field(self):
        """POST /upload-save accepts optional world_name form field."""
        # Claim host first
        claim_resp = self.client.post("/claim", json={"player_name": "HostPlayer"})
        self.assertEqual(claim_resp.status_code, 200)
        token = claim_resp.json()["session_token"]

        temp_zip = Path(self.tmp_dir.name) / "upload_test.zip"
        create_dummy_factorio_zip(temp_zip)

        with open(temp_zip, "rb") as f:
            resp = self.client.post(
                "/upload-save",
                files={"file": ("cloud_world.zip", f, "application/zip")},
                data={"session_token": token, "world_name": "My_Awesome_Base"},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["save"]["world_name"], "My_Awesome_Base")
        self.assertEqual(data["save"]["display_world_name"], "My_Awesome_Base [Cloud]")

        # Verify /status also reflects the uploaded world_name
        st = self.client.get("/status").json()
        self.assertEqual(st["world_name"], "My_Awesome_Base")
        self.assertEqual(st["display_world_name"], "My_Awesome_Base [Cloud]")

    def test_get_worlds_and_activate_world_endpoint(self):
        """GET /worlds returns world list and POST /worlds/activate restores archive."""
        # Upload world and archive it
        temp_zip = Path(self.tmp_dir.name) / "w1.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Alice", world_name="ArchivedWorld")
        arch_res = self.storage.archive_and_remove_current_world()
        arch_name = arch_res["archive_name"]

        # 1. GET /worlds
        resp_list = self.client.get("/worlds")
        self.assertEqual(resp_list.status_code, 200)
        data_list = resp_list.json()
        self.assertEqual(data_list["state"], "idle")
        self.assertIsNone(data_list["active_world"])
        self.assertEqual(len(data_list["archived_worlds"]), 1)
        self.assertEqual(data_list["archived_worlds"][0]["archive_name"], arch_name)

        # 2. POST /worlds/activate
        resp_act = self.client.post("/worlds/activate", json={"archive_name": arch_name, "player_name": "AdminAlice"})
        self.assertEqual(resp_act.status_code, 200)
        act_data = resp_act.json()
        self.assertTrue(act_data["exists"])
        self.assertEqual(act_data["world_name"], "ArchivedWorld")

        # Verify /status now shows world active
        st = self.client.get("/status").json()
        self.assertTrue(st["save_exists"])
        self.assertEqual(st["world_name"], "ArchivedWorld")

    def test_activate_world_endpoint_busy_conflict_409(self):
        """POST /worlds/activate returns 409 Conflict if server is in hosting_active state."""
        # Archive a world
        temp_zip = Path(self.tmp_dir.name) / "w1.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Alice", world_name="BusyTest")
        arch_res = self.storage.archive_and_remove_current_world()
        arch_name = arch_res["archive_name"]

        # Set session to hosting_active
        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"

        resp = self.client.post("/worlds/activate", json={"archive_name": arch_name})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "server_busy")

    def test_reset_world_triggers_telegram_notification(self):
        """POST /reset-world archives active world and dispatches notify_world_archived."""
        temp_zip = Path(self.tmp_dir.name) / "reset_test.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Bob", world_name="Vulcanus_Forge")

        resp = self.client.post("/reset-world", json={"player_name": "Bob"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["exists"])

        # Check Telegram calls
        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertTrue(any("Vulcanus_Forge [Cloud]</b> убран с сервера в архив" in c["json"].get("text", "") for c in send_calls))


class TestTelegramBotWorldsMenu(unittest.IsolatedAsyncioTestCase):
    """Tests for Telegram bot worlds menu formatting, callbacks, and dashboard protection."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.storage = SaveStorageManager(self.config)
        self.session_mgr = SessionLockManager(self.config)
        self.transport = MockTransport()
        self.mock_client = httpx.AsyncClient(transport=self.transport)
        self.bot = TelegramBotService(
            config=self.config,
            session_manager=self.session_mgr,
            storage_manager=self.storage,
            client=self.mock_client,
        )
        self.bot.settings.bot_token = "123:test"
        self.bot.settings.chat_id = 99999
        self.bot.settings.pinned_message_id = 101
        self.bot.settings.admin_user_ids = [42]

    async def asyncTearDown(self):
        await self.mock_client.aclose()
        self.tmp_dir.cleanup()

    def test_format_dashboard_html_shows_world_name_cloud(self):
        """Live dashboard formats 'Мир в сейфе: <b>{world_name} [Cloud]</b> — v{v}'."""
        temp_zip = Path(self.tmp_dir.name) / "dash_test.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Alice", world_name="Fulgora_Outpost")

        html_text = self.bot.format_dashboard_html()
        self.assertIn("<b>Fulgora_Outpost [Cloud]</b>", html_text)
        self.assertIn("Мир в сейфе:", html_text)

        # Archive world and check empty state text
        self.storage.archive_and_remove_current_world(deleted_by="Alice")
        empty_html = self.bot.format_dashboard_html()
        self.assertIn("Мир в сейфе: нет активного сохранения", empty_html)
        self.assertIn("Слот сервера: свободен для выбора нового мира", empty_html)

    def test_format_worlds_menu_html_and_keyboard(self):
        """Verifies format_worlds_menu_html and build_worlds_menu_keyboard content."""
        temp_zip = Path(self.tmp_dir.name) / "menu_test.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Bob", world_name="SpaceStation")

        menu_text = self.bot.format_worlds_menu_html()
        self.assertIn("Управление мирами в Сейфе", menu_text)
        self.assertIn("SpaceStation [Cloud]", menu_text)
        self.assertIn("Статус сервера: <b>[Свободен]</b>", menu_text)

        kb = self.bot.build_worlds_menu_keyboard()
        buttons = [b for row in kb["inline_keyboard"] for b in row]
        cb_datas = [b["callback_data"] for b in buttons]
        self.assertIn("world:archive", cb_datas)
        self.assertIn("menu:worlds", cb_datas)
        self.assertIn("menu:open", cb_datas)

    async def test_callback_menu_worlds_does_not_overwrite_pinned_dashboard(self):
        """CRITICAL: Clicking 'menu:worlds' under pinned message must send a NEW message."""
        cb_query_pinned = {
            "id": "cb_001",
            "from": {"id": 42, "username": "admin"},
            "message": {"message_id": 101, "chat": {"id": 99999}},  # 101 is pinned_message_id
            "data": "menu:worlds",
        }
        handled = await self.bot.handle_callback_query(cb_query_pinned)
        self.assertTrue(handled)

        # Verify sendMessage was called instead of editMessageText
        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertEqual(len(send_calls), 1)
        self.assertIn("Управление мирами в Сейфе", send_calls[0]["json"]["text"])

        # Pinned message (101) was NOT edited
        edit_calls = [c for c in self.transport.calls if "editMessageText" in c["url"]]
        self.assertEqual(len(edit_calls), 0)

    async def test_callback_menu_worlds_edits_existing_menu_message(self):
        """Clicking 'menu:worlds' inside an open menu (msg_id != pinned) edits the message."""
        cb_query_menu = {
            "id": "cb_002",
            "from": {"id": 42, "username": "admin"},
            "message": {"message_id": 505, "chat": {"id": 99999}},  # 505 is NOT pinned
            "data": "menu:worlds",
        }
        handled = await self.bot.handle_callback_query(cb_query_menu)
        self.assertTrue(handled)

        edit_calls = [c for c in self.transport.calls if "editMessageText" in c["url"]]
        self.assertEqual(len(edit_calls), 1)
        self.assertEqual(edit_calls[0]["json"]["message_id"], 505)

    async def test_callback_world_archive_lifecycle(self):
        """Clicking 'world:archive' archives active world, notifies chat, and updates dashboard."""
        temp_zip = Path(self.tmp_dir.name) / "arch_cb.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Bob", world_name="Vulcanus_Main")

        cb_query = {
            "id": "cb_003",
            "from": {"id": 42, "username": "admin"},
            "message": {"message_id": 505, "chat": {"id": 99999}},
            "data": "world:archive",
        }
        handled = await self.bot.handle_callback_query(cb_query)
        self.assertTrue(handled)

        # Check notification sent
        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertTrue(any("Vulcanus_Main [Cloud]</b> убран с сервера в архив" in c["json"].get("text", "") for c in send_calls))

        # Check active save is unlinked
        self.assertFalse(self.config.current_save_path.exists())

    async def test_callback_world_archive_blocked_when_busy(self):
        """Clicking 'world:archive' while game is active fails with alert without touching world."""
        temp_zip = Path(self.tmp_dir.name) / "arch_busy.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Bob", world_name="BusyWorld")

        self.session_mgr._session.state = SessionState.HOSTING_ACTIVE.value
        self.session_mgr._session.host_name = "HostAlice"

        cb_query = {
            "id": "cb_004",
            "from": {"id": 42, "username": "admin"},
            "message": {"message_id": 505, "chat": {"id": 99999}},
            "data": "world:archive",
        }
        handled = await self.bot.handle_callback_query(cb_query)
        self.assertTrue(handled)

        # Active save must NOT be unlinked
        self.assertTrue(self.config.current_save_path.exists())

        # Callback answered with alert
        ans_calls = [c for c in self.transport.calls if "answerCallbackQuery" in c["url"]]
        self.assertTrue(any("Нельзя убрать мир в архив, пока идет игра" in c["json"].get("text", "") for c in ans_calls))

    async def test_callback_world_activate_lifecycle(self):
        """Clicking 'world:activate:0' activates chosen archived world."""
        temp_zip = Path(self.tmp_dir.name) / "act_cb.zip"
        create_dummy_factorio_zip(temp_zip)
        self.storage.save_new_world_atomic(temp_zip, uploaded_by="Alice", world_name="Gleba_Base")
        self.storage.archive_and_remove_current_world(deleted_by="Alice")

        cb_query = {
            "id": "cb_005",
            "from": {"id": 42, "username": "admin"},
            "message": {"message_id": 505, "chat": {"id": 99999}},
            "data": "world:activate:0",
        }
        handled = await self.bot.handle_callback_query(cb_query)
        self.assertTrue(handled)

        # Active save is now restored
        self.assertTrue(self.config.current_save_path.exists())
        st = self.storage.get_current_save_metadata()
        self.assertEqual(st["world_name"], "Gleba_Base")

        # Announcement sent to chat
        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertTrue(any("Gleba_Base [Cloud]</b> выбран активным миром" in c["json"].get("text", "") for c in send_calls))

    async def test_worlds_command_sends_worlds_menu(self):
        """Command /worlds sends worlds menu message."""
        msg = {
            "message_id": 601,
            "from": {"id": 42, "username": "admin"},
            "chat": {"id": 99999},
            "text": "/worlds",
        }
        handled = await self.bot.handle_message(msg)
        self.assertTrue(handled)

        send_calls = [c for c in self.transport.calls if "sendMessage" in c["url"]]
        self.assertTrue(any("Управление мирами в Сейфе" in c["json"].get("text", "") for c in send_calls))

    async def test_register_commands_includes_worlds(self):
        """register_commands registers 'worlds' in setMyCommands."""
        res = await self.bot.register_commands()
        self.assertTrue(res)

        cmd_calls = [c for c in self.transport.calls if "setMyCommands" in c["url"]]
        self.assertEqual(len(cmd_calls), 1)
        cmds = cmd_calls[0]["json"]["commands"]
        cmd_names = [c["command"] for c in cmds]
        self.assertIn("worlds", cmd_names)


class TestZeroEmojiComplianceTask036(unittest.TestCase):
    """Enforces 100% Zero-Emoji Policy compliance on Task 036 additions."""

    EMOJI_PATTERN = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"
        "\U0001FA70-\U0001FAFF"
        "]+",
        flags=re.UNICODE,
    )

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = ServerConfig(storage_dir=Path(self.tmp_dir.name))
        self.storage = SaveStorageManager(self.config)
        self.bot = TelegramBotService(config=self.config, storage_manager=self.storage)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_worlds_menu_keyboards_and_text_have_zero_emojis(self):
        """No emojis in worlds menu, dashboard, or keyboard buttons."""
        kb_dash = self.bot.build_dashboard_keyboard()
        for row in kb_dash["inline_keyboard"]:
            for btn in row:
                self.assertFalse(self.EMOJI_PATTERN.search(btn["text"]), f"Emoji in dash btn: {btn['text']}")

        kb_settings = self.bot.build_settings_keyboard()
        for row in kb_settings["inline_keyboard"]:
            for btn in row:
                self.assertFalse(self.EMOJI_PATTERN.search(btn["text"]), f"Emoji in settings btn: {btn['text']}")

        kb_worlds = self.bot.build_worlds_menu_keyboard()
        for row in kb_worlds["inline_keyboard"]:
            for btn in row:
                self.assertFalse(self.EMOJI_PATTERN.search(btn["text"]), f"Emoji in worlds btn: {btn['text']}")

        menu_text = self.bot.format_worlds_menu_html()
        self.assertFalse(self.EMOJI_PATTERN.search(menu_text), f"Emoji in worlds menu text: {menu_text}")

        dash_text = self.bot.format_dashboard_html()
        self.assertFalse(self.EMOJI_PATTERN.search(dash_text), f"Emoji in dashboard text: {dash_text}")
