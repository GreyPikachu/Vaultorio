"""Тесты для модуля фоновых процессов Хоста (src/client/host_workers.py).

Покрывает пункты чек-листа T8.1 - T8.6:
- T8.1: LobbyStartDetector (Детекция реального запуска лобби)
- T8.2: LobbyStartDetector (Таймаут «Фальшивого Хоста»)
- T8.3: find_latest_session_save (Выбор свежего сейва сессии)
- T8.4: AutosaveWatcher (Фоновый Rolling Sync)
- T8.5: flush_final_save_on_exit (Выход из игры и фиксация)
- T8.6: HeartbeatWorker & HostWorkersManager (Жизненный цикл и координация)
"""

import os
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.client.host_workers import (
    AutosaveWatcher,
    HeartbeatWorker,
    HostWorkersManager,
    LobbyStartDetector,
    find_latest_session_save,
    flush_final_save_on_exit,
    parse_loaded_map_from_log,
)
from src.client.hub_client import HubClient


def _create_zip(path: Path, content: str = "dummy_save_content", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cloud_world/level.dat", content.encode("utf-8"))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ============================================================================
# T8.1: LobbyStartDetector (Детекция реального запуска лобби)
# ============================================================================


def test_t8_1_1_detect_lobby_start_from_log(tmp_path: Path):
    """T8.1.1: Обнаружение старта по ключевым фразам в factorio-current.log."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("0.000 Initializing Factorio...\n", encoding="utf-8")

    mock_client = MagicMock(spec=HubClient)
    mock_client.confirm_hosting.return_value = {"success": True, "state": "hosting_active"}

    detector = LobbyStartDetector(
        log_path=log_file,
        hub_client=mock_client,
        session_token="test_token_123",
        timeout_sec=10.0,
        factorio_port=0,  # отключаем сокет
    )

    # До записи маркера старта — не подтверждено
    assert detector.check_once() is False
    assert detector.is_confirmed is False
    mock_client.confirm_hosting.assert_not_called()

    # Дописываем строку хостинга игры
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("1.234 Info ServerMultiplayerManager.cpp: Hosting game at 0.0.0.0:34197\n")

    # Теперь должно обнаружить
    assert detector.check_once() is True
    assert detector.is_confirmed is True
    mock_client.confirm_hosting.assert_called_once_with("test_token_123", udp_port=0)


def test_t8_1_2_confirm_hosting_callback_and_flags(tmp_path: Path):
    """T8.1.2: Подтверждение хостинга на сервере и вызов callback on_confirmed."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("ServerRouter initialized\n", encoding="utf-8")

    mock_client = MagicMock(spec=HubClient)
    mock_client.confirm_hosting.return_value = {"success": True}

    confirmed_called = False

    def on_confirmed():
        nonlocal confirmed_called
        confirmed_called = True

    detector = LobbyStartDetector(
        log_path=log_file,
        hub_client=mock_client,
        session_token="token_xyz",
        timeout_sec=5.0,
        factorio_port=0,
        on_confirmed=on_confirmed,
    )

    res = detector.check_once()
    assert res is True
    assert detector.is_confirmed is True
    assert confirmed_called is True
    mock_client.confirm_hosting.assert_called_once()


def test_t8_1_3_detection_idempotency_stops_duplicate_calls(tmp_path: Path):
    """T8.1.3: Повторные вызовы check_once после подтверждения не дублируют вызовы к серверу."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("Opening socket for listening on port 34197\n", encoding="utf-8")

    mock_client = MagicMock(spec=HubClient)
    mock_client.confirm_hosting.return_value = {"success": True}

    detector = LobbyStartDetector(
        log_path=log_file,
        hub_client=mock_client,
        session_token="token_xyz",
        timeout_sec=5.0,
        factorio_port=0,
    )

    assert detector.check_once() is True
    assert detector.check_once() is True
    assert detector.check_once() is True
    assert mock_client.confirm_hosting.call_count == 1


# ============================================================================
# T8.2: LobbyStartDetector (Таймаут «Фальшивого Хоста»)
# ============================================================================


def test_t8_2_1_detector_timeout_releases_host(tmp_path: Path):
    """T8.2.1: Превышение timeout_sec вызывает hub_client.release_host с menu_idle_timeout."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("User is sitting in main menu...\n", encoding="utf-8")

    mock_client = MagicMock(spec=HubClient)
    mock_client.release_host.return_value = True

    timeout_called = False

    def on_timeout():
        nonlocal timeout_called
        timeout_called = True

    detector = LobbyStartDetector(
        log_path=log_file,
        hub_client=mock_client,
        session_token="token_afk_host",
        timeout_sec=0.2,
        check_interval=0.05,
        factorio_port=0,
        on_timeout=on_timeout,
    )

    detector.start()
    time.sleep(0.35)
    detector.stop()

    assert detector.is_timed_out is True
    assert detector.is_confirmed is False
    assert timeout_called is True
    mock_client.release_host.assert_called_once_with("token_afk_host", reason="menu_idle_timeout")


def test_t8_2_2_detector_validation_errors():
    """T8.2.2: Проверка валидации параметров конструктора."""
    with pytest.raises(ValueError, match="session_token"):
        LobbyStartDetector(
            log_path=Path("/tmp/log"),
            hub_client=MagicMock(),
            session_token="",
        )


# ============================================================================
# T8.3: find_latest_session_save (Выбор свежего сейва сессии)
# ============================================================================


def test_t8_3_1_find_latest_session_save_empty_or_old(tmp_path: Path):
    """T8.3.1: Возврат None при отсутствии сохранений или если они старше session_start_time."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    # Каталог пуст
    assert find_latest_session_save(saves_dir, session_start_time=1000.0) is None

    # Файл старше времени старта
    old_save = saves_dir / "old_world.zip"
    _create_zip(old_save, mtime=500.0)

    assert find_latest_session_save(saves_dir, session_start_time=1000.0) is None


def test_t8_3_2_find_latest_session_save_picks_newest_autosave(tmp_path: Path):
    """T8.3.2: Если изменен _autosave2.zip позже, чем cloud_world.zip, возвращается _autosave2.zip."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    session_start = 1000.0
    cloud_world = saves_dir / "cloud_world.zip"
    autosave1 = saves_dir / "_autosave1.zip"
    autosave2 = saves_dir / "_autosave2.zip"

    _create_zip(cloud_world, mtime=session_start + 10.0)
    _create_zip(autosave1, mtime=session_start + 20.0)
    _create_zip(autosave2, mtime=session_start + 30.0)

    freshest = find_latest_session_save(saves_dir, session_start_time=session_start)
    assert freshest is not None
    assert freshest.name == "_autosave2.zip"


def test_t8_3_3_find_latest_session_save_skips_invalid_zips(tmp_path: Path):
    """T8.3.3: Игнорирование поврежденных / недописанных архивов."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    session_start = 1000.0
    corrupt_save = saves_dir / "_autosave_corrupt.zip"
    corrupt_save.write_text("not a real zip content", encoding="utf-8")
    os.utime(corrupt_save, (session_start + 50.0, session_start + 50.0))

    valid_save = saves_dir / "_autosave1.zip"
    _create_zip(valid_save, mtime=session_start + 20.0)

    freshest = find_latest_session_save(saves_dir, session_start_time=session_start)
    assert freshest is not None
    assert freshest.name == "_autosave1.zip"


# ============================================================================
# T8.4: AutosaveWatcher (Фоновый Rolling Sync)
# ============================================================================


def test_t8_4_1_autosave_watcher_waits_for_confirmation(tmp_path: Path):
    """T8.4.1: Воркер не начинает выгрузку до тех пор, пока хостинг не подтвержден."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    session_start = time.time() - 10.0

    _create_zip(saves_dir / "_autosave1.zip", mtime=session_start + 5.0)

    mock_client = MagicMock(spec=HubClient)
    is_confirmed = False

    watcher = AutosaveWatcher(
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_123",
        session_start_time=session_start,
        upload_cooldown_sec=1.0,
        is_confirmed_getter=lambda: is_confirmed,
    )

    # При is_confirmed = False ничего не выгружается
    assert watcher.check_and_upload_once() is False
    mock_client.upload_save.assert_not_called()

    # Подтверждаем хостинг
    is_confirmed = True
    assert watcher.check_and_upload_once() is True
    mock_client.upload_save.assert_called_once()
    assert mock_client.upload_save.call_args[1]["release_on_success"] is False


def test_t8_4_2_autosave_watcher_cooldown_and_deduplication(tmp_path: Path):
    """T8.4.2 & T8.4.3: Соблюдение кулдауна и дедупликации по SHA-256."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    session_start = time.time() - 10.0

    save_path = saves_dir / "_autosave1.zip"
    _create_zip(save_path, content="version_1", mtime=session_start + 2.0)

    mock_client = MagicMock(spec=HubClient)
    mock_client.upload_save.return_value = {"success": True}

    watcher = AutosaveWatcher(
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_123",
        session_start_time=session_start,
        upload_cooldown_sec=2.0,
        is_confirmed_getter=lambda: True,
    )

    # Первая выгрузка — успех
    assert watcher.check_and_upload_once() is True
    assert mock_client.upload_save.call_count == 1

    # Немедленная вторая попытка — блокируется кулдауном
    assert watcher.check_and_upload_once() is False
    assert mock_client.upload_save.call_count == 1

    # Сбрасываем кулдаун для проверки дедупликации
    watcher.last_upload_time = 0.0

    # Тот же самый контент — блокируется дедупликацией по SHA-256
    assert watcher.check_and_upload_once() is False
    assert mock_client.upload_save.call_count == 1

    # Модифицируем файл (новый автосейв)
    _create_zip(save_path, content="version_2_new_progress", mtime=time.time())
    assert watcher.check_and_upload_once() is True
    assert mock_client.upload_save.call_count == 2


# ============================================================================
# T8.5: flush_final_save_on_exit (Выход из игры и фиксация)
# ============================================================================


def test_t8_5_1_exit_from_menu_without_confirm_releases_host(tmp_path: Path):
    """T8.5.1: Выход из главного меню без подтверждения хостинга освобождает слот без выгрузки."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    _create_zip(saves_dir / "cloud_world.zip", content="old_content")

    mock_client = MagicMock(spec=HubClient)
    mock_client.release_host.return_value = True

    res = flush_final_save_on_exit(
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_menu_exit",
        is_hosting_confirmed=False,
        session_start_time=time.time(),
    )

    assert res["action"] == "released_without_save"
    assert res["reason"] == "not_confirmed"
    mock_client.release_host.assert_called_once_with("token_menu_exit", reason="closed_in_menu")
    mock_client.upload_save.assert_not_called()


def test_t8_5_2_exit_picks_autosave_updates_cloud_world_and_uploads(tmp_path: Path):
    """T8.5.2: Выход после активной игры подхватывает _autosave1.zip, копирует в cloud_world.zip и выгружает."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    session_start = time.time() - 20.0

    cloud_world = saves_dir / "cloud_world.zip"
    autosave = saves_dir / "_autosave1.zip"

    _create_zip(cloud_world, content="initial_cloud_world", mtime=session_start + 1.0)
    _create_zip(autosave, content="new_autosave_progress_data", mtime=session_start + 10.0)

    mock_client = MagicMock(spec=HubClient)
    mock_client.upload_save.return_value = {"success": True, "state": "idle"}

    res = flush_final_save_on_exit(
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_active_game",
        is_hosting_confirmed=True,
        session_start_time=session_start,
    )

    assert res["action"] == "uploaded_final"
    assert res["released"] is True
    mock_client.upload_save.assert_called_once()
    assert mock_client.upload_save.call_args[1]["release_on_success"] is True

    # Проверяем, что cloud_world.zip обновился содержимым из _autosave1.zip
    with zipfile.ZipFile(cloud_world, "r") as zf:
        data = zf.read("cloud_world/level.dat").decode("utf-8")
        assert data == "new_autosave_progress_data"


def test_t8_5_3_exit_with_manual_save_uploads_cloud_world(tmp_path: Path):
    """T8.5.3: Выход с ручным сохранением в cloud_world.zip выгружает его и освобождает слот."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    session_start = time.time() - 20.0

    cloud_world = saves_dir / "cloud_world.zip"
    _create_zip(cloud_world, content="manual_saved_game", mtime=session_start + 15.0)

    mock_client = MagicMock(spec=HubClient)
    mock_client.upload_save.return_value = {"success": True}

    res = flush_final_save_on_exit(
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_manual_exit",
        is_hosting_confirmed=True,
        session_start_time=session_start,
    )

    assert res["action"] == "uploaded_final"
    mock_client.upload_save.assert_called_once_with(
        file_path=cloud_world,
        session_token="token_manual_exit",
        release_on_success=True,
        mod_list=None,
    )


# ============================================================================
# T8.6: HeartbeatWorker & HostWorkersManager (Жизненный цикл и координация)
# ============================================================================


def test_t8_6_1_heartbeat_worker_lifecycle():
    """T8.6.1: HeartbeatWorker отправляет heartbeat только при подтвержденном хостинге."""
    mock_client = MagicMock(spec=HubClient)
    mock_client.send_heartbeat.return_value = {"alive": True}

    is_confirmed = False
    worker = HeartbeatWorker(
        hub_client=mock_client,
        session_token="token_hb",
        interval_sec=0.1,
        is_confirmed_getter=lambda: is_confirmed,
        udp_port=34197,
    )

    # До подтверждения — send_once возвращает None
    assert worker.send_once() is None
    mock_client.send_heartbeat.assert_not_called()

    # Подтверждаем
    is_confirmed = True
    assert worker.send_once() == {"alive": True}
    mock_client.send_heartbeat.assert_called_once_with("token_hb", udp_port=34197)


def test_t8_6_2_host_workers_manager_coordination(tmp_path: Path):
    """T8.6.2 & T8.6.3: HostWorkersManager координирует запуск, остановку и on_game_exit."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("Info ServerMultiplayerManager.cpp: Hosting game at 0.0.0.0:34197\n", encoding="utf-8")

    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    cloud_world = saves_dir / "cloud_world.zip"
    _create_zip(cloud_world, content="manager_world", mtime=time.time())

    mock_client = MagicMock(spec=HubClient)
    mock_client.confirm_hosting.return_value = {"success": True}
    mock_client.upload_save.return_value = {"success": True}

    manager = HostWorkersManager(
        log_path=log_file,
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_mgr",
        detector_check_interval=0.05,
        heartbeat_interval_sec=0.1,
        autosave_poll_interval_sec=0.1,
        autosave_cooldown_sec=1.0,
    )

    manager.start()
    time.sleep(0.15)
    assert manager.detector.is_confirmed is True

    # Завершение игры
    exit_report = manager.on_game_exit()
    assert exit_report["action"] == "uploaded_final"
    assert exit_report["released"] is True

    # Проверяем, что все потоки остановлены
    assert not manager.detector._thread.is_alive()
    assert not manager.heartbeat._thread.is_alive()
    assert not manager.autosave_watcher._thread.is_alive()


# ============================================================================
# Дополнительные стресс-тесты и краевые случаи (Edge Cases)
# ============================================================================


def test_t8_1_4_detect_lobby_start_from_udp_port(tmp_path: Path):
    """T8.1.4: Обнаружение старта по занятости UDP-порта игрой."""
    import socket

    log_file = tmp_path / "empty_log.log"
    log_file.write_text("", encoding="utf-8")

    # Выделяем свободный UDP-порт для теста
    bound_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bound_sock.bind(("127.0.0.1", 0))
    test_port = bound_sock.getsockname()[1]

    mock_client = MagicMock(spec=HubClient)
    mock_client.confirm_hosting.return_value = {"success": True}

    detector = LobbyStartDetector(
        log_path=log_file,
        hub_client=mock_client,
        session_token="token_udp_port",
        factorio_port=test_port,
    )

    try:
        # Поскольку bound_sock держит порт test_port, detector должен обнаружить занятость порта
        assert detector.check_once() is True
        assert detector.is_confirmed is True
        mock_client.confirm_hosting.assert_called_once_with("token_udp_port", udp_port=test_port)
    finally:
        bound_sock.close()


def test_t8_3_4_find_latest_session_save_nonexistent_dir(tmp_path: Path):
    """T8.3.4: Несуществующая папка сохранений возвращает None без падения."""
    non_existent = tmp_path / "does_not_exist"
    assert find_latest_session_save(non_existent, session_start_time=100.0) is None


def test_t8_4_3_autosave_watcher_handles_network_upload_error(tmp_path: Path):
    """T8.4.3: Ошибка сети при фоновой выгрузке не роняет процесс воркера."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    session_start = time.time() - 10.0
    _create_zip(saves_dir / "_autosave1.zip", mtime=session_start + 2.0)

    mock_client = MagicMock(spec=HubClient)
    mock_client.upload_save.side_effect = RuntimeError("Network connection dropped")

    watcher = AutosaveWatcher(
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_err",
        session_start_time=session_start,
        upload_cooldown_sec=0.1,
        is_confirmed_getter=lambda: True,
    )

    # При ошибке возвращается False, исключение перехвачено
    assert watcher.check_and_upload_once() is False
    assert watcher.last_uploaded_sha256 is None


def test_t8_5_4_exit_with_no_saves_at_all(tmp_path: Path):
    """T8.5.4: Выход после подтвержденного хостинга при полном отсутствии файлов."""
    saves_dir = tmp_path / "empty_saves"
    saves_dir.mkdir()

    mock_client = MagicMock(spec=HubClient)
    mock_client.release_host.return_value = True

    res = flush_final_save_on_exit(
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_empty_saves",
        is_hosting_confirmed=True,
        session_start_time=time.time(),
    )

    assert res["action"] == "released_without_save"
    assert res["reason"] == "no_save_found"
    mock_client.release_host.assert_called_once_with("token_empty_saves", reason="no_save_found")


def test_t8_6_3_heartbeat_worker_handles_network_error():
    """T8.6.3: Сетевые ошибки в send_heartbeat логируются и возвращают None."""
    mock_client = MagicMock(spec=HubClient)
    mock_client.send_heartbeat.side_effect = ConnectionResetError("Server unreachable")

    worker = HeartbeatWorker(
        hub_client=mock_client,
        session_token="token_err",
        interval_sec=0.1,
        is_confirmed_getter=lambda: True,
    )

    assert worker.send_once() is None


# ============================================================================
# Задача 017: Изоляция одиночной игры и не-серверных карт
# ============================================================================


def test_t017_1_parse_loaded_map_from_log():
    """Задача 017: Парсинг имени загруженной карты из factorio-current.log (Unix & Windows)."""
    # 1. Unix-путь
    unix_log = (
        "0.000 Initializing Factorio...\n"
        "1.234 Loading map /Users/test/Library/Application Support/factorio/saves/cloud_world.zip: 789123 bytes.\n"
        "1.500 Map version 1.1.100\n"
    )
    assert parse_loaded_map_from_log(unix_log) == "cloud_world.zip"

    # 2. Windows-путь с обратными слэшами
    win_log = (
        "0.000 Initializing Factorio...\n"
        "2.100 Loading map C:\\Users\\Gamer\\AppData\\Roaming\\Factorio\\saves\\MySoloCampaign.zip: 1234567 bytes.\n"
    )
    assert parse_loaded_map_from_log(win_log) == "MySoloCampaign.zip"

    # 3. Несколько загрузок в одном логе (возвращает самую последнюю)
    multi_log = (
        "Loading map /saves/FirstWorld.zip: 100 bytes.\n"
        "Info: Returned to menu.\n"
        "Loading map /saves/SecondWorld.zip: 200 bytes.\n"
    )
    assert parse_loaded_map_from_log(multi_log) == "SecondWorld.zip"

    # 4. Создание нового мира с нуля (нет Loading map)
    new_map_log = (
        "0.000 Initializing Factorio...\n"
        "1.000 Creating new map\n"
        "2.000 Map generated successfully\n"
    )
    assert parse_loaded_map_from_log(new_map_log) is None

    # 5. Граничные случаи
    assert parse_loaded_map_from_log("") is None
    assert parse_loaded_map_from_log(None) is None  # type: ignore[arg-type]


def test_t017_2_detector_releases_host_on_non_cloud_map(tmp_path: Path):
    """Задача 017: Детектор немедленно освобождает слот Хоста при загрузке сторонней карты."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("0.000 Init...\n", encoding="utf-8")

    mock_client = MagicMock(spec=HubClient)
    mock_client.release_host.return_value = True

    non_cloud_called_with = None

    def on_non_cloud(map_name: str):
        nonlocal non_cloud_called_with
        non_cloud_called_with = map_name

    detector = LobbyStartDetector(
        log_path=log_file,
        hub_client=mock_client,
        session_token="token_local_map",
        target_save_name="cloud_world.zip",
        factorio_port=0,
        on_non_cloud_game=on_non_cloud,
    )

    # Игрок загружает личную карту
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("1.234 Loading map /Users/test/saves/MyPersonalBase.zip: 555555 bytes.\n")

    res = detector.check_once()
    assert res is False
    assert detector.is_confirmed is False
    assert detector.is_non_cloud_game is True
    assert detector.loaded_map_name == "MyPersonalBase.zip"
    assert non_cloud_called_with == "MyPersonalBase.zip"
    mock_client.release_host.assert_called_once_with("token_local_map", reason="local_map:MyPersonalBase.zip")
    mock_client.confirm_hosting.assert_not_called()

    # Даже если позже появятся маркеры хостинга, они блокируются
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("2.000 Hosting game at 0.0.0.0:34197\n")

    res2 = detector.check_once()
    assert res2 is False
    assert detector.is_confirmed is False
    mock_client.confirm_hosting.assert_not_called()


def test_t017_3_detector_removes_save_appearance_false_positive(tmp_path: Path):
    """Задача 017: Появление автосейва без сетевых маркеров и без порта НЕ активирует хостинг."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("0.000 Single player session...\n", encoding="utf-8")

    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    session_start = time.time()
    # Создаем свежий автосейв одиночной игры
    _create_zip(saves_dir / "_autosave1.zip", mtime=session_start + 10.0)

    mock_client = MagicMock(spec=HubClient)

    detector = LobbyStartDetector(
        log_path=log_file,
        hub_client=mock_client,
        session_token="token_solo_false_positive",
        saves_dir=saves_dir,
        session_start_time=session_start,
        factorio_port=0,  # сокет не слушается
    )

    # Проверяем: появление сейва НЕ должно активировать сетевой хостинг
    assert detector.check_once() is False
    assert detector.is_confirmed is False
    mock_client.confirm_hosting.assert_not_called()


def test_t017_4_flush_final_save_skips_when_non_cloud_game(tmp_path: Path):
    """Задача 017: При выходе из не-облачной игры cloud_world.zip не перезаписывается и ничего не выгружается."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    # Исходный облачный сейв
    canonical_save = saves_dir / "cloud_world.zip"
    _create_zip(canonical_save, content="ORIGINAL_CLOUD_SAVE", mtime=1000.0)

    # Локальный автосейв одиночной игры
    _create_zip(saves_dir / "_autosave1.zip", content="SOLO_CORRUPTING_CONTENT", mtime=2000.0)

    mock_client = MagicMock(spec=HubClient)
    mock_client.release_host.return_value = True

    res = flush_final_save_on_exit(
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_local_exit",
        is_hosting_confirmed=False,
        session_start_time=1500.0,
        target_save_name="cloud_world.zip",
        is_non_cloud_game=True,
        loaded_map_name="MySoloRun.zip",
    )

    assert res["action"] == "ignored_local_game"
    assert res["reason"] == "non_cloud_map"
    assert res["loaded_map"] == "MySoloRun.zip"
    assert res["released"] is True

    # Проверяем, что upload_save не вызывался
    mock_client.upload_save.assert_not_called()
    mock_client.release_host.assert_called_once_with("token_local_exit", reason="local_map_exit:MySoloRun.zip")

    # Исходный cloud_world.zip остался нетронутым
    with zipfile.ZipFile(canonical_save, "r") as zf:
        content = zf.read("cloud_world/level.dat").decode("utf-8")
        assert content == "ORIGINAL_CLOUD_SAVE"


def test_t017_5_find_latest_session_save_respects_allowed_save_names(tmp_path: Path):
    """Задача 017: find_latest_session_save игнорирует неразрешенные сохранения одиночной игры."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    session_start = 1000.0
    unrelated_solo = saves_dir / "UnrelatedSoloMap.zip"
    cloud_world = saves_dir / "cloud_world.zip"
    autosave = saves_dir / "_autosave1.zip"

    _create_zip(cloud_world, mtime=session_start + 10.0)
    _create_zip(autosave, mtime=session_start + 20.0)
    _create_zip(unrelated_solo, mtime=session_start + 50.0)  # Самый свежий, но посторонний!

    # С фильтром allowed_save_names={"cloud_world.zip"}:
    # unrelated_solo.zip должен быть отфильтрован, а выбран _autosave1.zip
    freshest = find_latest_session_save(
        saves_dir,
        session_start_time=session_start,
        allowed_save_names={"cloud_world.zip"},
    )
    assert freshest is not None
    assert freshest.name == "_autosave1.zip"

    # Если есть ТОЛЬКО посторонний сейв
    saves_dir2 = tmp_path / "saves2"
    saves_dir2.mkdir()
    _create_zip(saves_dir2 / "OtherMap.zip", mtime=session_start + 50.0)

    freshest2 = find_latest_session_save(
        saves_dir2,
        session_start_time=session_start,
        allowed_save_names={"cloud_world.zip"},
    )
    assert freshest2 is None


def test_t017_6_host_workers_manager_handles_non_cloud_lifecycle(tmp_path: Path):
    """Задача 017: HostWorkersManager останавливает фоновые службы и безопасно завершает сессию при не-серверной карте."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("0.000 Init\n", encoding="utf-8")

    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    _create_zip(saves_dir / "cloud_world.zip", content="ORIGINAL", mtime=1000.0)

    mock_client = MagicMock(spec=HubClient)
    mock_client.release_host.return_value = True

    manager = HostWorkersManager(
        log_path=log_file,
        saves_dir=saves_dir,
        hub_client=mock_client,
        session_token="token_mgr_non_cloud",
        target_save_name="cloud_world.zip",
        detector_check_interval=0.05,
        heartbeat_interval_sec=0.1,
        autosave_poll_interval_sec=0.1,
    )

    manager.start()

    # Игрок загружает локальный мир
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("1.000 Loading map /saves/SingleplayerBase.zip: 12345 bytes.\n")

    time.sleep(0.15)

    assert manager.detector.is_non_cloud_game is True
    assert manager.detector.loaded_map_name == "SingleplayerBase.zip"

    # Завершаем игру
    exit_report = manager.on_game_exit()
    assert exit_report["action"] == "ignored_local_game"
    assert exit_report["reason"] == "non_cloud_map"
    assert exit_report["loaded_map"] == "SingleplayerBase.zip"

    mock_client.upload_save.assert_not_called()


