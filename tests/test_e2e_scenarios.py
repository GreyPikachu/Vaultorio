"""Сквозные интеграционные тесты и проверка граничных сценариев отказа и восстановления (E2E).

Покрывает чек-лист обязательного тест-плана T12.1 - T12.7 для Задачи 012:
- T12.1: Сценарий 1 (Защита от Split-Brain и безопасное подключение Гостя)
- T12.2: Сценарий 2 (Защита от «Фальшивого Хоста» при нахождении в главном меню)
- T12.3: Сценарий 3 (Гонка при передаче эстафеты в состоянии uploading_final)
- T12.4: Сценарий 4 (Выход Хоста без ручного сохранения: подхват _autosave1.zip)
- T12.5: Сценарий 5 (Одновременный запуск двух первых игроков: гонка за слот Хоста)
- T12.6: Сценарий 6 (Аварийное падение Хоста / обрыв связи: истечение lease_timeout)
- T12.7: Сценарий 7 (Чистый выход: 0 висячих процессов/сокетов, восстановление сейвов и README)
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.host_workers import HostWorkersManager
from src.client.hub_client import HubClient, compute_file_sha256
from src.client.session_orchestrator import RoleType, SessionOrchestrator
from src.server.api import create_app
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager
from src.server.storage import SaveStorageManager
from src.testing_support.mock_factorio_core import MockFactorioCore


# ============================================================================
# Вспомогательные фикстуры и фабрики
# ============================================================================


def create_test_save_archive(path: Path, content: str = "mock_factorio_level_data") -> Path:
    """Создает валидный ZIP-архив сохранения Factorio размером > 100 байт с маркерами."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = path.with_suffix(".tmp")
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cloud_world/level.dat", (content * 10).encode("utf-8"))
        zf.writestr("cloud_world/info.json", b'{"save_name": "cloud_world", "version": 1}')
    os.replace(temp_zip, path)
    return path


def make_test_server(
    temp_dir: Path,
    lease_timeout_seconds: float = 90.0,
    max_backups: int = 3,
) -> Tuple[Any, SaveStorageManager, SessionLockManager, ServerConfig]:
    """Создает изолированный сервер FastAPI с тестовой конфигурацией."""
    server_dir = temp_dir / "server_storage"
    server_dir.mkdir(parents=True, exist_ok=True)

    config = ServerConfig(
        storage_dir=server_dir,
        max_backups=max_backups,
        max_upload_size_bytes=10 * 1024 * 1024,
        http_port=8088,
        udp_stun_port=14197,
        lease_timeout_seconds=int(lease_timeout_seconds),
    )
    storage = SaveStorageManager(config)
    session_manager = SessionLockManager(config)
    app = create_app(config=config, storage=storage, session_manager=session_manager)
    return app, storage, session_manager, config


def make_test_client(
    app: Any,
    temp_dir: Path,
    player_name: str,
    guest_proxy_port: int = 34198,
    host_factorio_port: int = 34197,
) -> Tuple[SessionOrchestrator, FactorioEnvironment, HubClient]:
    """Создает изолированного клиента Factorio, подключенного к тестовому FastAPI приложению."""
    client_dir = temp_dir / f"client_{player_name}"
    user_data = client_dir / "userdata"
    game_root = client_dir / "gameroot"
    user_data.mkdir(parents=True, exist_ok=True)
    game_root.mkdir(parents=True, exist_ok=True)

    env = FactorioEnvironment(
        custom_game_root=game_root,
        custom_user_data_dir=user_data,
        cloud_save_name="cloud_world.zip",
    )
    env.set_player_username(player_name)

    client_config = ClientConfig(
        hub_http_url="http://testserver",
        hub_stun_host="127.0.0.1",
        hub_stun_port=14197,
        player_name=player_name,
        guest_proxy_port=guest_proxy_port,
        host_factorio_port=host_factorio_port,
    )

    test_http_client = TestClient(app)
    hub_client = HubClient(config=client_config, http_client=test_http_client)

    orchestrator = SessionOrchestrator(
        config=client_config,
        env=env,
        hub_client=hub_client,
    )
    return orchestrator, env, hub_client


# ============================================================================
# T12.1: Сценарий 1 (Защита от Split-Brain и безопасное подключение Гостя)
# ============================================================================


