"""Комплексный набор интеграционных и модульных тестов для HubClient.

Покрывает чек-лист тест-плана T7.1 - T7.6:
- T7.1: Опрос статуса и wait_if_uploading_final (ожидание и таймаут).
- T7.2: Жизненный цикл бронирования хоста claim_host (idle, занято, автоожидание при uploading_final).
- T7.3: Управление сессией хоста (confirm_hosting, send_heartbeat, release_host).
- T7.4: Синхронизация сохранения перед хостом sync_save_before_host
        (no_server_save, дедупликация SHA-256, скачивание с бэкапом, сверка модов, .cloud_stash).
- T7.5: Выгрузка сейва upload_save (успех, release_on_success, защита от Split-Brain 403 Forbidden, ретраи).
- T7.6: Регистрация и дерегистрация гостей.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx

from starlette.testclient import TestClient

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.hub_client import HubClient, compute_file_sha256
from src.server.api import create_app
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager
from src.server.storage import SaveStorageManager


class TestHubClientIntegration(unittest.TestCase):
    """Интеграционные тесты HubClient против реального FastAPI сервера."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.server_dir = Path(self.temp_dir) / "server"
        self.client_dir = Path(self.temp_dir) / "client"
        self.server_dir.mkdir(parents=True)
        self.client_dir.mkdir(parents=True)

        # Конфигурация сервера
        self.server_config = ServerConfig(
            storage_dir=self.server_dir / "saves",
            http_port=8088,
            udp_stun_port=14197,
            lease_timeout_seconds=90,
        )
        self.storage = SaveStorageManager(self.server_config)
        self.session_manager = SessionLockManager(self.server_config)
        self.app = create_app(
            config=self.server_config,
            storage=self.storage,
            session_manager=self.session_manager,
            enable_stun=False,
        )

        # In-process TestClient для мгновенного и надежного сетевого взаимодействия
        self.http_transport_client = TestClient(self.app)

        # Конфигурация клиента
        self.client_config = ClientConfig(
            hub_http_url="http://testserver",
            hub_stun_host="127.0.0.1",
            hub_stun_port=14197,
        )
        self.hub_client = HubClient(
            config=self.client_config,
            http_client=self.http_transport_client,
        )
        self.env = FactorioEnvironment(custom_user_data_dir=self.client_dir)
        self.env.ensure_dirs()

    def tearDown(self) -> None:
        self.hub_client.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_valid_factorio_zip(self, file_path: Path, marker: str = "v1") -> Path:
        """Создает валидный тестовый архив сохранения Factorio."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(file_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("test_world/level.dat", f"Binary Level Data: {marker}".encode("utf-8") * 5)
            zf.writestr("test_world/info.json", json.dumps({"name": marker, "version": "2.0"}))
            zf.writestr("test_world/control.lua", "-- Lua script\n")
        return file_path

    # =========================================================================
    # T7.1: Опрос статуса и wait_if_uploading_final
    # =========================================================================

    def test_t7_1_get_status(self) -> None:
        """T7.1: get_status корректно считывает исходное состояние сервера."""
        status = self.hub_client.get_status()
        self.assertEqual(status["state"], "idle")
        self.assertFalse(status["save_exists"])
        self.assertIsNone(status["current_host"])

    def test_t7_1_wait_if_uploading_final_timeout(self) -> None:
        """T7.1: wait_if_uploading_final выходит по таймауту, если состояние не меняется."""
        # Переводим сервер в uploading_final вручную
        claim = self.session_manager.claim_host("Alice", "127.0.0.1")
        self.session_manager.confirm_hosting(claim["session_token"])
        self.session_manager.begin_final_upload(claim["session_token"])

        # Ожидание с маленьким таймаутом
        t0 = time.monotonic()
        status = self.hub_client.wait_if_uploading_final(max_wait_sec=0.2, poll_interval_sec=0.05)
        elapsed = time.monotonic() - t0

        self.assertGreaterEqual(elapsed, 0.15)
        self.assertEqual(status["state"], "uploading_final")

    def test_t7_1_wait_if_uploading_final_success(self) -> None:
        """T7.1: wait_if_uploading_final успешно определяет завершение выгрузки."""
        claim = self.session_manager.claim_host("Alice", "127.0.0.1")
        self.session_manager.confirm_hosting(claim["session_token"])
        self.session_manager.begin_final_upload(claim["session_token"])

        # Фоновый поток, освобождающий сервер через 0.1 сек
        def delayed_release():
            time.sleep(0.1)
            self.session_manager.release_host(claim["session_token"], reason="upload_done")

        th = threading.Thread(target=delayed_release)
        th.start()

        status = self.hub_client.wait_if_uploading_final(max_wait_sec=2.0, poll_interval_sec=0.03)
        th.join()

        self.assertEqual(status["state"], "idle")

    # =========================================================================
    # T7.2: Жизненный цикл бронирования хоста (claim_host)
    # =========================================================================

    def test_t7_2_claim_host_validation(self) -> None:
        """T7.2: Запрос бронирования с пустым именем вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.hub_client.claim_host("   ")

    def test_t7_2_claim_host_success(self) -> None:
        """T7.2: Успешный захват слота Хоста в состоянии idle."""
        resp = self.hub_client.claim_host("Alice")
        self.assertTrue(resp["claimed"])
        self.assertEqual(resp["reason"], "slot_acquired")
        self.assertIsNotNone(resp["session_token"])

        # Сервер перешел в claiming
        status = self.hub_client.get_status()
        self.assertEqual(status["state"], "claiming")
        self.assertEqual(status["current_host"], "Alice")

    def test_t7_2_claim_host_already_hosting(self) -> None:
        """T7.2: Запрос отклоняется, если уже есть активный Хост."""
        self.hub_client.claim_host("Alice")
        # Второй игрок пытается захватить
        resp = self.hub_client.claim_host("Bob")
        self.assertFalse(resp["claimed"])
        self.assertIn(resp["reason"], ("already_hosted", "slot_busy"))

    def test_t7_2_claim_host_auto_wait_uploading_final(self) -> None:
        """T7.2: claim_host с wait_for_upload автоматически ждет завершения uploading_final и захватывает слот."""
        claim_alice = self.session_manager.claim_host("Alice", "127.0.0.1")
        self.session_manager.confirm_hosting(claim_alice["session_token"])
        self.session_manager.begin_final_upload(claim_alice["session_token"])

        # Фоновый поток завершает выгрузку через 0.1 сек
        def finish_alice_upload():
            time.sleep(0.1)
            self.session_manager.release_host(claim_alice["session_token"], reason="save_committed")

        th = threading.Thread(target=finish_alice_upload)
        th.start()

        # Bob запрашивает хост - должен подождать и захватить
        resp = self.hub_client.claim_host("Bob", wait_for_upload=True, max_wait_sec=2.0)
        th.join()

        self.assertTrue(resp["claimed"])
        self.assertEqual(resp["reason"], "slot_acquired")
        self.assertIsNotNone(resp["session_token"])

        status = self.hub_client.get_status()
        self.assertEqual(status["current_host"], "Bob")

    # =========================================================================
    # T7.3: Управление сессией хоста (confirm, heartbeat, release)
    # =========================================================================

    def test_t7_3_confirm_hosting(self) -> None:
        """T7.3: confirm_hosting переводит сервер в hosting_active."""
        claim = self.hub_client.claim_host("Alice")
        token = claim["session_token"]

        conf = self.hub_client.confirm_hosting(token, udp_port=34197)
        self.assertTrue(conf["confirmed"])

        status = self.hub_client.get_status()
        self.assertEqual(status["state"], "hosting_active")

    def test_t7_3_send_heartbeat(self) -> None:
        """T7.3: send_heartbeat продлевает аренду слота хоста."""
        claim = self.hub_client.claim_host("Alice")
        token = claim["session_token"]
        self.hub_client.confirm_hosting(token)

        hb = self.hub_client.send_heartbeat(token)
        self.assertTrue(hb["alive"])
        self.assertEqual(hb["state"], "hosting_active")
        self.assertGreater(hb["lease_seconds"], 0)

    def test_t7_3_release_host(self) -> None:
        """T7.3: release_host переводит сервер в состояние idle."""
        claim = self.hub_client.claim_host("Alice")
        token = claim["session_token"]
        self.hub_client.confirm_hosting(token)

        released = self.hub_client.release_host(token, reason="clean_exit")
        self.assertTrue(released)

        status = self.hub_client.get_status()
        self.assertEqual(status["state"], "idle")
        self.assertIsNone(status["current_host"])

    def test_t7_3_invalid_token_rejected(self) -> None:
        """T7.3: Невалидный токен сессии отклоняется сервером."""
        with self.assertRaises(Exception):
            self.hub_client.confirm_hosting("invalid_token")

    # =========================================================================
    # T7.4: Синхронизация сохранения перед хостом (sync_save_before_host)
    # =========================================================================

    def test_t7_4_sync_save_no_server_save(self) -> None:
        """T7.4: Если на сервере нет сейва, sync_save_before_host возвращает no_server_save."""
        res = self.hub_client.sync_save_before_host(self.env)
        self.assertEqual(res["action"], "no_server_save")
        self.assertFalse(res["updated"])
        self.assertIsNone(res["sha256"])

    def test_t7_4_sync_save_download_when_missing_locally(self) -> None:
        """T7.4: Скачивание сейва, если его нет локально."""
        # Создаем и коммитим сейв на сервере
        server_zip = self.server_dir / "init_save.zip"
        self._create_valid_factorio_zip(server_zip, marker="world_alpha")
        self.storage.save_new_world_atomic(server_zip, uploaded_by="server_init")

        res = self.hub_client.sync_save_before_host(self.env)
        self.assertEqual(res["action"], "downloaded")
        self.assertTrue(res["updated"])

        local_file = self.env.saves_dir / "cloud_world.zip"
        self.assertTrue(local_file.is_file())
        self.assertEqual(compute_file_sha256(local_file), res["sha256"])

    def test_t7_4_sync_save_deduplication_already_up_to_date(self) -> None:
        """T7.4: При совпадении SHA-256 скачивание пропускается (deduplication)."""
        save_file = self._create_valid_factorio_zip(self.server_dir / "save_v1.zip", marker="sync_v1")
        self.storage.save_new_world_atomic(save_file, uploaded_by="server_init")

        # Первичное скачивание
        self.hub_client.sync_save_before_host(self.env)

        # Повторный вызов
        res = self.hub_client.sync_save_before_host(self.env)
        self.assertEqual(res["action"], "already_up_to_date")
        self.assertFalse(res["updated"])

    def test_t7_4_sync_save_updates_outdated_local_with_backup(self) -> None:
        """T7.4: При несовпадении хеша создается cloud_world_prev_local.zip и файл обновляется."""
        # Локальный устаревший сейв
        local_file = self.env.saves_dir / "cloud_world.zip"
        self._create_valid_factorio_zip(local_file, marker="old_local_save")
        old_local_sha = compute_file_sha256(local_file)

        # Серверный новый сейв
        server_file = self._create_valid_factorio_zip(self.server_dir / "new_server.zip", marker="new_server_save")
        new_server_sha = compute_file_sha256(server_file)
        self.storage.save_new_world_atomic(server_file, uploaded_by="server_init")

        res = self.hub_client.sync_save_before_host(self.env)
        self.assertEqual(res["action"], "downloaded")
        self.assertTrue(res["updated"])
        self.assertEqual(compute_file_sha256(local_file), new_server_sha)

        # Проверяем бэкап старого локального файла в stash
        backup_file = self.env.cloud_stash_dir / "cloud_world_prev_local.zip"
        self.assertTrue(backup_file.is_file())
        self.assertEqual(compute_file_sha256(backup_file), old_local_sha)

    def test_t7_4_sync_save_detects_mods_mismatch(self) -> None:
        """T7.4: Выявление модов, включенных на сервере, но отсутствующих локально."""
        save_file = self._create_valid_factorio_zip(self.server_dir / "save_mods.zip")
        mods_manifest = [
            {"name": "base", "enabled": True},
            {"name": "space-age", "enabled": True},
            {"name": "quality", "enabled": True},
        ]
        self.storage.save_new_world_atomic(save_file, uploaded_by="server_init", mod_list=mods_manifest)

        # Локально установлен только base
        local_mods = {"mods": [{"name": "base", "enabled": True}]}
        (self.env.mods_dir / "mod-list.json").write_text(json.dumps(local_mods), encoding="utf-8")

        res = self.hub_client.sync_save_before_host(self.env)
        self.assertIn("space-age", res["mods_mismatch"])
        self.assertIn("quality", res["mods_mismatch"])
        self.assertNotIn("base", res["mods_mismatch"])

    def test_t7_4_sync_save_restores_from_cloud_stash(self) -> None:
        """T7.4: sync_save_before_host возвращает спрятанный сейв из .cloud_stash/."""
        stash_file = self.env.cloud_stash_dir / "cloud_world.zip"
        self._create_valid_factorio_zip(stash_file, marker="stashed_save")

        self.hub_client.sync_save_before_host(self.env)

        # Файл должен вернуться в корень saves/
        root_file = self.env.saves_dir / "cloud_world.zip"
        self.assertTrue(root_file.is_file())

    # =========================================================================
    # T7.5: Выгрузка сейва (upload_save) и защита от Split-Brain
    # =========================================================================

    def test_t7_5_upload_save_non_existent(self) -> None:
        """T7.5: Выгрузка несуществующего файла вызывает FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            self.hub_client.upload_save("non_existent_file.zip", session_token="token")

    def test_t7_5_upload_save_invalid_zip(self) -> None:
        """T7.5: Выгрузка не-ZIP файла вызывает ValueError."""
        corrupt_file = Path(self.temp_dir) / "junk.zip"
        corrupt_file.write_bytes(b"not a real zip file")
        with self.assertRaises(ValueError):
            self.hub_client.upload_save(corrupt_file, session_token="token")

    def test_t7_5_upload_save_split_brain_shield_403(self) -> None:
        """T7.5: Попытка выгрузки с неавторизованным/чужим токеном вызывает PermissionError (403 Forbidden)."""
        valid_save = self._create_valid_factorio_zip(Path(self.temp_dir) / "upload_test.zip")
        with self.assertRaises(PermissionError) as ctx:
            self.hub_client.upload_save(valid_save, session_token="forged_token")
        self.assertIn("Защита от Split-Brain", str(ctx.exception))

    def test_t7_5_upload_save_success(self) -> None:
        """T7.5: Успешная выгрузка сохранения активным хостом."""
        claim = self.hub_client.claim_host("Alice")
        token = claim["session_token"]
        self.hub_client.confirm_hosting(token)

        save_path = self._create_valid_factorio_zip(Path(self.temp_dir) / "world_to_upload.zip", marker="final_v1")
        expected_sha = compute_file_sha256(save_path)
        mod_list = [{"name": "base", "enabled": True}]

        res = self.hub_client.upload_save(
            save_path,
            session_token=token,
            release_on_success=False,
            mod_list=mod_list,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["sha256"], expected_sha)
        self.assertFalse(res["released"])

        # Проверяем метаданные на сервере
        status = self.hub_client.get_status()
        self.assertTrue(status["save_exists"])
        self.assertEqual(status["save_metadata"]["sha256"], expected_sha)

    def test_t7_5_upload_save_with_release_on_success(self) -> None:
        """T7.5: Выгрузка с release_on_success=True переводит сервер в idle."""
        claim = self.hub_client.claim_host("Alice")
        token = claim["session_token"]
        self.hub_client.confirm_hosting(token)

        save_path = self._create_valid_factorio_zip(Path(self.temp_dir) / "final_save.zip", marker="final_exit")

        res = self.hub_client.upload_save(
            save_path,
            session_token=token,
            release_on_success=True,
        )

        self.assertTrue(res["success"])
        self.assertTrue(res["released"])

        status = self.hub_client.get_status()
        self.assertEqual(status["state"], "idle")
        self.assertIsNone(status["current_host"])

    def test_t7_5_upload_save_network_retry_success(self) -> None:
        """T7.5: Успешный ретрай при временном сбое сети."""
        claim = self.hub_client.claim_host("Alice")
        token = claim["session_token"]
        self.hub_client.confirm_hosting(token)

        save_path = self._create_valid_factorio_zip(Path(self.temp_dir) / "retry_save.zip")

        real_post = self.http_transport_client.post
        call_count = [0]

        def flaky_post(*args, **kwargs):
            if kwargs.get("files") and call_count[0] == 0:
                call_count[0] += 1
                raise httpx.ConnectError("Transient connection drop")
            return real_post(*args, **kwargs)

        with patch.object(self.http_transport_client, "post", side_effect=flaky_post):
            res = self.hub_client.upload_save(save_path, session_token=token, max_retries=3)
            self.assertTrue(res["success"])
            self.assertEqual(call_count[0], 1)

    # =========================================================================
    # T7.6: Регистрация гостей
    # =========================================================================

    def test_t7_6_guest_register_and_deregister(self) -> None:
        """T7.6: Регистрация и дерегистрация гостей в лобби."""
        reg = self.hub_client.register_guest("Bob", "1.0.0")
        self.assertEqual(reg["status"], "registered")
        self.assertEqual(reg["player_name"], "Bob")

        dereg = self.hub_client.deregister_guest("Bob")
        self.assertTrue(dereg)

    def test_t7_5_upload_save_with_progress_callback(self) -> None:
        """T7.5: Выгрузка сейва с вызовом progress_callback."""
        claim = self.hub_client.claim_host("Alice")
        token = claim["session_token"]
        self.hub_client.confirm_hosting(token)

        save_path = self._create_valid_factorio_zip(Path(self.temp_dir) / "prog_save.zip")
        progress_events = []

        def on_prog(uploaded: int, total: int, speed: float) -> None:
            progress_events.append((uploaded, total, speed))

        res = self.hub_client.upload_save(
            file_path=save_path,
            session_token=token,
            progress_callback=on_prog,
        )
        self.assertTrue(res["success"])
        self.assertTrue(len(progress_events) >= 1)
        last_uploaded, last_total, last_speed = progress_events[-1]
        self.assertEqual(last_uploaded, last_total)
        self.assertGreater(last_speed, 0)


if __name__ == "__main__":
    unittest.main()
