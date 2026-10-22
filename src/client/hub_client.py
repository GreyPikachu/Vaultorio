"""Клиентский модуль взаимодействия с HTTP API «Сейфа» (HubClient).

Реализует:
1. Захват и подтверждение роли Хоста с умным ожиданием (wait_if_uploading_final)
   при передаче эстафеты от предыдущего игрока.
2. Поддержание сессии (heartbeat, release_host, begin_final_upload).
3. Синхронизацию сохранения перед запуском игры (sync_save_before_host)
   с дедупликацией по SHA-256, бэкапом и сверкой модов.
4. Потоковую выгрузку сейва (upload_save) с защитой от Split-Brain (403 Forbidden)
   и повторными попытками при сетевых помехах.
5. Регистрацию и дерегистрацию гостей лобби.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

import httpx

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment

logger = logging.getLogger("factorio_client.hub")


def compute_file_sha256(file_path: Path | str, chunk_size: int = 65536) -> str:
    """Вычисляет SHA-256 хеш файла блоками по 64 КБ."""
    hasher = hashlib.sha256()
    path = Path(file_path)
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower()


class HubClient:
    """HTTP-клиент для взаимодействия с API сервера-сейфа Factorio."""

    def __init__(
        self,
        config: Optional[ClientConfig] = None,
        http_client: Optional[httpx.Client] = None,
        timeout: Optional[float] = None,
    ) -> None:
        """Инициализирует клиент.

        Args:
            config: Конфигурация клиента (если не передана, берется из окружения).
            http_client: Экземпляр httpx.Client (полезно для тестов с ASGITransport).
            timeout: Таймаут запросов в секундах.
        """
        self.config = config or ClientConfig.from_env()
        self.timeout = timeout or self.config.request_timeout_seconds

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                base_url=self.config.hub_http_url.rstrip("/"),
                timeout=self.timeout,
            )
            self._owns_client = True

    def close(self) -> None:
        """Закрывает HTTP-сессию."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HubClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # =========================================================================
    # Раздел 1: Статус сервера и умное ожидание эстафеты (uploading_final)
    # =========================================================================

    def get_status(self) -> dict[str, Any]:
        """Возвращает текущий статус сервера (состояние, хост, метаданные сейва)."""
        try:
            resp = self._client.get("/status")
            resp.raise_for_status()
            data = resp.json()

            # Нормализация данных для удобного плоского доступа в клиенте
            session = data.get("session") or {}
            save = data.get("save") or {}
            data.setdefault("state", session.get("state", "idle"))
            data.setdefault("current_host", session.get("host_name"))
            data.setdefault("save_exists", save.get("exists", False))
            data.setdefault("save_metadata", save)
            if "mod_list" not in data or data["mod_list"] is None:
                data["mod_list"] = save.get("mod_list")

            return data
        except Exception as e:
            logger.error(f"Ошибка при получении статуса сервера: {e}")
            raise

    def wait_if_uploading_final(
        self,
        max_wait_sec: float = 30.0,
        poll_interval_sec: float = 1.5,
    ) -> dict[str, Any]:
        """Ожидает завершения финальной выгрузки сейва предыдущим хостом.

        Если сервер находится в состоянии 'uploading_final', перехватчик опрашивает
        статус каждые poll_interval_sec секунд, пока сервер не перейдет в 'idle'.
        Это гарантирует, что новый Хост получит самый свежий финальный сейв,
        а не устаревший промежуточный автосейв.

        Args:
            max_wait_sec: Максимальное время ожидания в секундах.
            poll_interval_sec: Интервал между опросами статуса.

        Returns:
            Актуальный словарь статуса сервера после выхода из ожидания.
        """
        start_time = time.monotonic()
        last_status = self.get_status()

        def _is_uploading(st: dict[str, Any]) -> bool:
            return st.get("state") == "uploading_final" or st.get("session", {}).get("state") == "uploading_final"

        while _is_uploading(last_status):
            elapsed = time.monotonic() - start_time
            if elapsed >= max_wait_sec:
                logger.warning(
                    f"Превышено максимальное время ожидания uploading_final ({max_wait_sec} сек). "
                    f"Сервер все еще в состоянии uploading_final."
                )
                break

            logger.info(
                f"Предыдущий хост прямо сейчас выгружает финальный сейв в облако. "
                f"Ожидание передачи эстафеты ({elapsed:.1f}/{max_wait_sec} сек)..."
            )
            time.sleep(poll_interval_sec)
            last_status = self.get_status()

        return last_status

    # =========================================================================
    # Раздел 2: Управление сессией хоста (Claim, Confirm, Heartbeat, Release)
    # =========================================================================

    def claim_host(
        self,
        player_name: str,
        local_ip: Optional[str] = None,
        wait_for_upload: bool = True,
        max_wait_sec: float = 30.0,
    ) -> dict[str, Any]:
        """Запрашивает предварительную бронь роли Хоста на сервере.

        Если сервер возвращает 'uploading_final' и wait_for_upload=True, метод
        автоматически ожидает завершения выгрузки и повторяет попытку захвата.

        Args:
            player_name: Имя игрока.
            local_ip: Локальный IP адрес клиента (опционально).
            wait_for_upload: Ожидать ли при состоянии uploading_final.
            max_wait_sec: Максимальное время ожидания выгрузки.

        Returns:
            Ответ сервера (словарь с ключами success, claimed, session_token, reason и т.д.).
        """
        clean_name = player_name.strip()
        if not clean_name:
            raise ValueError("Имя игрока не может быть пустым при запросе брони хоста.")

        payload: dict[str, Any] = {"player_name": clean_name}
        if local_ip:
            payload["local_ip"] = local_ip

        try:
            resp = self._client.post("/claim", json=payload)
            resp.raise_for_status()
            data = resp.json()

            is_claimed = bool(data.get("success") or data.get("claimed", False))
            data["claimed"] = is_claimed
            if is_claimed:
                data.setdefault("reason", "slot_acquired")

            # Если сервер занят финальной выгрузкой сейва предыдущего игрока
            if not is_claimed and data.get("reason") == "uploading_final":
                if wait_for_upload:
                    logger.info("Обнаружена финальная выгрузка сейва. Запускаем автоматическое ожидание...")
                    status_after_wait = self.wait_if_uploading_final(max_wait_sec=max_wait_sec)
                    if (
                        status_after_wait.get("state") == "idle"
                        or status_after_wait.get("session", {}).get("state") == "idle"
                    ):
                        logger.info("Предыдущая выгрузка завершена! Повторяем запрос брони Хоста...")
                        retry_resp = self._client.post("/claim", json=payload)
                        retry_resp.raise_for_status()
                        retry_data = retry_resp.json()
                        retry_claimed = bool(
                            retry_data.get("success") or retry_data.get("claimed", False)
                        )
                        retry_data["claimed"] = retry_claimed
                        if retry_claimed:
                            retry_data.setdefault("reason", "slot_acquired")
                        return retry_data

            return data
        except Exception as e:
            logger.error(f"Ошибка при запросе брони роли Хоста для '{clean_name}': {e}")
            raise

    def confirm_hosting(
        self,
        session_token: str,
        udp_port: Optional[int] = None,
    ) -> dict[str, Any]:
        """Подтверждает реальный запуск сетевого лобби в меню игры.

        Переводит статус сессии из 'claiming' в 'hosting_active'.
        """
        payload: dict[str, Any] = {"session_token": session_token}
        if udp_port is not None:
            payload["udp_port"] = udp_port

        try:
            resp = self._client.post("/confirm-hosting", json=payload)
            resp.raise_for_status()
            data = resp.json()
            data["confirmed"] = bool(data.get("success") or data.get("confirmed", False))
            return data
        except Exception as e:
            logger.error(f"Ошибка при подтверждении старта хостинга: {e}")
            raise

    def begin_final_upload(self, session_token: str, ttl_seconds: int = 45) -> dict[str, Any]:
        """Уведомляет сервер о начале финальной выгрузки сейва при закрытии игры.

        Переводит статус сессии в 'uploading_final', предотвращая гонку сейвов.
        """
        payload = {"session_token": session_token}
        try:
            resp = self._client.post("/begin-final-upload", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Ошибка при вызове begin_final_upload: {e}")
            raise

    def send_heartbeat(
        self,
        session_token: str,
        udp_port: Optional[int] = None,
    ) -> dict[str, Any]:
        """Продлевает аренду слота Хоста во время активной игры (каждые 30 сек)."""
        payload: dict[str, Any] = {"session_token": session_token}
        if udp_port is not None:
            payload["udp_port"] = udp_port

        try:
            resp = self._client.post("/heartbeat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            session_info = data.get("status", {}).get("session", {})
            data.setdefault("alive", bool(data.get("success", False)))
            data.setdefault("state", session_info.get("state", "hosting_active"))
            data.setdefault("lease_seconds", session_info.get("lease_remaining_seconds", 90.0))
            return data
        except Exception as e:
            logger.warning(f"Не удалось отправить heartbeat: {e}")
            raise

    def release_host(self, session_token: str, reason: str = "normal_exit") -> bool:
        """Освобождает слот Хоста, возвращая сервер в состояние 'idle'."""
        payload = {"session_token": session_token, "reason": reason}
        try:
            resp = self._client.post("/release", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"Слот хоста успешно освобожден ({reason}): {data.get('message')}")
                return bool(data.get("success") or data.get("released", True))
            logger.warning(f"Сервер вернул код {resp.status_code} при освобождении слота.")
            return False
        except Exception as e:
            logger.warning(f"Ошибка при освобождении слота хоста: {e}")
            return False

    # =========================================================================
    # Раздел 3: Синхронизация сохранения перед игрой (sync_save_before_host)
    # =========================================================================

    def sync_save_before_host(
        self,
        env: FactorioEnvironment,
        status: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Синхронизирует облачное сохранение перед тем, как игрок станет Хостом.

        Алгоритм:
        1. Восстанавливает спрятанный сейв из .cloud_stash/ (если был Гостем).
        2. Сверяет SHA-256 локального файла с серверным (дедупликация).
        3. Если на сервере файл новее/отличается — скачивает в .part, валидирует,
           делает бэкап локального файла в cloud_world_prev_local.zip и атомарно заменяет.
        4. Сверяет манифест активных модов и выявляет отсутствующие.

        Args:
            env: Окружение Factorio с путями к папкам и файлам.
            status: Предварительно запрошенный статус (или None для автозапроса).

        Returns:
            Словарь с результатом синхронизации:
            - 'action': 'no_server_save' | 'already_up_to_date' | 'downloaded'
            - 'updated': bool
            - 'sha256': str | None
            - 'mods_mismatch': list[str] (список отсутствующих активных модов)
        """
        # Шаг 1: Восстановление файла, если прошлый сеанс был в роли Гостя
        env.restore_hidden_cloud_save()

        # Шаг 2: Проверка статуса сервера
        current_status = status or self.get_status()
        save_data = current_status.get("save") or current_status.get("save_metadata") or {}
        save_exists = current_status.get("save_exists") or save_data.get("exists", False)
        server_sha = save_data.get("sha256")

        if not save_exists or not server_sha:
            logger.info("На сервере еще нет облачного сохранения (первый запуск карты).")
            return {
                "action": "no_server_save",
                "updated": False,
                "sha256": None,
                "mods_mismatch": [],
            }

        server_sha = server_sha.lower()
        server_mods: list[dict[str, Any]] = (
            current_status.get("mod_list") or save_data.get("mod_list") or []
        )

        # Шаг 3: Проверка манифеста модов
        local_mods = env.get_active_mods_manifest()
        local_mod_names = {m.get("name") for m in local_mods if m.get("name")}

        missing_mods: list[str] = []
        for sm in server_mods:
            if isinstance(sm, dict) and sm.get("enabled", True):
                mod_name = sm.get("name")
                if mod_name and mod_name not in local_mod_names and mod_name != "base":
                    missing_mods.append(mod_name)

        if missing_mods:
            logger.warning(
                f"ВНИМАНИЕ: На сервере активны моды, которых нет в локальной игре: {missing_mods}!"
            )

        # Шаг 4: Проверка локального сохранения
        local_save_path = env.saves_dir / env.cloud_save_name
        if local_save_path.is_file():
            try:
                local_sha = compute_file_sha256(local_save_path)
                if local_sha == server_sha:
                    logger.info("Локальное сохранение полностью совпадает с серверным (SHA-256 идентичен).")
                    return {
                        "action": "already_up_to_date",
                        "updated": False,
                        "sha256": server_sha,
                        "mods_mismatch": missing_mods,
                    }
            except Exception as e:
                logger.warning(f"Не удалось вычислить SHA-256 локального сейва: {e}")

        # Шаг 5: Скачивание актуального сейва с сервера
        logger.info(f"Скачивание актуального сохранения {env.cloud_save_name} с сервера-сейфа...")
        env.saves_dir.mkdir(parents=True, exist_ok=True)
        part_path = env.saves_dir / f"{env.cloud_save_name}.part"

        try:
            with self._client.stream("GET", "/download-save") as resp:
                resp.raise_for_status()
                with part_path.open("wb") as out_f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        out_f.write(chunk)

            # Валидация скачанного файла
            downloaded_sha = compute_file_sha256(part_path)
            if downloaded_sha != server_sha:
                part_path.unlink(missing_ok=True)
                raise ValueError(
                    f"Хеш скачанного файла ({downloaded_sha}) не совпадает с серверным ({server_sha})!"
                )

            if not zipfile.is_zipfile(part_path):
                part_path.unlink(missing_ok=True)
                raise ValueError("Скачанный файл не является валидным ZIP-архивом.")

            # Резервное копирование старого локального файла (если был)
            if local_save_path.is_file():
                backup_local = env.saves_dir / "cloud_world_prev_local.zip"
                shutil.copy2(local_save_path, backup_local)
                logger.info(f"Предыдущий локальный сейв сохранен в {backup_local.name}")

            # Атомарная замена
            os.replace(part_path, local_save_path)
            logger.info(f"Облачное сохранение успешно обновлено: {local_save_path.name} ({server_sha[:8]}...)")

            return {
                "action": "downloaded",
                "updated": True,
                "sha256": server_sha,
                "mods_mismatch": missing_mods,
            }
        except Exception as e:
            part_path.unlink(missing_ok=True)
            logger.error(f"Ошибка при скачивании сейва с сервера: {e}")
            raise

    def download_save(
        self,
        target_path: Path | str,
        expected_sha: Optional[str] = None,
        chunk_size: int = 65536,
    ) -> Path:
        """Скачивает файл сохранения с сервера-сейфа по указанному пути.

        Скачивание производится через временный файл .part с валидацией ZIP-архива
        и SHA-256 (если передан expected_sha), после чего файл атомарно перемещается.

        Args:
            target_path: Путь для сохранения скачанного файла.
            expected_sha: Ожидаемый SHA-256 хеш (если известен).
            chunk_size: Размер блока потокового чтения.

        Returns:
            Итоговый путь к скачанному файлу.
        """
        dest = Path(target_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        part_path = dest.with_suffix(dest.suffix + ".part")

        try:
            with self._client.stream("GET", "/download-save") as resp:
                resp.raise_for_status()
                with part_path.open("wb") as out_f:
                    for chunk in resp.iter_bytes(chunk_size=chunk_size):
                        out_f.write(chunk)

            if expected_sha:
                downloaded_sha = compute_file_sha256(part_path)
                if downloaded_sha.lower() != expected_sha.lower():
                    part_path.unlink(missing_ok=True)
                    raise ValueError(
                        f"Хеш скачанного файла ({downloaded_sha}) не совпадает с ожидаемым ({expected_sha})!"
                    )

            if not zipfile.is_zipfile(part_path):
                part_path.unlink(missing_ok=True)
                raise ValueError("Скачанный файл не является валидным ZIP-архивом Factorio.")

            os.replace(part_path, dest)
            logger.info(f"Сейф успешно скачан: {dest.name}")
            return dest
        except Exception as e:
            part_path.unlink(missing_ok=True)
            logger.error(f"Ошибка при скачивании сейва в {dest}: {e}")
            raise

    # =========================================================================
    # Раздел 4: Выгрузка сохранения в облако (upload_save)
    # =========================================================================

    def upload_save(
        self,
        file_path: Path | str,
        session_token: str,
        release_on_success: bool = False,
        mod_list: Optional[list[dict[str, Any]]] = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Выгружает файл сохранения на сервер-сейф с защитой от Split-Brain.

        Args:
            file_path: Путь к архиву сохранения Factorio (.zip).
            session_token: Активный токен сессии Хоста.
            release_on_success: Если True, предварительно вызывает begin_final_upload
                                и после выгрузки переводит сервер в idle.
            mod_list: Манифест активных модов.
            max_retries: Число повторных попыток при сетевых сбоях.

        Returns:
            Ответ сервера (словарь с success, sha256, backups_count, released).
        """
        path = Path(file_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Файл сохранения для выгрузки не найден: {path}")

        if not zipfile.is_zipfile(path):
            raise ValueError(f"Файл {path} не является валидным ZIP-архивом.")

        expected_sha = compute_file_sha256(path)

        # Если это финальная выгрузка, сначала выставляем статус uploading_final
        if release_on_success:
            try:
                self.begin_final_upload(session_token)
            except Exception as e:
                logger.warning(f"Не удалось предварительно выставить begin_final_upload: {e}")

        mod_list_json = json.dumps(mod_list, ensure_ascii=False) if mod_list is not None else None

        last_exc: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    f"Выгрузка сейва {path.name} (SHA: {expected_sha[:8]}..., "
                    f"попытка {attempt}/{max_retries})..."
                )

                with path.open("rb") as f:
                    files = {"file": (path.name, f, "application/zip")}
                    data = {
                        "session_token": session_token,
                        "release_on_success": "true" if release_on_success else "false",
                        "release_host": "true" if release_on_success else "false",
                    }
                    if mod_list_json:
                        data["mod_list_json"] = mod_list_json

                    headers = {"X-Expected-SHA256": expected_sha}

                    resp = self._client.post(
                        "/upload-save",
                        data=data,
                        files=files,
                        headers=headers,
                    )

                if resp.status_code == 403:
                    # Защита от Split-Brain: чужая сессия, немедленно прерываем
                    msg = (
                        "ОТКАЗ В ВЫГРУЗКЕ (403 Forbidden): Защита от Split-Brain! "
                        "Токен сессии недействителен или хост уже был перехвачен."
                    )
                    logger.error(msg)
                    raise PermissionError(msg)

                if resp.status_code in (400, 422):
                    # Ошибка валидации структуры файла или параметров
                    err_msg = f"Сервер отклонил архив сохранения (код {resp.status_code}): {resp.text}"
                    logger.error(err_msg)
                    raise ValueError(err_msg)

                resp.raise_for_status()
                result = resp.json()
                save_data = result.get("save") or {}
                status_data = result.get("status") or {}
                session_data = status_data.get("session") or {}
                result.setdefault("sha256", save_data.get("sha256", expected_sha))
                result.setdefault("backups_count", save_data.get("backups_count", 0))
                is_released = (
                    status_data.get("state") == "idle"
                    or session_data.get("state") == "idle"
                )
                result.setdefault("released", is_released)
                logger.info(
                    f"Сейф успешно принял сохранение! Бэкапов на сервере: {result.get('backups_count', 0)}, "
                    f"слот освобожден: {result.get('released', False)}"
                )
                return result

            except PermissionError:
                raise
            except ValueError:
                raise
            except Exception as e:
                last_exc = e
                logger.warning(f"Попытка {attempt} выгрузки сейва завершилась ошибкой: {e}")
                if attempt < max_retries:
                    time.sleep(1.0 * attempt)

        raise ConnectionError(
            f"Не удалось выгрузить сохранение на сервер после {max_retries} попыток: {last_exc}"
        )

    # =========================================================================
    # Раздел 5: Регистрация и дерегистрация гостей лобби
    # =========================================================================

    def register_guest(self, player_name: str, client_version: str = "1.0.0") -> dict[str, Any]:
        """Регистрирует Гостя в активном списке лобби на сервере."""
        payload = {"guest_name": player_name.strip()}
        try:
            resp = self._client.post("/guests/register", json=payload)
            resp.raise_for_status()
            data = resp.json()
            data["player_name"] = player_name.strip()
            data["status"] = "registered" if data.get("success") else "failed"
            return data
        except Exception as e:
            logger.warning(f"Не удалось зарегистрировать гостя '{player_name}': {e}")
            raise

    def deregister_guest(self, player_name: str) -> bool:
        """Удаляет Гостя из списка активных подключений лобби."""
        payload = {"guest_name": player_name.strip()}
        try:
            resp = self._client.post("/guests/deregister", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                return bool(data.get("success", True))
            return False
        except Exception as e:
            logger.warning(f"Ошибка при дерегистрации гостя '{player_name}': {e}")
            return False