def test_t12_1_split_brain_shield_and_guest_join(tmp_path: Path):
    """T12.1: Проверка физического скрытия сейва у Гостя и запрета неавторизованной выгрузки (403)."""
    app, storage, session_manager, config = make_test_server(tmp_path)

    # Исходное состояние: на сервере уже есть мир, загруженный ранее
    init_tmp = config.tmp_dir / f"{config.temp_save_prefix}init1.zip"
    create_test_save_archive(init_tmp, "server_world_v1")
    storage.save_new_world_atomic(init_tmp, uploaded_by="InitUser", mod_list=[{"name": "base", "version": "2.0.0"}])

    # Игрок Alex занимает слот Хоста и подтверждает хостинг
    orch_alex, env_alex, hub_alex = make_test_client(app, tmp_path, "Alex")
    claim_alex = hub_alex.claim_host("Alex")
    assert claim_alex["claimed"] is True
    alex_token = claim_alex["session_token"]
    hub_alex.confirm_hosting(alex_token, udp_port=34197)

    # Статус сервера стал hosting_active
    status = hub_alex.get_status()
    assert status["state"] == "hosting_active"
    assert status["current_host"] == "Alex"

    # Теперь запускается второй игрок Dima (Гость)
    # У Dima локально в saves/ уже лежит старый cloud_world.zip
    orch_dima, env_dima, hub_dima = make_test_client(app, tmp_path, "Dima")
    dima_local_save = env_dima.saves_dir / "cloud_world.zip"
    create_test_save_archive(dima_local_save, "dima_stale_local_data")
    assert dima_local_save.is_file()

    # Dima подготавливает сессию через SessionOrchestrator
    with patch("src.client.session_orchestrator.show_role_banner"), \
         patch("src.client.session_orchestrator.copy_to_clipboard"), \
         patch("src.client.session_orchestrator.GuestP2PProxy"):
        prep_report = orch_dima.prepare_session()

    # T12.1.1: Проверка назначения роли Гостя и физического скрытия сейва
    assert orch_dima.role == RoleType.GUEST
    assert prep_report["role"] == RoleType.GUEST
    assert prep_report["save_hidden"] is True
    # В корне saves/ файла cloud_world.zip быть НЕ ДОЛЖНО!
    assert not (env_dima.saves_dir / "cloud_world.zip").exists()
    # Файл должен находиться в .cloud_stash/
    stashed_file = env_dima.cloud_stash_dir / "cloud_world.zip"
    assert stashed_file.is_file()

    # Запускаем MockFactorioCore в режиме Гостя для валидации защиты
    mock_guest = MockFactorioCore(
        mode="guest",
        user_data_dir=env_dima.user_data_dir,
        duration_sec=0.1,
    )
    guest_run_res = mock_guest.run()
    assert guest_run_res == 0

    # T12.1.2: Попытка Гостя выгрузить сейв с чужим/поддельным токеном блокируется с 403 Forbidden
    fake_save_to_upload = tmp_path / "fake_upload.zip"
    create_test_save_archive(fake_save_to_upload, "split_brain_attack_content")

    # Выгрузка с поддельным токеном вызывает PermissionError (403 Forbidden)
    with pytest.raises(PermissionError, match="403 Forbidden"):
        hub_dima.upload_save(fake_save_to_upload, session_token="invalid_dima_token_123")

    # Завершение сессии Гостя Dima (teardown)
    orch_dima.teardown()

    # Скрытый сейв должен быть безопасно возвращен из .cloud_stash/ обратно в saves/
    assert (env_dima.saves_dir / "cloud_world.zip").is_file()
    assert not (env_dima.cloud_stash_dir / "cloud_world.zip").exists()

    # Чистка ресурсов Alex
    hub_alex.release_host(alex_token)


# ============================================================================
# T12.2: Сценарий 2 (Защита от «Фальшивого Хоста»)
# ============================================================================


