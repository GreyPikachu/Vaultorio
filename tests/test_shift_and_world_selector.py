"""Тесты для Задачи 018: Детектор зажатого Shift и выбор активного мира сервера.

Покрывает пункты обязательного тест-плана Задачи 018:
1. is_shift_key_pressed через переменную окружения FACTORIO_FORCE_SETUP_GUI.
2. is_shift_key_pressed: обработка ошибок ctypes и платформенных сбоев (graceful fallback).
3. FactorioEnvironment.list_local_saves: корректная фильтрация автосейвов, служебных файлов и сортировка.
4. FirstRunSetupWindow с existing_config: работа без повторного ввода Кода Дружбы.
5. FirstRunSetupWindow: наполнение выпадающего списка миров (world_combo).
6. publish_selected_world_to_server: успешная публикация локального мира на сервер при idle.
7. publish_selected_world_to_server: отклонение при занятом сервере (hosting_active).
8. SessionOrchestrator: запуск GUI по зажатому Shift или флагу force_setup_gui для настроенного клиента.
9. factorio_wrapper: обработка CLI-флага --wrapper-setup.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
import time
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.factorio_wrapper import parse_wrapper_args
from src.client.hub_client import HubClient
from src.client.session_orchestrator import RoleType, SessionOrchestrator
from src.client.setup_gui import (
    DEFAULT_SERVER_WORLD_OPTION,
    FirstRunSetupWindow,
    publish_selected_world_to_server,
)
from src.client.shift_detector import is_shift_key_pressed


def _create_dummy_zip(path: Path, content: str = "dummy_data", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("level.dat", content.encode("utf-8"))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ============================================================================
# 1 & 2. Тесты детектора клавиши Shift
# ============================================================================


def test_shift_detector_env_var(monkeypatch: pytest.MonkeyPatch):
    """Проверка определения зажатия через переменную окружения FACTORIO_FORCE_SETUP_GUI."""
    monkeypatch.delenv("FACTORIO_FORCE_SETUP_GUI", raising=False)
    # По умолчанию без ctypes вызовов или без зажатия
    with patch("sys.platform", "linux"):
        assert is_shift_key_pressed() is False

    # Включение через "1"
    monkeypatch.setenv("FACTORIO_FORCE_SETUP_GUI", "1")
    assert is_shift_key_pressed() is True

    # Включение через "true"
    monkeypatch.setenv("FACTORIO_FORCE_SETUP_GUI", "true")
    assert is_shift_key_pressed() is True

    # Выключение через "0"
    monkeypatch.setenv("FACTORIO_FORCE_SETUP_GUI", "0")
    with patch("sys.platform", "linux"):
        assert is_shift_key_pressed() is False


def test_shift_detector_ctypes_graceful_fallback(monkeypatch: pytest.MonkeyPatch):
    """Сбои ctypes или недоступность библиотек безопасно возвращают False."""
    monkeypatch.delenv("FACTORIO_FORCE_SETUP_GUI", raising=False)

    with patch("sys.platform", "darwin"):
        with patch("ctypes.cdll.LoadLibrary", side_effect=OSError("Framework not found")):
            assert is_shift_key_pressed() is False

    with patch("sys.platform", "win32"):
        # 1. windll отсутствует (None)
        with patch.object(ctypes, "windll", None, create=True):
            assert is_shift_key_pressed() is False

        # 2. GetAsyncKeyState бросает исключение
        mock_windll = MagicMock()
        mock_windll.user32.GetAsyncKeyState.side_effect = OSError("API call failed")
        with patch.object(ctypes, "windll", mock_windll, create=True):
            assert is_shift_key_pressed() is False


def test_shift_detector_win32_mask(monkeypatch: pytest.MonkeyPatch):
    """Эмуляция клавиши Shift на Windows."""
    monkeypatch.delenv("FACTORIO_FORCE_SETUP_GUI", raising=False)

    mock_windll = MagicMock()
    mock_windll.user32.GetAsyncKeyState.return_value = 0x8000

    with patch("sys.platform", "win32"):
        with patch.object(ctypes, "windll", mock_windll, create=True):
            assert is_shift_key_pressed() is True

    # Клавиша отпущена (GetAsyncKeyState вернет 0)
    mock_windll.user32.GetAsyncKeyState.return_value = 0
    with patch("sys.platform", "win32"):
        with patch.object(ctypes, "windll", mock_windll, create=True):
            assert is_shift_key_pressed() is False


def test_shift_detector_darwin_mask(monkeypatch: pytest.MonkeyPatch):
    """Эмуляция флагов macOS ApplicationServices."""
    monkeypatch.delenv("FACTORIO_FORCE_SETUP_GUI", raising=False)

    mock_lib = MagicMock()
    # kCGEventFlagMaskShift = 0x00020000
    mock_lib.CGEventSourceFlagsState.return_value = 0x00020000

    with patch("sys.platform", "darwin"):
        with patch("ctypes.cdll.LoadLibrary", return_value=mock_lib):
            assert is_shift_key_pressed() is True

    # Клавиша отпущена
    mock_lib.CGEventSourceFlagsState.return_value = 0x00000000
    with patch("sys.platform", "darwin"):
        with patch("ctypes.cdll.LoadLibrary", return_value=mock_lib):
            assert is_shift_key_pressed() is False


# ============================================================================
# 3. Тесты FactorioEnvironment.list_local_saves
# ============================================================================


def test_factorio_env_list_local_saves(tmp_path: Path):
    """Проверка фильтрации и сортировки локальных миров в FactorioEnvironment."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    env = FactorioEnvironment(custom_user_data_dir=tmp_path)

    # 1. Если папка пуста
    assert env.list_local_saves() == []

    # 2. Создаем набор файлов
    now = time.time()
    save1 = _create_dummy_zip(saves_dir / "Campaign1.zip", mtime=now - 200)
    save2 = _create_dummy_zip(saves_dir / "MegaBase.zip", mtime=now - 50)
    save3 = _create_dummy_zip(saves_dir / "SpaceExploration.zip", mtime=now - 10)

    # Файлы, которые должны игнорироваться:
    _create_dummy_zip(saves_dir / "_autosave1.zip", mtime=now)
    _create_dummy_zip(saves_dir / "_autosave2.zip", mtime=now)
    _create_dummy_zip(saves_dir / ".hidden_save.zip", mtime=now)
    (saves_dir / "partial.tmp.zip").write_text("not a zip")
    (saves_dir / "save.part").write_text("part")
    (saves_dir / "notes.txt").write_text("text")

    # Сам облачный сейв
    _create_dummy_zip(saves_dir / "cloud_world.zip", mtime=now - 100)

    # По умолчанию cloud_world.zip исключается:
    saves = env.list_local_saves(include_cloud_world=False)
    names = [s.name for s in saves]
    assert names == ["SpaceExploration.zip", "MegaBase.zip", "Campaign1.zip"]

    # С параметром include_cloud_world=True
    saves_with_cloud = env.list_local_saves(include_cloud_world=True)
    names_with_cloud = [s.name for s in saves_with_cloud]
    assert "cloud_world.zip" in names_with_cloud
    assert len(names_with_cloud) == 4
    # Самый свежий из них SpaceExploration.zip
    assert names_with_cloud[0] == "SpaceExploration.zip"


