"""Тесты синхронизации статуса сервера, имен миров [Cloud] и пустого сейфа (Задача 037).

Покрывает обязательный чек-лист испытаний:
- CP-037-1: HubClient.get_status пробрасывает world_name и display_world_name
- CP-037-2: HubClient.upload_save передает world_name в POST /upload-save
- CP-037-3: HubClient.sync_save_before_host перемещает локальный cloud_world.zip в .cloud_stash при пустом сейфе
- CP-037-4: HubClient.get_server_worlds опрашивает GET /worlds
- CP-037-5: publish_selected_world_to_server передает path.stem как world_name
- CP-037-6: FirstRunSetupWindow._format_server_status_banner форматирует баннер статуса сервера
- CP-037-7: FirstRunSetupWindow._format_world_title форматирует заголовок активного мира
- CP-037-8: archive_world_btn заблокирована (disabled) при server_state != idle
- CP-037-9: _on_refresh_status_click обновляет виджеты статуса сервера и мира
- CP-037-10: _archive_world_worker использует имя мира для локального архива
- CP-037-11: Переход в select_initial_world при пустом сервере
- CP-037-12: Соблюдение Zero-Emoji Policy
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.hub_client import HubClient
from src.client.setup_gui import (
    COLOR_ACCENT,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_TEXT_MUTED,
    KEEP_CURRENT_SERVER_WORLD_OPTION,
    SKIP_WORLD_UPLOAD_OPTION,
    FirstRunSetupWindow,
    publish_selected_world_to_server,
)


def _create_dummy_zip(dest: Path, inner_name: str = "level.dat") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(inner_name, b"dummy content for factorio save")


class TestHubClientWorldSync(unittest.TestCase):
    """Тесты расширений HubClient для синхронизации миров и статуса."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.env = FactorioEnvironment(
            custom_game_root=self.tmp / "game",
            custom_user_data_dir=self.tmp / "userdata",
        )
        self.cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="dev_tok_test",
            player_name="Engineer",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cp_037_1_hub_client_get_status_normalizes_world_names(self) -> None:
        """CP-037-1: HubClient.get_status пробрасывает world_name и display_world_name."""
        mock_resp = {
            "session": {"state": "idle", "host_name": None},
            "save": {
                "exists": True,
                "world_name": "Gleba_Outpost",
                "display_world_name": "Gleba_Outpost [Cloud]",
                "sha256": "abcdef123456",
                "version_counter": 3,
            },
        }

        mock_client = MagicMock(spec=httpx.Client)
        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.json.return_value = mock_resp
        mock_client.get.return_value = mock_http_resp

        hub = HubClient(config=self.cfg, http_client=mock_client)
        st = hub.get_status()

        self.assertEqual(st.get("world_name"), "Gleba_Outpost")
        self.assertEqual(st.get("display_world_name"), "Gleba_Outpost [Cloud]")
        self.assertEqual(st.get("state"), "idle")
        self.assertTrue(st.get("save_exists"))

    def test_cp_037_2_hub_client_upload_save_sends_world_name(self) -> None:
        """CP-037-2: HubClient.upload_save передает world_name в форму запроса."""
        save_file = self.tmp / "test_save.zip"
        _create_dummy_zip(save_file)

        mock_client = MagicMock(spec=httpx.Client)
        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.json.return_value = {"success": True, "sha256": "1234"}
        mock_client.post.return_value = mock_http_resp

        hub = HubClient(config=self.cfg, http_client=mock_client)
        res = hub.upload_save(
            file_path=save_file,
            session_token="sess_token_abc",
            world_name="Nauvis_Base",
        )

        self.assertTrue(res.get("success"))
        mock_client.post.assert_called_once()
        _, kwargs = mock_client.post.call_args
        self.assertEqual(kwargs["data"].get("world_name"), "Nauvis_Base")
        self.assertEqual(kwargs["data"].get("session_token"), "sess_token_abc")

    def test_cp_037_3_hub_client_sync_save_before_host_moves_local_save_on_empty_server(self) -> None:
        """CP-037-3: sync_save_before_host перемещает устаревший локальный сейф в .cloud_stash при пустом сервере."""
        local_save = self.env.saves_dir / self.env.cloud_save_name
        _create_dummy_zip(local_save)
        self.assertTrue(local_save.is_file())

        empty_status = {
            "save_exists": False,
            "save": {"exists": False, "sha256": None},
            "session": {"state": "idle"},
        }

        mock_client = MagicMock(spec=httpx.Client)
        hub = HubClient(config=self.cfg, http_client=mock_client)

        res = hub.sync_save_before_host(self.env, status=empty_status)

        self.assertEqual(res.get("action"), "no_server_save")
        self.assertFalse(res.get("updated"))
        self.assertFalse(local_save.exists(), "Устаревший cloud_world.zip должен быть убран из saves/")

        stash_file = self.env.cloud_stash_dir / "cloud_world_prev_local.zip"
        self.assertTrue(stash_file.is_file(), "Файл должен быть перемещен в .cloud_stash/cloud_world_prev_local.zip")

    def test_cp_037_4_hub_client_get_server_worlds(self) -> None:
        """CP-037-4: HubClient.get_server_worlds вызывает GET /worlds."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "active_world": {"name": "Vulcanus", "exists": True},
            "archived_worlds": [],
            "backups": [],
        }
        mock_client.get.return_value = mock_resp

        hub = HubClient(config=self.cfg, http_client=mock_client)
        worlds = hub.get_server_worlds()

        mock_client.get.assert_called_once_with("/worlds")
        self.assertIn("active_world", worlds)
        self.assertEqual(worlds["active_world"]["name"], "Vulcanus")


class TestPublishWorldNameDeduction(unittest.TestCase):
    """Тесты публикации мира и дедукции имени мира."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.env = FactorioEnvironment(
            custom_game_root=self.tmp / "game",
            custom_user_data_dir=self.tmp / "userdata",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cp_037_5_publish_selected_world_deduces_stem_as_world_name(self) -> None:
        """CP-037-5: publish_selected_world_to_server передает path.stem в upload_save."""
        source_save = self.env.saves_dir / "Aquilo_Outpost.zip"
        _create_dummy_zip(source_save)

        mock_hub = MagicMock(spec=HubClient)
        mock_hub.get_status.return_value = {"state": "idle"}
        mock_hub.claim_host.return_value = {"claimed": True, "session_token": "token_123"}
        mock_hub.upload_save.return_value = {"success": True}

        res = publish_selected_world_to_server(
            env=self.env,
            hub_client=mock_hub,
            source_save_path=source_save,
            player_name="Tester",
            world_name=source_save.stem,
        )

        self.assertTrue(res.get("published"))
        mock_hub.upload_save.assert_called_once()
        _, kwargs = mock_hub.upload_save.call_args
        self.assertEqual(kwargs.get("world_name"), "Aquilo_Outpost")