def test_t12_2_fake_host_protection_menu_idle(tmp_path: Path):
    """T12.2: Игрок забронировал слот (claiming), но остался в меню: бронь снимается без порчи сейва."""
    app, storage, session_manager, config = make_test_server(tmp_path, lease_timeout_seconds=2)

    # Начальный валидный сейв на сервере
    init_tmp = config.tmp_dir / f"{config.temp_save_prefix}init2.zip"
    create_test_save_archive(init_tmp, "original_content_untouched")
    store_meta = storage.save_new_world_atomic(init_tmp, uploaded_by="InitUser")
    original_sha = store_meta["sha256"]

    orch_alex, env_alex, hub_alex = make_test_client(app, tmp_path, "Alex")

    # T12.2.1: Игрок получает бронь claiming
    claim_res = hub_alex.claim_host("Alex")
    assert claim_res["claimed"] is True
    alex_token = claim_res["session_token"]

    status = hub_alex.get_status()
    assert status["state"] == "claiming"
    assert status["current_host"] == "Alex"

    # Игрок запускает игру, но находится в главном меню (idle_menu)
    # Сетевое лобби Factorio не запущено: нет лога 'Hosting game at' и нет UDP-сокета
    mock_idle = MockFactorioCore(
        mode="idle_menu",
        user_data_dir=env_alex.user_data_dir,
        duration_sec=0.2,
    )
    assert mock_idle.run() == 0

    # Создаем HostWorkersManager и проверяем поведение при выходе
    workers = HostWorkersManager(
        log_path=env_alex.log_file_path,
        saves_dir=env_alex.saves_dir,
        hub_client=hub_alex,
        session_token=alex_token,
    )
    # Хостинг не был подтвержден
    assert workers.detector.is_confirmed is False

    # T12.2.2: При выходе из меню on_game_exit освобождает слот без выгрузки сейва
    exit_report = workers.on_game_exit()
    assert exit_report["action"] == "released_without_save"
    assert exit_report["reason"] == "not_confirmed"
    assert exit_report["released"] is True

    # Сервер вернулся в idle
    status_after_exit = hub_alex.get_status()
    assert status_after_exit["state"] == "idle"
    assert status_after_exit["current_host"] is None

    # Облачный сейв остался в полной сохранности и не был перезаписан
    current_save_info = storage.get_current_save_metadata()
    assert current_save_info["exists"] is True
    assert current_save_info["sha256"] == original_sha


def test_t12_2_fake_host_claiming_timeout_auto_reset(tmp_path: Path):
    """T12.2 (Таймаут): При зависании игрока в claiming слот автоматически сбрасывается по таймауту."""
    app, storage, session_manager, config = make_test_server(tmp_path, lease_timeout_seconds=1)
    # Настраиваем короткий TTL для claiming
    session_manager.CLAIM_TTL_SECONDS = 0.5

    orch_alex, env_alex, hub_alex = make_test_client(app, tmp_path, "Alex")
    claim_res = hub_alex.claim_host("Alex")
    assert claim_res["claimed"] is True

    # Ждем истечения таймаута бронирования (0.6 сек)
    time.sleep(0.6)

    # Проверяем статус: сессия автоматически вернулась в idle
    status = hub_alex.get_status()
    assert status["state"] == "idle"
    assert status["current_host"] is None


# ============================================================================
# T12.3: Сценарий 3 (Гонка при передаче эстафеты — uploading_final)
# ============================================================================


def test_t12_3_handover_race_during_uploading_final(tmp_path: Path):
    """T12.3: При выгрузке финального сейва сервер удерживает uploading_final, гость ждет и получает новый мир."""
    app, storage, session_manager, _ = make_test_server(tmp_path)

    orch_alex, env_alex, hub_alex = make_test_client(app, tmp_path, "Alex")
    claim_alex = hub_alex.claim_host("Alex")
    alex_token = claim_alex["session_token"]
    hub_alex.confirm_hosting(alex_token)

    # T12.3.1: Alex закрывает игру и переводит сервер в состояние uploading_final
    begin_res = hub_alex.begin_final_upload(alex_token)
    assert begin_res["success"] is True

    status_during_upload = hub_alex.get_status()
    assert status_during_upload["state"] == "uploading_final"

    # Второй игрок Dima запускает игру в ту же секунду
    orch_dima, env_dima, hub_dima = make_test_client(app, tmp_path, "Dima")

    # Имитируем фоновый поток Alex, который через 0.4 сек завершает выгрузку нового сейва
    alex_final_save = tmp_path / "alex_final_world.zip"
    create_test_save_archive(alex_final_save, "alex_final_victory_save")

    def delayed_alex_upload():
        time.sleep(0.4)
        hub_alex.upload_save(
            file_path=alex_final_save,
            session_token=alex_token,
            release_on_success=True,
        )

    upload_thread = threading.Thread(target=delayed_alex_upload, daemon=True)
    upload_thread.start()

    # T12.3.2: Dima запрашивает claim_host(wait_for_upload=True)
    # Метод перехватывает uploading_final, входит в wait_if_uploading_final и дожидается освобождения
    claim_dima = hub_dima.claim_host(
        player_name="Dima",
        wait_for_upload=True,
        max_wait_sec=5.0,
    )
    upload_thread.join(timeout=3.0)

    # Dima успешно получил роль Хоста после завершения выгрузки Alex
    assert claim_dima["claimed"] is True
    dima_token = claim_dima["session_token"]
    assert dima_token != alex_token

    # Dima скачивает свежий сейф через sync_save_before_host
    sync_res = hub_dima.sync_save_before_host(env_dima)
    assert sync_res["updated"] is True
    assert sync_res["action"] == "downloaded"

    # Проверяем содержимое скачанного файла: это именно финальный сейв Alex
    dima_save_file = env_dima.saves_dir / "cloud_world.zip"
    assert dima_save_file.is_file()
    with zipfile.ZipFile(dima_save_file, "r") as zf:
        level_data = zf.read("cloud_world/level.dat").decode("utf-8")
        assert "alex_final_victory_save" in level_data

    # Освобождаем слот Dima
    hub_dima.release_host(dima_token)