# ============================================================================
# 6 & 7. Тесты publish_selected_world_to_server
# ============================================================================


def test_publish_selected_world_success(tmp_path: Path):
    """Успешная публикация локального мира на сервер, когда сервер свободен (idle)."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    env = FactorioEnvironment(custom_user_data_dir=tmp_path)

    # Исходный облачный сейв
    cloud_save = saves_dir / "cloud_world.zip"
    _create_dummy_zip(cloud_save, content="OLD_SERVER_WORLD")

    # Выбираемый локальный мир
    my_world = saves_dir / "MyIndustrialBase.zip"
    _create_dummy_zip(my_world, content="NEW_INDUSTRIAL_WORLD")

    mock_client = MagicMock(spec=HubClient)
    mock_client.get_status.return_value = {"state": "idle"}
    mock_client.claim_host.return_value = {"claimed": True, "session_token": "token_publish_123"}
    mock_client.upload_save.return_value = {"success": True, "sha256": "abcdef123"}

    res = publish_selected_world_to_server(
        env=env,
        hub_client=mock_client,
        source_save_path=my_world,
        player_name="Pikachu",
    )

    assert res["published"] is True

    # Проверяем, что был создан локальный бэкап старого cloud_world.zip
    backup_file = saves_dir / "cloud_world_prev_local.zip"
    assert backup_file.is_file()
    with zipfile.ZipFile(backup_file, "r") as zf:
        assert zf.read("level.dat").decode("utf-8") == "OLD_SERVER_WORLD"

    # Проверяем, что cloud_world.zip теперь содержит данные нового мира
    with zipfile.ZipFile(cloud_save, "r") as zf:
        assert zf.read("level.dat").decode("utf-8") == "NEW_INDUSTRIAL_WORLD"

    # Проверяем сетевые вызовы
    mock_client.claim_host.assert_called_once_with(player_name="Pikachu", wait_for_upload=True)
    mock_client.upload_save.assert_called_once_with(
        file_path=cloud_save,
        session_token="token_publish_123",
        release_on_success=True,
        mod_list=env.get_active_mods_manifest(),
    )


def test_publish_selected_world_rejects_when_busy(tmp_path: Path):
    """Публикация отклоняется с исключением, если сервер занят другим игроком."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    env = FactorioEnvironment(custom_user_data_dir=tmp_path)

    cloud_save = saves_dir / "cloud_world.zip"
    _create_dummy_zip(cloud_save, content="CURRENT_WORLD")

    my_world = saves_dir / "MyWorld.zip"
    _create_dummy_zip(my_world, content="ANOTHER_WORLD")

    mock_client = MagicMock(spec=HubClient)
    mock_client.get_status.return_value = {"state": "hosting_active", "current_host": "FriendAlex"}

    with pytest.raises(RuntimeError, match="Сейчас на сервере играет FriendAlex"):
        publish_selected_world_to_server(
            env=env,
            hub_client=mock_client,
            source_save_path=my_world,
            player_name="Pikachu",
        )

    # Файлы не должны быть изменены
    with zipfile.ZipFile(cloud_save, "r") as zf:
        assert zf.read("level.dat").decode("utf-8") == "CURRENT_WORLD"
    mock_client.claim_host.assert_not_called()
    mock_client.upload_save.assert_not_called()