class TestGUIWorldSyncAndStatus(unittest.TestCase):
    """Тесты интерфейса и синхронизации статуса в FirstRunSetupWindow."""

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.env = FactorioEnvironment(
            custom_game_root=self.tmp / "game",
            custom_user_data_dir=self.tmp / "userdata",
        )
        self.cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="dev_tok_test",
            player_name="Engineer",
        )

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_cp_037_6_format_server_status_banner_all_states(self) -> None:
        """CP-037-6: _format_server_status_banner корректно форматирует все состояния."""
        app = FirstRunSetupWindow(root=self.root, env=self.env, existing_config=None)

        # 1. Не настроен
        txt, col = app._format_server_status_banner()
        self.assertIn("ожидание подключения", txt)
        self.assertEqual(col, COLOR_TEXT_MUTED)

        # Настраиваем клиент
        app.is_already_configured = True
        app.existing_config = self.cfg

        # 2. idle
        app.server_state = "idle"
        txt, col = app._format_server_status_banner()
        self.assertIn("[Свободен]", txt)
        self.assertEqual(col, COLOR_SUCCESS)

        # 3. claiming
        app.server_state = "claiming"
        app.server_host_name = "Alex"
        txt, col = app._format_server_status_banner()
        self.assertIn("[Подготовка лобби...]", txt)
        self.assertIn("Alex", txt)
        self.assertEqual(col, COLOR_ACCENT)

        # 4. hosting_active
        app.server_state = "hosting_active"
        app.server_host_name = "Bob"
        app.server_guests_count = 2
        txt, col = app._format_server_status_banner()
        self.assertIn("[В игре!]", txt)
        self.assertIn("Bob", txt)
        self.assertIn("гостей: 2", txt)
        self.assertEqual(col, COLOR_INFO)

        # 5. uploading_final
        app.server_state = "uploading_final"
        app.server_host_name = "Charlie"
        txt, col = app._format_server_status_banner()
        self.assertIn("[Сохранение мира в сейф...]", txt)
        self.assertIn("Charlie", txt)
        self.assertEqual(col, COLOR_ACCENT)

    def test_cp_037_7_format_world_title_active_and_empty(self) -> None:
        """CP-037-7: _format_world_title форматирует заголовок мира для активного и пустого сервера."""
        app = FirstRunSetupWindow(root=self.root, env=self.env, existing_config=self.cfg)

        # Активный мир с версией и размером
        app.server_has_save = True
        app.server_world_display = "Nauvis_Industrial [Cloud]"
        app.server_world_version = 7
        app.server_world_size_mb = 18.5
        title = app._format_world_title()
        self.assertEqual(title, "Активный мир на сервере: Nauvis_Industrial [Cloud] (v7, 18.5 МБ)")

        # Активный мир без версии
        app.server_world_version = None
        app.server_world_size_mb = None
        title = app._format_world_title()
        self.assertEqual(title, "Активный мир на сервере: Nauvis_Industrial [Cloud]")

        # Пустой сервер на шаге select_initial_world
        app.server_has_save = False
        app.step = "select_initial_world"
        title = app._format_world_title()
        self.assertEqual(title, "На сервере еще нет сохраненного мира. Выберите ваш мир для загрузки:")

        # Пустой сервер на шаге manage_world
        app.step = "manage_world"
        title = app._format_world_title()
        self.assertEqual(title, "Активный мир на сервере: Не выбран (слот пуст — выберите мир для загрузки):")

    def test_cp_037_8_archive_button_disabled_when_hosting_active(self) -> None:
        """CP-037-8: archive_world_btn заблокирована (disabled) при server_state != idle."""
        mock_status = {
            "save_exists": True,
            "state": "hosting_active",
            "current_host": "Alex",
            "save": {
                "exists": True,
                "world_name": "Fulgora_Lab",
                "display_world_name": "Fulgora_Lab [Cloud]",
                "size_bytes": 10485760,
                "version_counter": 2,
            },
        }

        with patch.object(HubClient, "get_status", return_value=mock_status):
            app = FirstRunSetupWindow(root=self.root, env=self.env, existing_config=self.cfg)
            self.root.update()

            self.assertEqual(app.server_state, "hosting_active")
            self.assertTrue(hasattr(app, "archive_world_btn"))
            self.assertEqual(str(app.archive_world_btn.cget("state")), "disabled")
            self.assertIn("Fulgora_Lab [Cloud]", app.world_title_lbl.cget("text"))
            self.assertIn("[В игре!] Хост: Alex", app.server_status_lbl.cget("text"))
            self.assertEqual(app.world_options[0], KEEP_CURRENT_SERVER_WORLD_OPTION)

    def test_cp_037_9_refresh_status_click_updates_widgets(self) -> None:
        """CP-037-9: _on_refresh_status_click опрашивает сервер и обновляет статус-виджеты."""
        initial_status = {
            "save_exists": True,
            "state": "idle",
            "save": {
                "exists": True,
                "world_name": "World1",
                "display_world_name": "World1 [Cloud]",
                "size_bytes": 5242880,
                "version_counter": 1,
            },
        }

        with patch.object(HubClient, "get_status", return_value=initial_status):
            app = FirstRunSetupWindow(root=self.root, env=self.env, existing_config=self.cfg)
            self.root.update()
            self.assertIn("[Свободен]", app.server_status_lbl.cget("text"))

        updated_status = {
            "save_exists": True,
            "state": "hosting_active",
            "current_host": "Captain",
            "active_guests": [{"name": "Mate"}],
            "save": {
                "exists": True,
                "world_name": "World2",
                "display_world_name": "World2 [Cloud]",
                "size_bytes": 15728640,
                "version_counter": 4,
            },
        }

        with patch.object(HubClient, "get_status", return_value=updated_status):
            app._on_refresh_status_click()
            # Даем фоновому потоку отработать и обновляем очередь Tk
            self.root.update()
            import time
            time.sleep(0.05)
            self.root.update()

            self.assertIn("[В игре!] Хост: Captain", app.server_status_lbl.cget("text"))
            self.assertIn("World2 [Cloud]", app.world_title_lbl.cget("text"))
            self.assertEqual(str(app.archive_world_btn.cget("state")), "disabled")

    def test_cp_037_10_archive_world_worker_uses_world_name_slug(self) -> None:
        """CP-037-10: _archive_world_worker использует настоящее имя мира для локального архива."""
        app = FirstRunSetupWindow(root=self.root, env=self.env, existing_config=self.cfg)
        app.server_world_name = "Super_Mega_Base"

        mock_hub = MagicMock(spec=HubClient)
        mock_hub.get_status.return_value = {
            "state": "idle",
            "save_exists": True,
            "world_name": "Super_Mega_Base",
            "save": {"exists": True, "world_name": "Super_Mega_Base"},
        }
        mock_hub.reset_server_world.return_value = {"success": True}

        saved_path_collector = []

        def fake_download(dest):
            saved_path_collector.append(Path(dest))
            _create_dummy_zip(Path(dest))

        mock_hub.download_save.side_effect = fake_download

        with patch("src.client.setup_gui.HubClient", return_value=mock_hub):
            app._archive_world_worker(download_local=True)
            self.root.update()

        self.assertEqual(len(saved_path_collector), 1)
        self.assertTrue(saved_path_collector[0].name.startswith("Super_Mega_Base_архив_сервера_"))
        self.assertTrue(saved_path_collector[0].name.endswith(".zip"))

    def test_cp_037_11_empty_server_opens_world_selector(self) -> None:
        """CP-037-11: При пустом сервере окно переходит в select_initial_world без фейкового сохранения."""
        local_save = self.env.saves_dir / "MyLocalWorld.zip"
        _create_dummy_zip(local_save)

        empty_status = {
            "save_exists": False,
            "state": "idle",
            "save": {"exists": False},
        }

        with patch.object(HubClient, "get_status", return_value=empty_status):
            app = FirstRunSetupWindow(root=self.root, env=self.env, existing_config=self.cfg)
            self.root.update()

            self.assertEqual(app.step, "select_initial_world")
            self.assertNotIn(KEEP_CURRENT_SERVER_WORLD_OPTION, app.world_options)
            self.assertIn("MyLocalWorld.zip", app.world_options)
            self.assertIn(SKIP_WORLD_UPLOAD_OPTION, app.world_options)
            self.assertFalse(app.archive_world_btn.winfo_ismapped())


class TestZeroEmojiCompliance(unittest.TestCase):
    """CP-037-12: Проверка отсутствия эмодзи во всех измененных файлах клиента."""

    def test_cp_037_12_zero_emoji_in_client_code(self) -> None:
        emoji_pattern = re.compile(
            r"[\U00010000-\U0010ffff"
            r"\u2600-\u26ff"
            r"\u2700-\u27bf"
            r"\u2300-\u23ff"
            r"\u2b50-\u2b55]"
        )

        files_to_check = [
            Path("src/client/hub_client.py"),
            Path("src/client/setup_gui.py"),
            Path("src/client/__init__.py"),
        ]

        for file_path in files_to_check:
            content = file_path.read_text(encoding="utf-8")
            matches = emoji_pattern.findall(content)
            self.assertEqual(
                matches,
                [],
                f"В файле {file_path} обнаружены запрещенные эмодзи: {matches}",
            )


if __name__ == "__main__":
    unittest.main()