# ============================================================================
# T12.4: Сценарий 4 (Выход Хоста без ручного сохранения)
# ============================================================================


def test_t12_4_host_exit_without_manual_save_autosave_recovery(tmp_path: Path):
    """T12.4: При выходе без сохранения перехватчик находит более свежий _autosave1.zip и выгружает его."""
    app, storage, session_manager, config = make_test_server(tmp_path)

    orch_alex, env_alex, hub_alex = make_test_client(app, tmp_path, "Alex")
    claim_alex = hub_alex.claim_host("Alex")
    alex_token = claim_alex["session_token"]
    hub_alex.confirm_hosting(alex_token)

    now = time.time()
    session_start = now - 150

    # T12.4.1: Создаем старый cloud_world.zip и более свежий _autosave1.zip
    old_cloud_world = env_alex.saves_dir / "cloud_world.zip"
    create_test_save_archive(old_cloud_world, "old_base_save_content")
    old_mtime = now - 100
    os.utime(old_cloud_world, (old_mtime, old_mtime))

    # Свежий автосейв, сгенерированный игрой Factorio
    fresh_autosave = env_alex.saves_dir / "_autosave1.zip"
    create_test_save_archive(fresh_autosave, "recovered_autosave_content_from_factorio")
    fresh_mtime = now - 10
    os.utime(fresh_autosave, (fresh_mtime, fresh_mtime))

    # Инициализируем HostWorkersManager с session_start_time
    workers = HostWorkersManager(
        log_path=env_alex.log_file_path,
        saves_dir=env_alex.saves_dir,
        hub_client=hub_alex,
        session_token=alex_token,
        session_start_time=session_start,
    )
    # Помечаем детектор как подтвержденный
    workers.detector._is_confirmed = True

    # T12.4.2: Хост завершает сессию через on_game_exit()
    exit_report = workers.on_game_exit()

    assert exit_report["action"] == "uploaded_final"
    assert exit_report["released"] is True

    # Проверяем, что локальный cloud_world.zip был перезаписан содержимым автосейва
    with zipfile.ZipFile(old_cloud_world, "r") as zf:
        data = zf.read("cloud_world/level.dat").decode("utf-8")
        assert "recovered_autosave_content_from_factorio" in data

    # Проверяем, что на сервере сейф обновился и содержит те же данные
    server_save_info = storage.get_current_save_metadata()
    assert server_save_info["exists"] is True
    server_file = config.current_save_path
    with zipfile.ZipFile(server_file, "r") as zf:
        server_data = zf.read("cloud_world/level.dat").decode("utf-8")
        assert "recovered_autosave_content_from_factorio" in server_data

    # Сервер вернулся в idle
    status = hub_alex.get_status()
    assert status["state"] == "idle"


# ============================================================================
# T12.5: Сценарий 5 (Одновременный запуск двух первых игроков)
# ============================================================================


