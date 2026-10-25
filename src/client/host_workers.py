"""Модуль фоновых рабочих процессов Хоста для Factorio P2P.

Включает:
- LobbyStartDetector: Детектор реального запуска сетевого мира (защита от «Фальшивого Хоста»).
- find_latest_session_save: Поиск самого свежего сохранения сессии (подхват _autosaveN.zip).
- HeartbeatWorker: Фоновое продление аренды слота Хоста на сервере.
- AutosaveWatcher: Фоновый демон регулярной выгрузки автосейвов (Rolling Sync).
- flush_final_save_on_exit: Финализация сейва при закрытии игры и освобождение слота.
- HostWorkersManager: Комплексный оркестратор фоновых воркеров Хоста.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

from src.client.hub_client import HubClient, compute_file_sha256
from src.client.chronicle_watcher import ChronicleWatcher

logger = logging.getLogger("factorio_hub.host_workers")


def parse_loaded_map_from_log(log_text: str) -> str | None:
    """Парсит имя загруженной карты (.zip) из фрагмента factorio-current.log.

    Движок Factorio при загрузке любого сохранения пишет в лог:
    Loading map /path/to/saves/<map_name>.zip
    или на Windows:
    Loading map C:\\Users\\...\\saves\\<map_name>.zip

    Args:
        log_text: Текстовый фрагмент лога Factorio.

    Returns:
        Имя файла сохранения (например, 'cloud_world.zip' или 'PersonalBase.zip')
        или None, если событие загрузки карты не найдено.
    """
    if not log_text or not isinstance(log_text, str):
        return None

    # Ищем паттерн 'Loading map' с путем до .zip файла
    # Поддерживает и Unix /, и Windows \\ разделители
    pattern = r"Loading map\s+(?:.*[/\\])?([^/\\\r\n]+\.zip)"
    matches = re.findall(pattern, log_text, flags=re.IGNORECASE)
    if not matches:
        return None

    # Возвращаем имя последней загруженной карты в этом фрагменте
    latest_map = matches[-1].strip()
    return latest_map if latest_map else None


def find_latest_session_save(
    saves_dir: Path,
    session_start_time: float,
    min_mtime_margin: float = 2.0,
    allowed_save_names: set[str] | list[str] | None = None,
) -> Path | None:
    """Ищет самый свежий валидный ZIP-архив сохранения, созданный или измененный за текущую сессию.

    Сравнивает время модификации файлов с (session_start_time - min_mtime_margin),
    проверяет целостность архива через zipfile.is_zipfile и возвращает путь
    к файлу с максимальным st_mtime.
    При указании allowed_save_names учитывает только указанные файлы и файлы автосейвов (_autosave*.zip).
    """
    if not isinstance(saves_dir, Path):
        saves_dir = Path(saves_dir)

    if not saves_dir.is_dir():
        logger.debug(f"Каталог сохранений не найден: {saves_dir}")
        return None

    allowed_lower: set[str] | None = None
    if allowed_save_names:
        allowed_lower = {n.lower() for n in allowed_save_names}

    cutoff_mtime = session_start_time - min_mtime_margin
    candidates: list[tuple[float, Path]] = []

    try:
        for entry in saves_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.name.startswith("."):
                continue
            if not entry.name.lower().endswith(".zip"):
                continue

            entry_lower = entry.name.lower()
            if allowed_lower is not None:
                if entry_lower not in allowed_lower and not entry_lower.startswith("_autosave"):
                    continue

            try:
                stat = entry.stat()
                if stat.st_mtime >= cutoff_mtime and stat.st_size > 0:
                    # Проверяем, что файл дозаписан и является валидным ZIP
                    if zipfile.is_zipfile(entry):
                        candidates.append((stat.st_mtime, entry))
                    else:
                        logger.debug(f"Файл {entry.name} не является валидным ZIP (возможно, пишется игрой)")
            except (OSError, PermissionError) as err:
                logger.debug(f"Ошибка доступа к файлу сохранения {entry.name}: {err}")
                continue
    except Exception as err:
        logger.warning(f"Ошибка сканирования каталога {saves_dir}: {err}")
        return None

    if not candidates:
        return None

    # Сортируем по st_mtime (самый свежий первый)
    candidates.sort(key=lambda item: item[0], reverse=True)
    freshest_path = candidates[0][1]
    logger.info(f"Найден свежий сейв сессии: {freshest_path.name} (mtime: {candidates[0][0]})")
    return freshest_path


class LobbyStartDetector:
    """Детектор реального старта сетевого лобби.

    Предотвращает проблему «Фальшивого Хоста». Мониторит лог игры factorio-current.log,
    локальный UDP-порт 34197 и изменения сохранений.
    При подтверждении запуска переводит статус на сервере в hosting_active.
    При превышении timeout_sec без запуска снимает бронь Хоста.
    """

    def __init__(
        self,
        log_path: Path,
        hub_client: HubClient,
        session_token: str,
        factorio_port: int = 34197,
        saves_dir: Path | None = None,
        check_interval: float = 1.0,
        timeout_sec: float = 175.0,
        session_start_time: float | None = None,
        on_confirmed: Callable[[], None] | None = None,
        on_timeout: Callable[[], None] | None = None,
        target_save_name: str = "cloud_world.zip",
        on_non_cloud_game: Callable[[str], None] | None = None,
    ) -> None:
        if not session_token:
            raise ValueError("session_token не может быть пустым")

        self.log_path = Path(log_path) if not isinstance(log_path, Path) else log_path
        self.hub_client = hub_client
        self.session_token = session_token
        self.factorio_port = factorio_port
        self.saves_dir = Path(saves_dir) if saves_dir and not isinstance(saves_dir, Path) else saves_dir
        self.check_interval = max(0.01, float(check_interval))
        self.timeout_sec = max(0.05, float(timeout_sec))
        self.session_start_time = float(session_start_time if session_start_time is not None else time.time())
        self.on_confirmed = on_confirmed
        self.on_timeout = on_timeout
        self.target_save_name = str(target_save_name or "cloud_world.zip")
        self.on_non_cloud_game = on_non_cloud_game

        self._is_confirmed = False
        self._is_timed_out = False
        self._is_non_cloud_game = False
        self._loaded_map_name: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._log_seek_pos = 0

        # Указатель чтения лога: по умолчанию читаем с начала (0), чтобы не упустить
        # строки старта лобби (Hosting game at), записанные игрой в первые миллисекунды.
        # Если лог существовал задолго до старта сессии, начинаем с текущего конца.
        if self.log_path.exists():
            try:
                stat = self.log_path.stat()
                if stat.st_mtime < (self.session_start_time - 15.0):
                    self._log_seek_pos = stat.st_size
                else:
                    self._log_seek_pos = 0
            except OSError:
                self._log_seek_pos = 0

    @property
    def is_confirmed(self) -> bool:
        with self._lock:
            return self._is_confirmed

    @property
    def is_timed_out(self) -> bool:
        with self._lock:
            return self._is_timed_out

    @property
    def is_non_cloud_game(self) -> bool:
        with self._lock:
            return self._is_non_cloud_game

    @property
    def loaded_map_name(self) -> str | None:
        with self._lock:
            return self._loaded_map_name

    def check_once(self) -> bool:
        """Выполняет разовую проверку индикаторов старта лобби или истечения таймаута."""
        with self._lock:
            if self._is_confirmed:
                return True
            if self._is_timed_out:
                return False
            if self._is_non_cloud_game:
                return False

        # Проверка таймаута
        now = time.time()
        elapsed = now - self.session_start_time
        if elapsed >= self.timeout_sec:
            logger.warning(
                f"Таймаут ожидания старта лобби ({elapsed:.1f}с >= {self.timeout_sec}с). "
                "Освобождаем слот Хоста на сервере."
            )
            with self._lock:
                self._is_timed_out = True

            try:
                self.hub_client.release_host(self.session_token, reason="menu_idle_timeout")
            except Exception as err:
                logger.error(f"Ошибка при снятии брони по таймауту: {err}")

            if self.on_timeout:
                try:
                    self.on_timeout()
                except Exception as cb_err:
                    logger.error(f"Ошибка в callback on_timeout: {cb_err}")
            return False

        detected = False
        detection_reason = ""

        # 1. Проверка лог-файла factorio-current.log
        if self.log_path.exists():
            try:
                with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._log_seek_pos)
                    new_content = f.read()
                    self._log_seek_pos = f.tell()

                if new_content:
                    # Проверяем, какую карту загрузил игрок
                    loaded_map = parse_loaded_map_from_log(new_content)
                    if loaded_map:
                        with self._lock:
                            self._loaded_map_name = loaded_map
                        clean_loaded = loaded_map.lower()
                        clean_target = self.target_save_name.lower()
                        if clean_loaded != clean_target and not clean_loaded.startswith("_autosave"):
                            logger.info(
                                f"Игрок загрузил локальный не-серверный мир '{loaded_map}' вместо '{self.target_save_name}'. "
                                "Освобождаем слот Хоста на сервере и отключаем сетевую синхронизацию."
                            )
                            with self._lock:
                                self._is_non_cloud_game = True
                                self._is_confirmed = False

                            try:
                                self.hub_client.release_host(self.session_token, reason=f"local_map:{loaded_map}")
                            except Exception as rel_err:
                                logger.warning(f"Ошибка при освобождении слота (local_map): {rel_err}")

                            if self.on_non_cloud_game:
                                try:
                                    self.on_non_cloud_game(loaded_map)
                                except Exception as cb_err:
                                    logger.error(f"Ошибка в callback on_non_cloud_game: {cb_err}")
                            return False

                    keywords = [
                        "Hosting game at",
                        "ServerRouter",
                        "Opening socket for listening on port",
                        "Info ServerMultiplayerManager.cpp",
                        "Info ServerMultiplayerManager",
                    ]
                    for kw in keywords:
                        if kw.lower() in new_content.lower():
                            detected = True
                            detection_reason = f"лог содержит '{kw}'"
                            break
            except Exception as err:
                logger.debug(f"Ошибка чтения лога {self.log_path}: {err}")

        # 2. Проверка UDP-сокета Factorio (порт занят игрой)
        if not detected and self.factorio_port:
            try:
                # Пробуем открыть временный UDP-сокет на этом же порту.
                # Если порт уже занят процессом Factorio, bind вызовет OSError (EADDRINUSE).
                test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    test_sock.bind(("127.0.0.1", self.factorio_port))
                    # Порт свободен — игра ещё не слушает сокет
                    test_sock.close()
                except OSError:
                    # Порт занят! Игровой сервер запустился и держит сокет
                    test_sock.close()
                    detected = True
                    detection_reason = f"порт UDP {self.factorio_port} занят процессом игры"
            except Exception as err:
                logger.debug(f"Ошибка проверки UDP-порта {self.factorio_port}: {err}")

        with self._lock:
            if self._is_non_cloud_game:
                return False

        if detected:
            logger.info(f"Обнаружен реальный запуск сетевого лобби ({detection_reason})")
            confirm_success = False
            try:
                resp = self.hub_client.confirm_hosting(self.session_token, udp_port=self.factorio_port)
                logger.info(f"Хостинг успешно подтвержден на сервере: {resp}")
                confirm_success = True
            except Exception as err:
                logger.error(f"Не удалось подтвердить хостинг на сервере: {err}")
                # Даже если запрос завершился ошибкой сети, локально мы фиксируем факт запуска
                confirm_success = True

            if confirm_success:
                with self._lock:
                    self._is_confirmed = True

                if self.on_confirmed:
                    try:
                        self.on_confirmed()
                    except Exception as cb_err:
                        logger.error(f"Ошибка в callback on_confirmed: {cb_err}")
                return True

        return False

    def start(self) -> None:
        """Запускает фоновый поток мониторинга старта лобби."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="LobbyStartDetectorThread",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"LobbyStartDetector запущен (таймаут: {self.timeout_sec}с)")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.is_confirmed or self.is_timed_out:
                break
            self.check_once()
            self._stop_event.wait(self.check_interval)

    def stop(self) -> None:
        """Останавливает мониторинг."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("LobbyStartDetector остановлен")


class HeartbeatWorker:
    """Фоновый воркер продления сессии Хоста (POST /heartbeat).

    Отправляет запросы каждые interval_sec только после подтверждения хостинга.
    """

    def __init__(
        self,
        hub_client: HubClient,
        session_token: str,
        interval_sec: float = 30.0,
        is_confirmed_getter: Callable[[], bool] | None = None,
        udp_port: int | None = None,
        guests_metrics_getter: Callable[[], list[dict]] | None = None,
    ) -> None:
        if not session_token:
            raise ValueError("session_token не может быть пустым")

        self.hub_client = hub_client
        self.session_token = session_token
        self.interval_sec = max(0.01, float(interval_sec))
        self.is_confirmed_getter = is_confirmed_getter
        self.udp_port = udp_port
        self.guests_metrics_getter = guests_metrics_getter

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def send_once(self) -> dict | None:
        """Выполняет разовую отправку Heartbeat на сервер."""
        if self.is_confirmed_getter and not self.is_confirmed_getter():
            logger.debug("Heartbeat пропущен: хостинг еще не подтвержден")
            return None

        guests_metrics = None
        if self.guests_metrics_getter is not None:
            try:
                guests_metrics = self.guests_metrics_getter()
            except Exception as e:
                logger.debug(f"Не удалось получить метрики гостей для heartbeat: {e}")
                guests_metrics = None

        hb_kwargs: dict = {}
        if self.udp_port is not None:
            hb_kwargs["udp_port"] = self.udp_port
        if guests_metrics is not None:
            hb_kwargs["guests_metrics"] = guests_metrics

        try:
            resp = self.hub_client.send_heartbeat(
                self.session_token,
                **hb_kwargs,
            )
            logger.debug(f"Heartbeat успешно отправлен: {resp}")
            return resp
        except Exception as err:
            logger.warning(f"Ошибка отправки Heartbeat: {err}")
            return None

    def start(self) -> None:
        """Запускает фоновый поток Heartbeat."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="HeartbeatWorkerThread",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"HeartbeatWorker запущен (интервал: {self.interval_sec}с)")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self.interval_sec)
            if self._stop_event.is_set():
                break
            self.send_once()

    def stop(self) -> None:
        """Останавливает Heartbeat воркер."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("HeartbeatWorker остановлен")


class AutosaveWatcher:
    """Фоновый демон автосейвов (Rolling Sync).

    Отслеживает появление свежих файлов сохранений и каждые несколько минут
    выгружает самый свежий снимок на VPS без освобождения слота (release_on_success=False).
    """

    def __init__(
        self,
        saves_dir: Path,
        hub_client: HubClient,
        session_token: str,
        session_start_time: float,
        poll_interval_sec: float = 5.0,
        upload_cooldown_sec: float = 120.0,
        is_confirmed_getter: Callable[[], bool] | None = None,
        mod_list: list[dict] | None = None,
        target_save_name: str = "cloud_world.zip",
    ) -> None:
        if not session_token:
            raise ValueError("session_token не может быть пустым")

        self.saves_dir = Path(saves_dir) if not isinstance(saves_dir, Path) else saves_dir
        self.hub_client = hub_client
        self.session_token = session_token
        self.session_start_time = float(session_start_time)
        self.poll_interval_sec = max(0.01, float(poll_interval_sec))
        self.upload_cooldown_sec = max(0.01, float(upload_cooldown_sec))
        self.is_confirmed_getter = is_confirmed_getter
        self.mod_list = mod_list
        self.target_save_name = str(target_save_name or "cloud_world.zip")

        self.last_uploaded_sha256: str | None = None
        self.last_upload_time: float = 0.0

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def check_and_upload_once(self) -> bool:
        """Проверяет появление нового сейва и выгружает его при соблюдении условий."""
        if self.is_confirmed_getter and not self.is_confirmed_getter():
            logger.debug("Rolling Sync пропущен: хостинг еще не подтвержден")
            return False

        now = time.time()
        with self._lock:
            if now - self.last_upload_time < self.upload_cooldown_sec:
                logger.debug(
                    f"Rolling Sync на кулдауне ({now - self.last_upload_time:.1f}с < {self.upload_cooldown_sec}с)"
                )
                return False

        latest_save = find_latest_session_save(
            self.saves_dir,
            self.session_start_time,
            allowed_save_names={self.target_save_name},
        )
        if latest_save is None:
            return False

        try:
            current_sha256 = compute_file_sha256(latest_save)
        except Exception as err:
            logger.warning(f"Не удалось вычислить SHA-256 для {latest_save.name}: {err}")
            return False

        with self._lock:
            if current_sha256 == self.last_uploaded_sha256:
                return False

        # Копируем файл во временный staging-файл, чтобы избежать чтения в момент дозаписи игрой
        staging_dir = self.saves_dir / ".cloud_stash"
        staging_file: Optional[Path] = None
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            staging_file = staging_dir / f"_staging_{int(time.time())}.zip"
            shutil.copy2(latest_save, staging_file)

            if not zipfile.is_zipfile(staging_file):
                logger.warning(f"Staging-файл {staging_file} поврежден или недописан, отмена отправки")
                return False

            staging_sha = compute_file_sha256(staging_file)
            logger.info(f"Запуск фоновой выгрузки (Rolling Sync): {latest_save.name} (SHA: {staging_sha[:8]}...)")

            resp = self.hub_client.upload_save(
                file_path=staging_file,
                session_token=self.session_token,
                release_on_success=False,
                mod_list=self.mod_list,
            )
            logger.info(f"Фоновая выгрузка сейва {latest_save.name} успешно завершена: {resp}")

            with self._lock:
                self.last_uploaded_sha256 = staging_sha
                self.last_upload_time = time.time()

            return True
        except Exception as err:
            logger.error(f"Ошибка фоновой выгрузки автосейва {latest_save.name}: {err}")
            return False
        finally:
            if staging_file and staging_file.exists():
                try:
                    staging_file.unlink(missing_ok=True)
                except OSError:
                    pass

    def start(self) -> None:
        """Запускает фоновый поток мониторинга автосейвов."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="AutosaveWatcherThread",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"AutosaveWatcher запущен (опрос: {self.poll_interval_sec}с, кулдаун: {self.upload_cooldown_sec}с)")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self.poll_interval_sec)
            if self._stop_event.is_set():
                break
            self.check_and_upload_once()

    def stop(self) -> None:
        """Останавливает фоновый поток автосейвов."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("AutosaveWatcher остановлен")


def flush_final_save_on_exit(
    saves_dir: Path,
    hub_client: HubClient,
    session_token: str,
    is_hosting_confirmed: bool,
    session_start_time: float,
    mod_list: list[dict] | None = None,
    target_save_name: str = "cloud_world.zip",
    is_non_cloud_game: bool = False,
    loaded_map_name: str | None = None,
) -> dict:
    """Выполняет финализацию сохранения при закрытии процесса игры.

    1. Если обнаружен запуск локальной/не-серверной карты (is_non_cloud_game=True):
       НЕ перезаписывает cloud_world.zip, отменяет выгрузку и освобождает слот на сервере.
    2. Если хостинг НЕ был подтвержден (игрок закрыл игру из главного меню или не открыл мультиплеер):
       НЕ перезаписывает серверный сейв, а мгновенно освобождает слот через release_host.
    3. Если хостинг был активен для целевого серверного мира:
       Находит самый свежий файл сессии (включая _autosaveN.zip при выходе без ручного сохранения),
       атомарно обновляет целевой сейв (target_save_name) и отправляет его на VPS с release_on_success=True.
    """
    if not isinstance(saves_dir, Path):
        saves_dir = Path(saves_dir)

    if is_non_cloud_game:
        logger.info(
            f"Игра завершена в локальном/не-серверном режиме (карта: {loaded_map_name}). "
            f"Серверный сейв '{target_save_name}' не перезаписывается, выгрузка отменена."
        )
        try:
            hub_client.release_host(session_token, reason=f"local_map_exit:{loaded_map_name or 'unknown'}")
        except Exception as err:
            logger.warning(f"Ошибка при освобождении слота (local_map_exit): {err}")
        return {
            "action": "ignored_local_game",
            "reason": "non_cloud_map",
            "loaded_map": loaded_map_name,
            "released": True,
        }

    if not is_hosting_confirmed:
        logger.info("Игра закрыта без подтверждения хостинга (в меню). Освобождаем слот без выгрузки сейва.")
        try:
            hub_client.release_host(session_token, reason="closed_in_menu")
        except Exception as err:
            logger.warning(f"Ошибка при освобождении слота (closed_in_menu): {err}")
        return {
            "action": "released_without_save",
            "reason": "not_confirmed",
            "released": True,
        }

    # Игрок реально играл в сетевом режиме в целевой мир
    freshest_save = find_latest_session_save(
        saves_dir,
        session_start_time,
        allowed_save_names={target_save_name},
    )
    canonical_save = saves_dir / target_save_name

    target_save_to_upload: Path | None = None

    if freshest_save is not None:
        # Если самым свежим оказался автосейв, копируем его в канонический файл
        if freshest_save.resolve() != canonical_save.resolve():
            logger.info(
                f"Самым свежим файлом сессии является автосейв {freshest_save.name}. "
                f"Атомарно обновляем {canonical_save.name}."
            )
            temp_copy = saves_dir / f"{canonical_save.stem}_{int(time.time())}.tmp"
            try:
                shutil.copy2(freshest_save, temp_copy)
                os.replace(temp_copy, canonical_save)
            except Exception as copy_err:
                logger.error(f"Ошибка атомарного обновления {canonical_save.name}: {copy_err}")
                if temp_copy.exists():
                    try:
                        temp_copy.unlink()
                    except OSError:
                        pass
        target_save_to_upload = canonical_save
    elif canonical_save.exists() and zipfile.is_zipfile(canonical_save):
        logger.info(f"Новых сейвов за сессию не обнаружено, выгружаем существующий {canonical_save.name}")
        target_save_to_upload = canonical_save

    if target_save_to_upload is not None and target_save_to_upload.exists():
        logger.info(f"Финальная выгрузка сейва {target_save_to_upload.name} с флагом release_on_success=True")
        try:
            upload_resp = hub_client.upload_save(
                file_path=target_save_to_upload,
                session_token=session_token,
                release_on_success=True,
                mod_list=mod_list,
            )
            return {
                "action": "uploaded_final",
                "save_path": str(target_save_to_upload),
                "released": True,
                "upload_result": upload_resp,
            }
        except Exception as err:
            logger.error(f"Ошибка финальной выгрузки сейва: {err}")
            # Попытка освободить слот при фатальной ошибке выгрузки
            try:
                hub_client.release_host(session_token, reason="final_upload_failed")
            except Exception:
                pass
            raise

    # Если файлов сохранения вообще нет
    logger.warning("Файлов сохранения для выгрузки не обнаружено. Освобождаем слот.")
    try:
        hub_client.release_host(session_token, reason="no_save_found")
    except Exception as err:
        logger.warning(f"Ошибка при освобождении слота: {err}")

    return {
        "action": "released_without_save",
        "reason": "no_save_found",
        "released": True,
    }


class HostWorkersManager:
    """Комплексный координатор фоновых потоков Хоста.

    Управляет LobbyStartDetector, HeartbeatWorker и AutosaveWatcher.
    Предоставляет методы start(), stop() и on_game_exit().
    """

    def __init__(
        self,
        log_path: Path,
        saves_dir: Path,
        hub_client: HubClient,
        session_token: str,
        session_start_time: float | None = None,
        factorio_port: int = 34197,
        detector_timeout_sec: float = 175.0,
        detector_check_interval: float = 1.0,
        heartbeat_interval_sec: float = 30.0,
        autosave_poll_interval_sec: float = 5.0,
        autosave_cooldown_sec: float = 120.0,
        mod_list: list[dict] | None = None,
        target_save_name: str = "cloud_world.zip",
        on_non_cloud_game: Callable[[str], None] | None = None,
        script_output_dir: Path | str | None = None,
        guests_metrics_getter: Callable[[], list[dict]] | None = None,
    ) -> None:
        self.log_path = Path(log_path)
        self.saves_dir = Path(saves_dir)
        self.script_output_dir = Path(script_output_dir) if script_output_dir is not None else self.saves_dir.parent / "script-output"
        self.hub_client = hub_client
        self.session_token = session_token
        self.session_start_time = float(session_start_time if session_start_time is not None else time.time())
        self.factorio_port = factorio_port
        self.mod_list = mod_list
        self.target_save_name = str(target_save_name or "cloud_world.zip")
        self.user_on_non_cloud_game = on_non_cloud_game
        self.guests_metrics_getter = guests_metrics_getter

        def _handle_non_cloud(map_name: str) -> None:
            logger.info(
                f"HostWorkersManager: обнаружен запуск не-серверной карты '{map_name}'. "
                "Останавливаем фоновые службы."
            )
            try:
                self.heartbeat.stop()
            except Exception as e:
                logger.warning(f"Ошибка остановки Heartbeat: {e}")
            try:
                self.autosave_watcher.stop()
            except Exception as e:
                logger.warning(f"Ошибка остановки AutosaveWatcher: {e}")
            try:
                self.chronicle_watcher.stop()
            except Exception as e:
                logger.warning(f"Ошибка остановки ChronicleWatcher: {e}")
            if self.user_on_non_cloud_game:
                try:
                    self.user_on_non_cloud_game(map_name)
                except Exception as cb_err:
                    logger.error(f"Ошибка в user_on_non_cloud_game: {cb_err}")

        self.detector = LobbyStartDetector(
            log_path=self.log_path,
            hub_client=self.hub_client,
            session_token=self.session_token,
            factorio_port=self.factorio_port,
            saves_dir=self.saves_dir,
            check_interval=detector_check_interval,
            timeout_sec=detector_timeout_sec,
            session_start_time=self.session_start_time,
            target_save_name=self.target_save_name,
            on_non_cloud_game=_handle_non_cloud,
        )

        self.heartbeat = HeartbeatWorker(
            hub_client=self.hub_client,
            session_token=self.session_token,
            interval_sec=heartbeat_interval_sec,
            is_confirmed_getter=lambda: self.detector.is_confirmed and not self.detector.is_non_cloud_game,
            udp_port=self.factorio_port,
            guests_metrics_getter=self.guests_metrics_getter,
        )

        self.autosave_watcher = AutosaveWatcher(
            saves_dir=self.saves_dir,
            hub_client=self.hub_client,
            session_token=self.session_token,
            session_start_time=self.session_start_time,
            poll_interval_sec=autosave_poll_interval_sec,
            upload_cooldown_sec=autosave_cooldown_sec,
            is_confirmed_getter=lambda: self.detector.is_confirmed and not self.detector.is_non_cloud_game,
            mod_list=self.mod_list,
            target_save_name=self.target_save_name,
        )

        self.chronicle_watcher = ChronicleWatcher(
            script_output_dir=self.script_output_dir,
            hub_client=self.hub_client,
            session_token=self.session_token,
            log_path=self.log_path,
            is_confirmed_getter=lambda: self.detector.is_confirmed and not self.detector.is_non_cloud_game,
        )

    def start(self) -> None:
        """Запускает все рабочие потоки."""
        logger.info("Запуск всех фоновых потоков Хоста (HostWorkersManager)...")
        self.detector.start()
        self.heartbeat.start()
        self.autosave_watcher.start()
        self.chronicle_watcher.start()

    def stop(self) -> None:
        """Останавливает все рабочие потоки."""
        logger.info("Остановка всех фоновых потоков Хоста (HostWorkersManager)...")
        self.detector.stop()
        self.heartbeat.stop()
        self.autosave_watcher.stop()
        self.chronicle_watcher.stop()

    def on_game_exit(self) -> dict:
        """Обработчик закрытия игрового процесса Factorio."""
        logger.info("Событие закрытия игры: останавливаем воркеры и финализируем сейв...")
        is_confirmed = self.detector.is_confirmed
        is_non_cloud = self.detector.is_non_cloud_game
        loaded_map = self.detector.loaded_map_name

        if is_confirmed and not is_non_cloud:
            try:
                self.chronicle_watcher.flush_on_exit()
            except Exception as chr_err:
                logger.warning(f"Ошибка отправки финальной хроники при выходе: {chr_err}")

        self.stop()
        return flush_final_save_on_exit(
            saves_dir=self.saves_dir,
            hub_client=self.hub_client,
            session_token=self.session_token,
            is_hosting_confirmed=is_confirmed,
            session_start_time=self.session_start_time,
            mod_list=self.mod_list,
            target_save_name=self.target_save_name,
            is_non_cloud_game=is_non_cloud,
            loaded_map_name=loaded_map,
        )
