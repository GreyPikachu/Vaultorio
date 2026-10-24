"""Тесты для главного оркестратора перехватчика Factorio (src/client/session_orchestrator.py, factorio_wrapper.py).

Покрывает чек-лист обязательного тест-плана T10.1 - T10.6:
- T10.1: Pre-Flight инициализация и восстановление окружения
- T10.2: Ветка Хоста (захват роли, синхронизация и запуск служб)
- T10.3: Ветка Гостя (Split-Brain Shield и подключение к прокси)
- T10.4: Ожидание передачи эстафеты при uploading_final
- T10.5: Фоновый мониторинг эстафеты для Гостя в процессе игры
- T10.6: Post-Exit финализация, очистка сокетов/потоков и отказоустойчивость
"""

import os
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.factorio_wrapper import main, parse_wrapper_args, setup_client_logging
from src.client.hub_client import HubClient
from src.client.session_orchestrator import (
    GuestHandoverWatcher,
    RoleType,
    SessionOrchestrator,
)


def _create_zip(path: Path, content: str = "test_data") -> Path:
    """Вспомогательная функция для создания тестового ZIP-архива."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cloud_world/level.dat", content.encode("utf-8"))
    return path


@pytest.fixture(autouse=True)
def set_test_device_token():
    """Устанавливает тестовый device_token для беспрепятственного запуска сессий в тестах."""
    old_token = os.environ.get("FACTORIO_DEVICE_TOKEN")
    os.environ["FACTORIO_DEVICE_TOKEN"] = "test_dev_token_wrapper"
    yield
    if old_token is not None:
        os.environ["FACTORIO_DEVICE_TOKEN"] = old_token
    else:
        os.environ.pop("FACTORIO_DEVICE_TOKEN", None)


@pytest.fixture
def test_env(tmp_path: Path) -> FactorioEnvironment:
    """Создает изолированное окружение FactorioEnvironment во временной директории."""
    user_data = tmp_path / "userdata"
    game_root = tmp_path / "gameroot"
    user_data.mkdir(parents=True)
    game_root.mkdir(parents=True)
    return FactorioEnvironment(
        custom_game_root=game_root,
        custom_user_data_dir=user_data,
        cloud_save_name="cloud_world.zip",
    )


# ============================================================================
# T10.1: Pre-Flight инициализация и восстановление окружения
# ============================================================================


def test_t10_1_1_restore_hidden_save_on_preflight(test_env: FactorioEnvironment):
    """T10.1.1: Восстановление спрятанного сейва из .cloud_stash/ при старте оркестратора."""
    # Симулируем ситуацию: прошлый сеанс Гостя завершился аварийным выключением ПК
    stash_save = test_env.cloud_stash_dir / "cloud_world.zip"
    _create_zip(stash_save, "stashed_content")
    assert stash_save.is_file()
    assert not (test_env.saves_dir / "cloud_world.zip").exists()

    mock_hub = MagicMock(spec=HubClient)
    mock_hub.get_status.return_value = {"state": "hosting_active", "current_host": "Alex"}

    orchestrator = SessionOrchestrator(
        env=test_env,
        hub_client=mock_hub,
    )

    with patch("src.client.session_orchestrator.show_role_banner"), \
         patch("src.client.session_orchestrator.copy_to_clipboard"), \
         patch("src.client.session_orchestrator.GuestP2PProxy"):
        # При вызове prepare_session() скрытый сейв восстанавливается
        orchestrator.prepare_session()

    # В ветке Гостя сейв снова прячется в .cloud_stash/, подтверждая работу Split-Brain Shield
    assert orchestrator.role == RoleType.GUEST
    assert stash_save.is_file()


def test_t10_1_2_player_name_resolution(test_env: FactorioEnvironment):
    """T10.1.2: Корректное определение никнейма игрока из настроек или fallback."""
    # 1. Из player-data.json
    test_env.set_player_username("TestEngineer")

    mock_hub = MagicMock(spec=HubClient)
    mock_hub.get_status.return_value = {"state": "idle"}
    mock_hub.claim_host.return_value = {"claimed": True, "session_token": "token_abc"}
    mock_hub.sync_save_before_host.return_value = {"updated": False}

    orchestrator = SessionOrchestrator(
        env=test_env,
        hub_client=mock_hub,
    )

    with patch("src.client.session_orchestrator.show_role_banner"), \
         patch("src.client.session_orchestrator.HostP2PBridge"), \
         patch("src.client.session_orchestrator.HostWorkersManager"):
        orchestrator.prepare_session()

    assert orchestrator.player_name == "TestEngineer"
    mock_hub.claim_host.assert_called_with(player_name="TestEngineer", wait_for_upload=True)


# ============================================================================
# T10.2: Ветка Хоста (захват роли, синхронизация и запуск служб)
# ============================================================================


def test_t10_2_1_claim_host_when_idle(test_env: FactorioEnvironment):
    """T10.2.1: Сервер свободен (idle) -> успешный claim_host -> назначение роли HOST."""
    test_env.set_player_username("HostPlayer")

    mock_hub = MagicMock(spec=HubClient)
    mock_hub.get_status.return_value = {"state": "idle"}
    mock_hub.claim_host.return_value = {
        "claimed": True,
        "session_token": "host_token_777",
    }
    mock_hub.sync_save_before_host.return_value = {
        "action": "already_up_to_date",
        "updated": False,
        "mods_mismatch": [],
    }

    orchestrator = SessionOrchestrator(
        env=test_env,
        hub_client=mock_hub,
    )

    with patch("src.client.session_orchestrator.show_role_banner") as mock_banner, \
         patch("src.client.session_orchestrator.HostP2PBridge") as mock_bridge_cls, \
         patch("src.client.session_orchestrator.HostWorkersManager") as mock_workers_cls:
        
        mock_bridge = MagicMock()
        mock_bridge_cls.return_value = mock_bridge
        mock_workers = MagicMock()
        mock_workers_cls.return_value = mock_workers

        report = orchestrator.prepare_session()

        assert orchestrator.role == RoleType.HOST
        assert orchestrator.session_token == "host_token_777"
        assert report["role"] == RoleType.HOST
        assert report["session_token"] == "host_token_777"

        # Проверка вызова синхронизации
        mock_hub.sync_save_before_host.assert_called_once()

        # Проверка показа баннера Хоста
        mock_banner.assert_called_once()
        assert "Вы — Хост!" in mock_banner.call_args[0][0]

        # Проверка запуска HostP2PBridge и HostWorkersManager
        mock_bridge.start.assert_called_once()
        mock_workers.start.assert_called_once()


def test_t10_2_2_sync_save_with_mods_mismatch_warning(test_env: FactorioEnvironment):
    """T10.2.2: Проверка обработки предупреждения о несовпадении модов при подготовке Хоста."""
    mock_hub = MagicMock(spec=HubClient)
    mock_hub.get_status.return_value = {"state": "idle"}
    mock_hub.claim_host.return_value = {"claimed": True, "session_token": "token_mods"}
    mock_hub.sync_save_before_host.return_value = {
        "action": "downloaded",
        "updated": True,
        "mods_mismatch": ["space-exploration", "krastorio2"],
    }

    orchestrator = SessionOrchestrator(env=test_env, hub_client=mock_hub)

    with patch("src.client.session_orchestrator.show_role_banner"), \
         patch("src.client.session_orchestrator.HostP2PBridge"), \
         patch("src.client.session_orchestrator.HostWorkersManager"):
        report = orchestrator.prepare_session()

    assert report["role"] == RoleType.HOST
    assert report["sync_result"]["mods_mismatch"] == ["space-exploration", "krastorio2"]


# ============================================================================
# T10.3: Ветка Гостя (Split-Brain Shield и подключение к прокси)
# ============================================================================


def test_t10_3_1_guest_role_when_server_busy(test_env: FactorioEnvironment):
    """T10.3.1: Сервер занят (hosting_active) -> роль GUEST, скрытие сейва и запуск прокси."""
    save_file = test_env.saves_dir / "cloud_world.zip"
    _create_zip(save_file, "guest_original_save")

    mock_hub = MagicMock(spec=HubClient)
    mock_hub.get_status.return_value = {
        "state": "hosting_active",
        "current_host": "Dima",
    }

    orchestrator = SessionOrchestrator(env=test_env, hub_client=mock_hub)

    with patch("src.client.session_orchestrator.show_role_banner") as mock_banner, \
         patch("src.client.session_orchestrator.copy_to_clipboard") as mock_copy, \
         patch("src.client.session_orchestrator.GuestP2PProxy") as mock_proxy_cls, \
         patch("src.client.session_orchestrator.GuestHandoverWatcher") as mock_watcher_cls:

        mock_proxy = MagicMock()
        mock_proxy_cls.return_value = mock_proxy
        mock_watcher = MagicMock()
        mock_watcher_cls.return_value = mock_watcher

        report = orchestrator.prepare_session()

        assert orchestrator.role == RoleType.GUEST
        assert report["host_name"] == "Dima"
        assert report["proxy_address"] == "127.0.0.1:34198"

        # Split-Brain Shield: файл cloud_world.zip физически скрыт в .cloud_stash/
        assert not save_file.exists()
        assert (test_env.cloud_stash_dir / "cloud_world.zip").is_file()

        # Проверка копирования адреса прокси в буфер обмена
        mock_copy.assert_called_with("127.0.0.1:34198")

        # Проверка баннера с именем Хоста
        mock_banner.assert_called_once()
        assert "Сейчас хостит Dima!" in mock_banner.call_args[0][0]

        # Проверка запуска прокси и фонового монитора эстафеты
        mock_proxy.start.assert_called_once()
        mock_watcher.start.assert_called_once()


def test_t10_3_2_guest_role_fallback_on_claim_rejection(test_env: FactorioEnvironment):
    """T10.3.2: Защита от гонки: claim_host вернул отказ (already_hosted) -> бесшовный fallback в Гостя."""
    save_file = test_env.saves_dir / "cloud_world.zip"
    _create_zip(save_file, "guest_save")

    mock_hub = MagicMock(spec=HubClient)
    # Изначально idle, но claim_host не прошел из-за гонки
    mock_hub.get_status.return_value = {"state": "idle", "current_host": None}
    mock_hub.claim_host.return_value = {"claimed": False, "reason": "already_hosted"}

    orchestrator = SessionOrchestrator(env=test_env, hub_client=mock_hub)

    with patch("src.client.session_orchestrator.show_role_banner"), \
         patch("src.client.session_orchestrator.copy_to_clipboard"), \
         patch("src.client.session_orchestrator.GuestP2PProxy") as mock_proxy_cls, \
         patch("src.client.session_orchestrator.GuestHandoverWatcher") as mock_watcher_cls:

        mock_proxy_cls.return_value = MagicMock()
        mock_watcher_cls.return_value = MagicMock()

        report = orchestrator.prepare_session()

        assert orchestrator.role == RoleType.GUEST
        assert not save_file.exists()
        assert (test_env.cloud_stash_dir / "cloud_world.zip").is_file()


# ============================================================================
# T10.4: Ожидание передачи эстафеты при uploading_final
# ============================================================================


def test_t10_4_1_wait_if_uploading_final(test_env: FactorioEnvironment):
    """T10.4.1: Статус uploading_final -> показ ожидания -> опрос -> успешный захват Хоста."""
    mock_hub = MagicMock(spec=HubClient)
    # Первый вызов get_status возвращает uploading_final
    mock_hub.get_status.return_value = {"state": "uploading_final", "current_host": "OldHost"}
    # wait_if_uploading_final возвращает idle после завершения выгрузки
    mock_hub.wait_if_uploading_final.return_value = {"state": "idle"}
    mock_hub.claim_host.return_value = {"claimed": True, "session_token": "new_host_token"}
    mock_hub.sync_save_before_host.return_value = {"action": "downloaded", "updated": True}

    orchestrator = SessionOrchestrator(env=test_env, hub_client=mock_hub)

    with patch("src.client.session_orchestrator.show_role_banner") as mock_banner, \
         patch("src.client.session_orchestrator.HostP2PBridge"), \
         patch("src.client.session_orchestrator.HostWorkersManager"):

        report = orchestrator.prepare_session()

        mock_hub.wait_if_uploading_final.assert_called_once()
        assert orchestrator.role == RoleType.HOST
        assert report["session_token"] == "new_host_token"

        # Было вызвано как минимум 2 баннера: ожидание и баннер Хоста
        assert mock_banner.call_count >= 2
        calls = [c[0][0] for c in mock_banner.call_args_list]
        assert any("Предыдущий Хост" in c for c in calls)
        assert any("Вы — Хост!" in c for c in calls)


# ============================================================================
# T10.5: Фоновый мониторинг эстафеты для Гостя в процессе игры
# ============================================================================


def test_t10_5_1_guest_handover_watcher_lifecycle(test_env: FactorioEnvironment):
    """T10.5.1: GuestHandoverWatcher фиксирует выход Хоста, восстанавливает сейв и скачивает свежий."""
    stashed_save = test_env.cloud_stash_dir / "cloud_world.zip"
    _create_zip(stashed_save, "old_stashed_save")

    mock_hub = MagicMock(spec=HubClient)
    # Имитируем последовательность статусов: сначала uploading_final, затем idle
    status_seq = [
        {"state": "uploading_final", "current_host": "LeavingHost"},
        {"state": "idle", "current_host": None},
    ]
    mock_hub.get_status.side_effect = lambda: status_seq.pop(0) if status_seq else {"state": "idle"}
    mock_hub.sync_save_before_host.return_value = {"action": "downloaded", "updated": True}

    handover_called = threading.Event()

    watcher = GuestHandoverWatcher(
        hub_client=mock_hub,
        env=test_env,
        initial_host="LeavingHost",
        poll_interval_sec=0.05,
        on_handover_ready=lambda: handover_called.set(),
    )

    with patch("src.client.session_orchestrator.show_role_banner") as mock_banner:
        watcher.start()
        # Ожидаем срабатывания эстафеты
        assert handover_called.wait(timeout=2.0)
        watcher.stop()

    assert watcher.is_handover_ready
    assert watcher.has_detected_host_exit
    # Проверка, что сейв восстановлен и скачан
    mock_hub.sync_save_before_host.assert_called_once()
    mock_banner.assert_called_once()
    assert "Хост LeavingHost вышел!" in mock_banner.call_args[0][0]


# ============================================================================
# T10.6: Post-Exit финализация и корректное закрытие
# ============================================================================


def test_t10_6_1_teardown_host_lifecycle(test_env: FactorioEnvironment):
    """T10.6.1: Завершение игры Хоста: вызов on_game_exit, остановка bridge и освобождение слота."""
    mock_hub = MagicMock(spec=HubClient)
    orchestrator = SessionOrchestrator(env=test_env, hub_client=mock_hub)
    orchestrator.role = RoleType.HOST

    mock_workers = MagicMock()
    mock_workers.on_game_exit.return_value = {"action": "uploaded_final", "released": True}
    orchestrator.host_workers = mock_workers

    mock_bridge = MagicMock()
    orchestrator.host_bridge = mock_bridge

    report = orchestrator.teardown()

    mock_workers.on_game_exit.assert_called_once()
    mock_bridge.stop.assert_called_once()
    assert orchestrator.host_workers is None
    assert orchestrator.host_bridge is None
    assert report["host_exit_report"]["released"] is True


def test_t10_6_2_teardown_guest_lifecycle_restores_save(test_env: FactorioEnvironment):
    """T10.6.2: Завершение игры Гостя: остановка прокси и обязательный возврат сейва из .cloud_stash/."""
    stashed_save = test_env.cloud_stash_dir / "cloud_world.zip"
    _create_zip(stashed_save, "my_guest_save")

    mock_hub = MagicMock(spec=HubClient)
    orchestrator = SessionOrchestrator(env=test_env, hub_client=mock_hub)
    orchestrator.role = RoleType.GUEST

    mock_proxy = MagicMock()
    orchestrator.guest_proxy = mock_proxy
    mock_watcher = MagicMock()
    orchestrator.handover_watcher = mock_watcher

    report = orchestrator.teardown()

    mock_proxy.stop.assert_called_once()
    mock_watcher.stop.assert_called_once()
    assert orchestrator.guest_proxy is None
    assert orchestrator.handover_watcher is None

    # Проверка, что скрытый сейв восстановлен обратно в корень saves/
    assert (test_env.saves_dir / "cloud_world.zip").is_file()
    assert report["restored_save"] == str(test_env.saves_dir / "cloud_world.zip")


def test_t10_6_3_offline_mode_resilience(test_env: FactorioEnvironment):
    """T10.6.3: Недоступность сервера-сейфа (оффлайн): переход в OFFLINE, игра работает безопасно."""
    mock_hub = MagicMock(spec=HubClient)
    mock_hub.get_status.side_effect = ConnectionRefusedError("VPS connection refused")

    orchestrator = SessionOrchestrator(env=test_env, hub_client=mock_hub)

    with patch("src.client.session_orchestrator.show_role_banner") as mock_banner:
        report = orchestrator.prepare_session()

    assert orchestrator.role == RoleType.OFFLINE
    assert report["reason"] == "server_unreachable"
    mock_banner.assert_called_once()
    assert "Сервер-сейф недоступен" in mock_banner.call_args[0][0]


def test_t10_6_4_locate_core_executable_and_cli_args(tmp_path: Path):
    """T10.6.4: Проверка поиска исполняемого файла factorio_core и парсинга аргументов wrapper."""
    # 1. Поиск бинарника через custom_path
    dummy_bin = tmp_path / "bin" / "x64" / "factorio_core"
    dummy_bin.parent.mkdir(parents=True)
    dummy_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy_bin.chmod(0o755)

    orchestrator = SessionOrchestrator()
    found = orchestrator.locate_core_executable(dummy_bin)
    assert found == dummy_bin.resolve()

    # 2. Проверка парсинга параметров командной строки
    args = [
        "--wrapper-game-root", "/opt/factorio",
        "--wrapper-player-name", "CyberAlex",
        "--load-game", "cloud_world.zip",
        "--mp-connect", "127.0.0.1:34198",
    ]
    known, game_args = parse_wrapper_args(args)
    assert known.wrapper_game_root == "/opt/factorio"
    assert known.wrapper_player_name == "CyberAlex"
    assert game_args == ["--load-game", "cloud_world.zip", "--mp-connect", "127.0.0.1:34198"]


def test_t10_6_5_run_full_cycle_try_finally(test_env: FactorioEnvironment, tmp_path: Path):
    """T10.6.5: Проверка полного цикла orchestrator.run() с гарантированным вызовом teardown()."""
    mock_hub = MagicMock(spec=HubClient)
    mock_hub.get_status.return_value = {"state": "idle"}
    mock_hub.claim_host.return_value = {"claimed": True, "session_token": "token_run"}
    mock_hub.sync_save_before_host.return_value = {"updated": False}

    dummy_bin = tmp_path / "factorio_core"
    dummy_bin.write_text("#!/bin/sh\nexit 0\n")
    dummy_bin.chmod(0o755)

    orchestrator = SessionOrchestrator(
        env=test_env,
        hub_client=mock_hub,
        custom_core_bin=dummy_bin,
    )

    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 12345
    mock_proc.poll.return_value = 0
    mock_proc.returncode = 0

    with patch("src.client.session_orchestrator.show_role_banner"), \
         patch("src.client.session_orchestrator.HostP2PBridge"), \
         patch("src.client.session_orchestrator.HostWorkersManager"), \
         patch.object(orchestrator, "launch_game", return_value=mock_proc), \
         patch.object(orchestrator, "teardown", wraps=orchestrator.teardown) as spy_teardown:

        code = orchestrator.run(game_args=["--arg1", "val1"])
        assert code == 0
        spy_teardown.assert_called_once()


def test_t10_6_6_run_offline_execv_posix(test_env: FactorioEnvironment, tmp_path: Path):
    """T10.6.6: В автономном режиме (OFFLINE) на POSIX вызывается os.execv без блокировки."""
    dummy_bin = tmp_path / "factorio_core"
    dummy_bin.write_text("#!/bin/sh\nexit 0\n")
    dummy_bin.chmod(0o755)

    mock_exec = MagicMock()
    orchestrator = SessionOrchestrator(
        env=test_env,
        custom_core_bin=dummy_bin,
        exec_fn=mock_exec,
    )
    orchestrator.role = RoleType.OFFLINE
    orchestrator.is_prepared = True

    with patch.object(orchestrator, "prepare_session", return_value={"role": RoleType.OFFLINE}), \
         patch.object(orchestrator, "launch_game") as mock_launch, \
         patch.object(orchestrator, "wait_game") as mock_wait, \
         patch("platform.system", return_value="Darwin"):

        code = orchestrator.run(game_args=["--offline-arg"])
        assert code == 0
        mock_exec.assert_called_once_with(str(dummy_bin), [str(dummy_bin), "--offline-arg"])
        mock_launch.assert_not_called()
        mock_wait.assert_not_called()


def test_t10_6_7_run_offline_windows_popen(test_env: FactorioEnvironment, tmp_path: Path):
    """T10.6.7: В автономном режиме (OFFLINE) на Windows вызывается subprocess.Popen без ожидания."""
    dummy_bin = tmp_path / "factorio_core.exe"
    dummy_bin.write_text("binary")

    orchestrator = SessionOrchestrator(
        env=test_env,
        custom_core_bin=dummy_bin,
    )
    orchestrator.role = RoleType.OFFLINE
    orchestrator.is_prepared = True

    with patch.object(orchestrator, "prepare_session", return_value={"role": RoleType.OFFLINE}), \
         patch.object(orchestrator, "wait_game") as mock_wait, \
         patch("subprocess.Popen") as mock_popen, \
         patch("platform.system", return_value="Windows"):

        code = orchestrator.run(game_args=["--win-arg"])
        assert code == 0
        mock_popen.assert_called_once_with([str(dummy_bin), "--win-arg"])
        mock_wait.assert_not_called()


def test_t10_6_8_run_offline_execv_fallback_on_error(test_env: FactorioEnvironment, tmp_path: Path):
    """T10.6.8: При сбое os.execv происходит безопасный fallback на launch_game."""
    dummy_bin = tmp_path / "factorio_core"
    dummy_bin.write_text("#!/bin/sh\nexit 0\n")
    dummy_bin.chmod(0o755)

    mock_exec = MagicMock(side_effect=OSError("Exec failed"))
    orchestrator = SessionOrchestrator(
        env=test_env,
        custom_core_bin=dummy_bin,
        exec_fn=mock_exec,
    )
    orchestrator.role = RoleType.OFFLINE
    orchestrator.is_prepared = True

    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 9999
    mock_proc.poll.return_value = 0
    mock_proc.returncode = 0

    with patch.object(orchestrator, "prepare_session", return_value={"role": RoleType.OFFLINE}), \
         patch.object(orchestrator, "launch_game", return_value=mock_proc) as mock_launch, \
         patch.object(orchestrator, "wait_game", return_value=0) as mock_wait, \
         patch("platform.system", return_value="Darwin"):

        code = orchestrator.run(game_args=["--fallback"])
        assert code == 0
        mock_exec.assert_called_once()
        mock_launch.assert_called_once_with(executable_path=None, game_args=["--fallback"])
        mock_wait.assert_called_once_with(mock_proc)