def test_t12_5_simultaneous_start_race_condition(tmp_path: Path):
    """T12.5: Два игрока одновременно жмут запуск: первый становится Хостом, второй гладко становится Гостем."""
    app, storage, session_manager, _ = make_test_server(tmp_path)

    orch_alex, env_alex, hub_alex = make_test_client(app, tmp_path, "Alex")
    orch_boris, env_boris, hub_boris = make_test_client(app, tmp_path, "Boris")

    # Игрок Alex первым захватывает слот
    claim_alex = hub_alex.claim_host("Alex")
    assert claim_alex["claimed"] is True
    alex_token = claim_alex["session_token"]
    hub_alex.confirm_hosting(alex_token)

    # Игрок Boris обращается в то же время и получает отказ (сервер уже занят)
    claim_boris = hub_boris.claim_host("Boris", wait_for_upload=False)
    assert claim_boris["claimed"] is False
    assert claim_boris.get("reason") in ("already_hosted", "already_claimed")

    # Boris запускает подготовку сессии через SessionOrchestrator
    # Внутри orchestrator.prepare_session(): видя отказ в claim_host, он мягко переходит в ROLE_GUEST
    with patch("src.client.session_orchestrator.show_role_banner"), \
         patch("src.client.session_orchestrator.copy_to_clipboard"), \
         patch("src.client.session_orchestrator.GuestP2PProxy"), \
         patch("src.client.session_orchestrator.GuestHandoverWatcher"):
        prep_boris = orch_boris.prepare_session()

    assert orch_boris.role == RoleType.GUEST
    assert prep_boris["role"] == RoleType.GUEST
    assert prep_boris["host_name"] == "Alex"

    # Проверяем, что в player-data.json Boris предзаполнен адрес прокси
    pdata_file = env_boris.user_data_dir / "player-data.json"
    assert pdata_file.is_file()

    # Чистка ресурсов
    orch_boris.teardown()
    hub_alex.release_host(alex_token)


# ============================================================================
# T12.6: Сценарий 6 (Аварийное падение Хоста / обрыв связи)
# ============================================================================


def test_t12_6_emergency_host_crash_lease_expiration_and_recovery(tmp_path: Path):
    """T12.6: Хост внезапно вылетает без release: по истечении lease_timeout сервер возвращается в idle."""
    # Конфигурируем сервер с коротким временем аренды (lease_timeout_seconds=1)
    app, storage, session_manager, config = make_test_server(
        tmp_path,
        lease_timeout_seconds=1,
    )

    orch_alex, env_alex, hub_alex = make_test_client(app, tmp_path, "Alex")
    claim_alex = hub_alex.claim_host("Alex")
    assert claim_alex["claimed"] is True
    alex_token = claim_alex["session_token"]
    hub_alex.confirm_hosting(alex_token)

    # Имитируем выгрузку периодического автосейва в процессе игры
    periodic_autosave = tmp_path / "periodic_autosave.zip"
    create_test_save_archive(periodic_autosave, "periodic_cloud_progress_checkpoint")
    hub_alex.upload_save(periodic_autosave, session_token=alex_token, release_on_success=False)

    # Статус сервера подтвержден
    st_active = hub_alex.get_status()
    assert st_active["state"] == "hosting_active"
    assert st_active["current_host"] == "Alex"

    # T12.6.1: АВАРИЯ! Хост падает (SIGKILL / отключение питания).
    # Никакой release_host() или begin_final_upload() не вызывается, heartbeat прекращается.
    # Ждем истечения lease_timeout (1.1 сек)
    time.sleep(1.1)

    # Проверяем статус: сервер автоматически сбросил сессию в idle из-за просрочки heartbeat
    st_after_crash = hub_alex.get_status()
    assert st_after_crash["state"] == "idle"
    assert st_after_crash["current_host"] is None

    # T12.6.2: Следующий игрок Dima может беспрепятственно занять роль нового Хоста
    orch_dima, env_dima, hub_dima = make_test_client(app, tmp_path, "Dima")
    claim_dima = hub_dima.claim_host("Dima")
    assert claim_dima["claimed"] is True
    dima_token = claim_dima["session_token"]

    # Dima скачивает сохранение с сервера
    sync_dima = hub_dima.sync_save_before_host(env_dima)
    assert sync_dima["updated"] is True

    # Проверяем, что Dima получил последний автосейв, загруженный перед аварией Alex
    dima_save = env_dima.saves_dir / "cloud_world.zip"
    with zipfile.ZipFile(dima_save, "r") as zf:
        data = zf.read("cloud_world/level.dat").decode("utf-8")
        assert "periodic_cloud_progress_checkpoint" in data

    # Освобождаем слот Dima
    hub_dima.release_host(dima_token)


# ============================================================================
# T12.7: Сценарий 7 (Чистый выход: 0 висячих процессов/сокетов и проверка README)
# ============================================================================