# ============================================================================
# 4 & 5. Тесты интерфейса FirstRunSetupWindow с existing_config
# ============================================================================


class TestSetupGuiWorldSelector(unittest.TestCase):
    """Тесты расширенного графического окна настройки с выбором мира."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_gui_sel_")).resolve()
        self.user_data = (self.temp_dir / "userdata").resolve()
        self.user_data.mkdir(parents=True)
        self.saves_dir = self.user_data / "saves"
        self.saves_dir.mkdir(parents=True)

        self.env = FactorioEnvironment(custom_user_data_dir=self.user_data)
        self.env.set_player_username("PikachuTester")

        # Создаем пару локальных миров
        _create_dummy_zip(self.saves_dir / "BaseAlpha.zip", mtime=1000.0)
        _create_dummy_zip(self.saves_dir / "BaseBeta.zip", mtime=2000.0)

        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_window_with_existing_config_combobox_and_buttons(self):
        """Проверка инициализации окна при наличии уже настроенной конфигурации."""
        cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="dev_tok_test_123",
            player_name="PikachuTester",
        )

        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            existing_config=cfg,
        )

        self.assertTrue(app.is_already_configured)
        self.assertEqual(app.action_btn.cget("text"), "Применить и запустить игру")

        # Проверяем наполнение выпадающего списка
        self.assertEqual(app.world_options[0], DEFAULT_SERVER_WORLD_OPTION)
        self.assertIn("BaseBeta.zip", app.world_options)
        self.assertIn("BaseAlpha.zip", app.world_options)
        self.assertEqual(app.world_combo_var.get(), DEFAULT_SERVER_WORLD_OPTION)

        # При клике на Применить с дефолтным миром возвращается existing_config
        app._on_activate_click()
        self.assertEqual(app.result_config, cfg)

    def test_window_with_existing_config_and_world_selection(self):
        """Проверка выбора нового мира и запуска публикации."""
        cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="dev_tok_test_123",
            player_name="PikachuTester",
        )

        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            existing_config=cfg,
        )

        # Пользователь выбирает BaseBeta.zip
        app.world_combo_var.set("BaseBeta.zip")

        with patch("src.client.setup_gui.publish_selected_world_to_server") as mock_pub:
            mock_pub.return_value = {"published": True}
            app._publish_world_worker("BaseBeta.zip", cfg)
            mock_pub.assert_called_once()
            self.assertEqual(app.result_config, cfg)


# ============================================================================
# 8. Тесты SessionOrchestrator интеграции с Shift / force_setup_gui
# ============================================================================


def test_session_orchestrator_opens_gui_on_shift_or_force(tmp_path: Path):
    """SessionOrchestrator открывает GUI при наличии Shift или флага force_setup_gui."""
    cfg = ClientConfig(
        hub_http_url="http://127.0.0.1:8088",
        device_token="dev_tok_123",
    )
    env = FactorioEnvironment(custom_user_data_dir=tmp_path)

    gui_called = False
    gui_received_existing_cfg = None

    def dummy_gui_provider(existing_config=None, **kwargs):
        nonlocal gui_called, gui_received_existing_cfg
        gui_called = True
        gui_received_existing_cfg = existing_config
        return existing_config

    mock_client = MagicMock(spec=HubClient)
    mock_client.get_status.return_value = {"state": "idle"}
    mock_client.claim_host.return_value = {"claimed": True, "session_token": "tok"}

    with patch("src.client.session_orchestrator.HostP2PBridge"), \
         patch("src.client.session_orchestrator.HostWorkersManager"):
        # 1. Обычный запуск без Shift и без force_setup_gui: GUI НЕ вызывается
        orchestrator_normal = SessionOrchestrator(
            config=cfg,
            env=env,
            hub_client=mock_client,
            setup_gui_provider=dummy_gui_provider,
            force_setup_gui=False,
            shift_detector=lambda: False,
        )
        orchestrator_normal.prepare_session()
        assert gui_called is False

        # 2. Запуск с зажатым Shift: GUI вызывается и получает existing_config
        gui_called = False
        orchestrator_shift = SessionOrchestrator(
            config=cfg,
            env=env,
            hub_client=mock_client,
            setup_gui_provider=dummy_gui_provider,
            force_setup_gui=False,
            shift_detector=lambda: True,
        )
        orchestrator_shift.prepare_session()
        assert gui_called is True
        assert gui_received_existing_cfg == cfg

        # 3. Запуск с force_setup_gui=True: GUI вызывается
        gui_called = False
        orchestrator_force = SessionOrchestrator(
            config=cfg,
            env=env,
            hub_client=mock_client,
            setup_gui_provider=dummy_gui_provider,
            force_setup_gui=True,
            shift_detector=lambda: False,
        )
        orchestrator_force.prepare_session()
        assert gui_called is True


# ============================================================================
# 9. Тест аргументов командной строки factorio_wrapper
# ============================================================================


def test_wrapper_args_wrapper_setup():
    """Проверка распознавания флага --wrapper-setup парсером аргументов перехватчика."""
    known, game_args = parse_wrapper_args(["--wrapper-setup", "--load-game", "test.zip"])
    assert known.wrapper_setup is True
    assert game_args == ["--load-game", "test.zip"]

    known_no_setup, game_args2 = parse_wrapper_args(["--load-game", "test.zip"])
    assert known_no_setup.wrapper_setup is False
    assert game_args2 == ["--load-game", "test.zip"]
