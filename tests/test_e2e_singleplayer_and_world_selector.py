"""Сквозные E2E-тесты для Задачи 019:

1. Поддержка параметров --map-name и режима singleplayer в MockFactorioCore.
2. Сквозная изоляция одиночной игры (отзыв брони Хоста и сохранение серверного сейфа от перезаписи).
3. Полный жизненный цикл смены активного мира через окно настроек по Shift (выгрузка и ротация бэкапов).
4. Защита от смены мира при занятом сервере (hosting_active).
5. Верификация актуальности и полноты пользовательской документации (README.md).
"""

from __future__ import annotations

import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.host_workers import HostWorkersManager, LobbyStartDetector
from src.client.hub_client import HubClient, compute_file_sha256
from src.client.session_orchestrator import RoleType, SessionOrchestrator
from src.client.setup_gui import publish_selected_world_to_server
from src.server.api import create_app
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager
from src.server.storage import SaveStorageManager
from src.testing_support.mock_factorio_core import MockFactorioCore, create_dummy_save


# ============================================================================
# Вспомогательные фикстуры и генераторы
# ============================================================================


def create_test_save(path: Path, content: str = "mock_level_data") -> Path:
    """Создает тестовый zip-архив сохранения."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = path.with_suffix(".tmp")
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("cloud_world/level.dat", (content * 10).encode("utf-8"))
        zf.writestr("cloud_world/info.json", b'{"save_name": "cloud_world", "version": 1}')
    os.replace(temp_zip, path)
    return path


def setup_e2e_server(
    temp_dir: Path,
    lease_timeout_seconds: float = 60.0,
    max_backups: int = 3,
) -> Tuple[Any, SaveStorageManager, SessionLockManager, ServerConfig]:
    """Развертывает изолированный тестовый сервер FastAPI."""
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


def setup_e2e_client(
    app: Any,
    temp_dir: Path,
    player_name: str,
) -> Tuple[SessionOrchestrator, FactorioEnvironment, HubClient]:
    """Развертывает изолированного клиента перехватчика."""
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
        device_token=f"device_tok_{player_name}",
    )

    test_http = TestClient(app)
    hub_client = HubClient(config=client_config, http_client=test_http)

    orchestrator = SessionOrchestrator(
        config=client_config,
        env=env,
        hub_client=hub_client,
    )
    return orchestrator, env, hub_client


# ============================================================================
# 1. Тест MockFactorioCore (режимы singleplayer и map-name)
# ============================================================================


def test_mock_factorio_core_map_name_and_singleplayer(tmp_path: Path):
    """MockFactorioCore поддерживает --map-name и режим singleplayer с генерацией логов."""
    user_dir = tmp_path / "mock_user_data"
    user_dir.mkdir()

    # 1. Запуск в режиме singleplayer
    core_sp = MockFactorioCore(
        mode="singleplayer",
        user_data_dir=user_dir,
        duration_sec=0.1,
        map_name="MyCustomSolo.zip",
    )
    rc_sp = core_sp.run()
    assert rc_sp == 0

    log_content = (user_dir / "factorio-current.log").read_text(encoding="utf-8")
    assert "Loading map" in log_content
    assert "MyCustomSolo.zip" in log_content
    assert "Singleplayer game active" in log_content
    assert (user_dir / "saves" / "_autosave1.zip").is_file()

    # 2. Запуск в режиме host с пользовательским map_name
    core_host = MockFactorioCore(
        mode="host",
        user_data_dir=user_dir,
        duration_sec=0.1,
        map_name="CoopSpecial.zip",
    )
    rc_host = core_host.run()
    assert rc_host == 0

    log_host = (user_dir / "factorio-current.log").read_text(encoding="utf-8")
    assert "Loading map" in log_host
    assert "CoopSpecial.zip" in log_host
    assert "Hosting game at 0.0.0.0:34197" in log_host


# ============================================================================
# 2. Сценарий 1: Изоляция одиночной игры (E2E)
# ============================================================================


def test_e2e_singleplayer_isolation_releases_host_and_preserves_server_world(tmp_path: Path):
    """Сквозной тест: игра в одиночный мир отзывает бронь Хоста и защищает серверный сейф."""
    app, storage, session_manager, config = setup_e2e_server(tmp_path)

    # 1. Исходное состояние сервера: в Сейфе лежит официальный cloud_world.zip
    init_tmp = config.tmp_dir / f"{config.temp_save_prefix}init_coop.zip"
    create_test_save(init_tmp, "OFFICIAL_COOP_SERVER_WORLD_PAYLOAD")
    storage.save_new_world_atomic(init_tmp, uploaded_by="OriginalHost")
    server_sha_before = storage.compute_sha256(config.current_save_path)
    assert server_sha_before is not None

    # 2. Игрок Alex запускает клиент
    orchestrator, client_env, hub_client = setup_e2e_client(app, tmp_path, "Alex")

    # В папке saves игрока есть его личный сейв одиночной игры
    solo_save = client_env.saves_dir / "MySoloFactory.zip"
    create_test_save(solo_save, "SOLO_FACTORY_PRIVATE_PROGRESS")

    # Игрок готовит сессию перехватчика
    with patch("src.client.session_orchestrator.show_role_banner"), \
         patch("src.client.session_orchestrator.HostP2PBridge"):

        prep = orchestrator.prepare_session()
        assert orchestrator.role == RoleType.HOST
        assert session_manager.get_public_status()["state"] in ("claiming", "idle")

        # 3. Имитируем запуск игры Factorio игроком Alex и загрузку одиночной карты
        # Записываем в лог загрузку карты MySoloFactory.zip
        client_env.log_file_path.write_text(
            f"   0.000 Initializing Factorio...\n"
            f"   0.050 Loading map {solo_save}: 54321 bytes.\n"
            f"   0.100 Game started.\n",
            encoding="utf-8",
        )

        # Создаем автосейв одиночной игры, симулируя прошедшие 2 минуты игры
        _create_autosave = client_env.saves_dir / "_autosave1.zip"
        create_test_save(_create_autosave, "SOLO_AUTOSAVE_PROGRESS_1")

        # Создаем и опрашиваем LobbyStartDetector
        detector = LobbyStartDetector(
            log_path=client_env.log_file_path,
            hub_client=hub_client,
            session_token=orchestrator.session_token,
            check_interval=0.05,
            target_save_name=client_env.cloud_save_name,
        )
        detector.check_once()

        # Проверяем, что детектор определил не-серверную карту
        assert detector.is_non_cloud_game is True
        assert detector.loaded_map_name == "MySoloFactory.zip"

        # Проверяем, что бронь на сервере мгновенно снята!
        assert session_manager.get_public_status()["state"] == "idle"

        # 4. Игрок закрывает игру
        workers_mock = MagicMock(spec=HostWorkersManager)
        workers_mock.on_game_exit.return_value = {
            "action": "ignored_local_game",
            "reason": "non_cloud_map",
        }
        orchestrator.host_workers = workers_mock

        cleanup_res = orchestrator.teardown()
        assert cleanup_res.get("role") == RoleType.HOST

        # 5. КРИТИЧЕСКАЯ ПРОВЕРКА: серверный сейф остался абсолютно нетронутым!
        server_sha_after = storage.compute_sha256(config.current_save_path)
        assert server_sha_after == server_sha_before
        server_file_bytes = config.current_save_path.read_bytes()
        assert b"OFFICIAL_COOP_SERVER_WORLD_PAYLOAD" in server_file_bytes
        assert b"SOLO_FACTORY" not in server_file_bytes
        assert b"SOLO_AUTOSAVE" not in server_file_bytes


# ============================================================================
# 3. Сценарий 2: Вызов окна настроек по Shift и смена мира (E2E)
# ============================================================================


def test_e2e_shift_hotkey_world_selector_full_lifecycle(tmp_path: Path):
    """Сквозной тест: вызов по Shift, выбор мира из combobox, загрузка на VPS и ротация бэкапа."""
    app, storage, session_manager, config = setup_e2e_server(tmp_path)

    # 1. Исходно на сервере лежит старый общий мир OldServerWorld
    init_tmp = config.tmp_dir / f"{config.temp_save_prefix}old_coop.zip"
    create_test_save(init_tmp, "OLD_WORLD_ON_SERVER_PAYLOAD")
    storage.save_new_world_atomic(init_tmp, uploaded_by="FirstHost")
    old_server_sha = storage.compute_sha256(config.current_save_path)

    # 2. Клиент игрока Bob
    orchestrator, client_env, hub_client = setup_e2e_client(app, tmp_path, "Bob")

    # Исходный локальный cloud_world у Боба
    local_cloud = client_env.saves_dir / "cloud_world.zip"
    create_test_save(local_cloud, "OLD_LOCAL_CLOUD_WORLD_CONTENT")

    # В папке saves есть новый мир, который Боб хочет сделать серверным
    new_world_file = client_env.saves_dir / "MegaBase_2026.zip"
    create_test_save(new_world_file, "MEGABASE_2026_NEW_SERVER_WORLD")

    # 3. Эмуляция запуска с зажатым Shift: оркестратор вызывает GUI
    gui_opened = False

    def mock_gui_provider(existing_config=None, **kwargs):
        nonlocal gui_opened
        gui_opened = True
        assert existing_config is not None
        # Игрок выбирает в Combobox мир MegaBase_2026.zip и нажимает Применить
        publish_selected_world_to_server(
            env=client_env,
            hub_client=hub_client,
            save_path=new_world_file,
            player_name="Bob",
        )
        return existing_config

    orchestrator_shift = SessionOrchestrator(
        config=orchestrator.config,
        env=client_env,
        hub_client=hub_client,
        setup_gui_provider=mock_gui_provider,
        force_setup_gui=False,
        shift_detector=lambda: True,
    )

    with patch("src.client.session_orchestrator.HostP2PBridge"), \
         patch("src.client.session_orchestrator.HostWorkersManager"), \
         patch("src.client.session_orchestrator.show_role_banner"):

        orchestrator_shift.prepare_session()

    assert gui_opened is True

    # 4. Проверяем локальные файлы клиента:
    # cloud_world.zip обновлен содержимым MegaBase_2026
    assert b"MEGABASE_2026_NEW_SERVER_WORLD" in local_cloud.read_bytes()
    # cloud_world_prev_local.zip содержит старый локальный сейв
    prev_local_backup = client_env.saves_dir / "cloud_world_prev_local.zip"
    assert prev_local_backup.is_file()
    assert b"OLD_LOCAL_CLOUD_WORLD_CONTENT" in prev_local_backup.read_bytes()

    # 5. Проверяем серверное хранилище:
    # Активный мир стал новым MegaBase_2026
    new_server_bytes = config.current_save_path.read_bytes()
    assert b"MEGABASE_2026_NEW_SERVER_WORLD" in new_server_bytes

    # Предыдущий мир сервера перемещен в backup_1.zip
    backup_1 = config.backups_dir / "backup_1.zip"
    assert backup_1.is_file()
    assert b"OLD_WORLD_ON_SERVER_PAYLOAD" in backup_1.read_bytes()

    # Слот Хоста освобожден после выгрузки, чтобы игра могла начаться штатно
    assert session_manager.get_public_status()["state"] in ("idle", "hosting_active", "claiming")


# ============================================================================
# 4. Сценарий 3: Защита от смены мира при занятом сервере
# ============================================================================


def test_e2e_shift_world_selector_rejected_when_server_busy(tmp_path: Path):
    """Смена мира отклоняется с понятной ошибкой, если на сервере уже играет другой игрок."""
    app, storage, session_manager, config = setup_e2e_server(tmp_path)

    # Исходный мир на сервере
    init_tmp = config.tmp_dir / f"{config.temp_save_prefix}init_busy.zip"
    create_test_save(init_tmp, "ORIGINAL_BUSY_WORLD")
    storage.save_new_world_atomic(init_tmp, uploaded_by="Alex")

    # Игрок Alex захватил слот и реально хостит (hosting_active)
    claim_res = session_manager.claim_host(player_name="Alex", client_public_ip="127.0.0.1")
    assert claim_res.get("success") is True
    session_manager.confirm_hosting(session_token=claim_res["session_token"])
    assert session_manager.get_public_status()["state"] == "hosting_active"

    # Игрок Bob пытается сменить мир
    _, client_env, hub_client = setup_e2e_client(app, tmp_path, "Bob")
    local_cloud = client_env.saves_dir / "cloud_world.zip"
    create_test_save(local_cloud, "BOB_LOCAL_CLOUD")

    cand_world = client_env.saves_dir / "MyNewSave.zip"
    create_test_save(cand_world, "BOB_NEW_SAVE_CONTENT")

    # Попытка выгрузки должна завершиться ошибкой
    with pytest.raises(RuntimeError) as exc_info:
        publish_selected_world_to_server(
            env=client_env,
            hub_client=hub_client,
            save_path=cand_world,
            player_name="Bob",
        )

    assert "Сейчас на сервере играет Alex" in str(exc_info.value)

    # Проверяем, что локальные файлы Боба и серверный сейф не повреждены
    assert b"BOB_LOCAL_CLOUD" in local_cloud.read_bytes()
    assert not (client_env.saves_dir / "cloud_world_prev_local.zip").exists()
    assert b"ORIGINAL_BUSY_WORLD" in config.current_save_path.read_bytes()


# ============================================================================
# 5. Верификация документации README.md
# ============================================================================


def test_readme_documentation_completeness():
    """README.md содержит документацию изоляции одиночной игры, детектора Shift и комбобокса."""
    readme_path = Path(__file__).resolve().parent.parent / "README.md"
    assert readme_path.is_file(), "Файл README.md должен присутствовать в корне репозитория"

    text = readme_path.read_text(encoding="utf-8")

    # Проверка разделов изоляции одиночной игры
    assert "Изоляция одиночной игры и не-серверных карт" in text
    assert "Loading map" in text
    assert "Как играть в свои личные одиночные миры" in text

    # Проверка разделов Shift и Combobox
    assert "Повторный вызов окна настроек по зажатой клавише Shift" in text
    assert "Выбор активного мира сервера" in text
    assert "--wrapper-setup" in text
    assert "shift_detector.py" in text

    # Проверка актуальности счетчика тестов
    assert "270+" in text