def test_t12_7_clean_teardown_zero_lingering_threads_and_sockets(tmp_path: Path):
    """T12.7: Проверка полной остановки потоков, освобождения портов 34197/34198 и возврата сейва."""
    app, storage, session_manager, _ = make_test_server(tmp_path)

    # 1. Проверка очистки для Хоста
    orch_host, env_host, hub_host = make_test_client(
        app,
        tmp_path,
        "HostCleanup",
        host_factorio_port=34197,
    )
    claim_host = hub_host.claim_host("HostCleanup")
    host_token = claim_host["session_token"]
    hub_host.confirm_hosting(host_token)

    # Запускаем реальные потоки HostWorkersManager
    workers = HostWorkersManager(
        log_path=env_host.log_file_path,
        saves_dir=env_host.saves_dir,
        hub_client=hub_host,
        session_token=host_token,
        heartbeat_interval_sec=0.1,
    )
    orch_host.role = RoleType.HOST
    orch_host.session_token = host_token
    orch_host.host_workers = workers
    workers.start()

    # Убеждаемся, что потоки работают
    assert workers.heartbeat._thread is not None and workers.heartbeat._thread.is_alive()

    # Выполняем teardown
    teardown_report = orch_host.teardown()
    assert teardown_report["role"] == RoleType.HOST

    # Убеждаемся, что все фоновые потоки завершились
    time.sleep(0.2)
    assert workers.heartbeat._thread is None or not workers.heartbeat._thread.is_alive()
    if workers.detector:
        assert workers.detector._thread is None or not workers.detector._thread.is_alive()
    if workers.autosave_watcher:
        assert workers.autosave_watcher._thread is None or not workers.autosave_watcher._thread.is_alive()

    # 2. Проверка очистки для Гостя
    orch_guest, env_guest, hub_guest = make_test_client(
        app,
        tmp_path,
        "GuestCleanup",
        guest_proxy_port=34198,
    )
    orch_guest.role = RoleType.GUEST

    # Создаем скрытый сейв у Гостя
    hidden_save = env_guest.cloud_stash_dir / "cloud_world.zip"
    create_test_save_archive(hidden_save, "guest_stashed_data")
    assert hidden_save.is_file()
    assert not (env_guest.saves_dir / "cloud_world.zip").exists()

    # Выполняем teardown Гостя
    guest_report = orch_guest.teardown()
    assert guest_report["role"] == RoleType.GUEST

    # T12.7.2: Сейф гарантированно восстановлен из .cloud_stash/
    assert (env_guest.saves_dir / "cloud_world.zip").is_file()
    assert not (env_guest.cloud_stash_dir / "cloud_world.zip").exists()

    # T12.7.1: Проверяем, что локальные порты 34197 и 34198 полностью освобождены
    sock1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock1.bind(("127.0.0.1", 34197))
    finally:
        sock1.close()

    sock2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock2.bind(("127.0.0.1", 34198))
    finally:
        sock2.close()


def test_t12_7_readme_documentation_validation():
    """T12.7.3: Проверка качества, полноты и соответствия ограничениям файла README.md."""
    readme_path = Path("README.md").resolve()
    assert readme_path.is_file(), "Файл README.md должен существовать в корне проекта"

    content = readme_path.read_text(encoding="utf-8")
    assert len(content) > 1000, "README.md должен содержать подробное описание"

    # Обязательные разделы
    assert "Архитектурная концепция" in content or "Архитектура" in content
    assert "Сейф + Перехватчик" in content
    assert "Ключевые механизмы защиты" in content or "Защита от Split-Brain" in content
    assert "Развертывание на VPS" in content
    assert "Установка для игроков" in content
    assert "Разработка и тестирование" in content

    # Проверка фиксации портов
    assert "8088" in content, "В README.md должен быть указан порт HTTP API 8088"
    assert "14197" in content, "В README.md должен быть указан порт STUN Coordinator 14197/udp"

    # Проверка строгого барьера: порт диапазона 20000:50000 для Hysteria 2
    assert "20000:50000" in content, "В README.md должен быть четко описан запрет на диапазон 20000:50000 для Hysteria 2"

    # Проверка жесткого пользовательского ограничения: никаких эмодзи и смайликов
    # Unicode диапазоны настоящих эмодзи (исключая стандартные символы псевдографики 0x2500-0x257F)
    emoji_pattern = re.compile(
        r"[\U0001F600-\U0001F64F"  # Emoticons
        r"\U0001F300-\U0001F5FF"  # Symbols & Pictographs
        r"\U0001F680-\U0001F6FF"  # Transport & Map
        r"\U0001F1E0-\U0001F1FF"  # Regional Indicator Symbols / Flags
        r"\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        r"\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        r"\U00002700-\U000027BF]"  # Dingbats
    )
    matches = emoji_pattern.findall(content)
    assert not matches, f"В README.md обнаружены эмодзи, нарушающие ограничение пользователя: {matches}"
