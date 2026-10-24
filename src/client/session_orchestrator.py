"""Главный оркестратор сессии и жизненного цикла перехватчика Factorio.

Обеспечивает:
1. Защиту от Split-Brain: принудительное скрытие cloud_world.zip у Гостя в .cloud_stash/.
2. Автоматическое распределение ролей:
   - Если сервер 'idle': захват роли Хоста (claim_host), докачка сейва (sync_save_before_host),
     запуск сетевого моста (HostP2PBridge) и фоновых демонов (HostWorkersManager).
   - Если сервер 'uploading_final': ожидание завершения выгрузки прежним хостом.
   - Если сервер занят: роль Гостя, скрытие локального сейва, копирование 127.0.0.1:34198
     в буфер обмена и запуск прокси (GuestP2PProxy).
3. Фоновый подхват эстафеты (GuestHandoverWatcher): при уходе Хоста во время игры
   автоматически скачивает свежий сейф и уведомляет игрока.
4. Корректный Post-Exit перехват (teardown):
   - Хост: финализация сейва, выгрузка и освобождение слота (on_game_exit).
   - Гость: гарантированный возврат скрытого сейва из .cloud_stash/ в корень saves/.
5. Отказоустойчивость: безопасный запуск в локальном режиме при недоступности сервера.
"""

from __future__ import annotations

import enum
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.host_workers import HostWorkersManager
from src.client.hub_client import HubClient
from src.client.p2p_tunnel import GuestP2PProxy, HostP2PBridge
from src.client.shift_detector import is_shift_key_pressed
from src.client.ui_notifier import copy_to_clipboard, show_role_banner

logger = logging.getLogger("factorio_client.orchestrator")


class RoleType(str, enum.Enum):
    """Тип активной роли участника сессии Factorio."""

    HOST = "HOST"
    GUEST = "GUEST"
    OFFLINE = "OFFLINE"
    NONE = "NONE"


class GuestHandoverWatcher:
    """Фоновый монитор передачи эстафеты для Гостя.

    Пока Гость находится в игре, поток периодически опрашивает сервер-сейф.
    Когда текущий Хост завершает игру (статус переходит в 'idle' после 'hosting_active'
    или 'uploading_final'), монитор автоматически:
    1. Возвращает скрытый сейв из .cloud_stash/.
    2. Скачивает свежий cloud_world.zip с VPS в папку saves/.
    3. Выводит системное уведомление о готовности стать новым Хостом.
    """

    def __init__(
        self,
        hub_client: HubClient,
        env: FactorioEnvironment,
        initial_host: str = "",
        poll_interval_sec: float = 5.0,
        on_handover_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        self.hub_client = hub_client
        self.env = env
        self.initial_host = initial_host
        self.poll_interval_sec = max(0.5, float(poll_interval_sec))
        self.on_handover_ready = on_handover_ready

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self.is_handover_ready = False
        self.has_detected_host_exit = False
        self.downloaded_save_path: Optional[Path] = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Запускает фоновый поток мониторинга эстафеты."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="GuestHandoverWatcherThread",
                daemon=True,
            )
            self._thread.start()
            logger.info("GuestHandoverWatcher запущен (мониторинг эстафеты от Хоста).")

    def stop(self, timeout_sec: float = 2.0) -> None:
        """Останавливает мониторинг эстафеты."""
        with self._lock:
            if self._thread is None:
                return
            self._stop_event.set()

        if self._thread.is_alive():
            self._thread.join(timeout=timeout_sec)
        with self._lock:
            self._thread = None
        logger.info("GuestHandoverWatcher остановлен.")

    def _worker_loop(self) -> None:
        """Основной цикл опроса сервера."""
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self.poll_interval_sec):
                break

            try:
                status = self.hub_client.get_status()
                state = status.get("state") or status.get("session", {}).get("state")

                # Фиксация начала финальной выгрузки сейва прежним хостом
                if state == "uploading_final":
                    if not self.has_detected_host_exit:
                        self.has_detected_host_exit = True
                        logger.info(
                            f"Прежний хост '{self.initial_host}' закрыл игру и выгружает сейф..."
                        )

                # Фиксация освобождения сервера после выгрузки (эстафета готова!)
                elif state == "idle" and (self.has_detected_host_exit or self.initial_host):
                    logger.info(
                        f"Хост '{self.initial_host}' полностью освободил сервер! Начинаем подхват эстафеты..."
                    )
                    # 1. Возвращаем локальный сейв из .cloud_stash/
                    self.env.restore_hidden_cloud_save()

                    # 2. Скачиваем свежий сейф с VPS
                    sync_res = self.hub_client.sync_save_before_host(self.env, status=status)
                    self.downloaded_save_path = self.env.saves_dir / self.env.cloud_save_name
                    self.is_handover_ready = True

                    host_label = self.initial_host or "Хост"
                    show_role_banner(
                        f"Хост {host_label} вышел! Свежий мир скачан — теперь вы можете запустить cloud_world как новый Хост",
                        duration_sec=10.0,
                    )

                    if self.on_handover_ready:
                        try:
                            self.on_handover_ready()
                        except Exception as cb_err:
                            logger.warning(f"Ошибка в on_handover_ready callback: {cb_err}")

                    break

            except Exception as err:
                logger.debug(f"Ошибка опроса статуса в GuestHandoverWatcher: {err}")


class SessionOrchestrator:
    """Главный координатор жизненного цикла запуска Factorio и управления ролями."""

    def __init__(
        self,
        config: Optional[ClientConfig] = None,
        env: Optional[FactorioEnvironment] = None,
        hub_client: Optional[HubClient] = None,
        custom_core_bin: Optional[Path | str] = None,
        config_path: Optional[Path | str] = None,
        setup_gui_provider: Optional[Callable[..., Optional[ClientConfig]]] = None,
        force_setup_gui: bool = False,
        shift_detector: Optional[Callable[[], bool]] = None,
        exec_fn: Optional[Callable[[str, List[str]], None]] = None,
        show_launcher_gui: bool = False,
    ) -> None:
        self.env = env or FactorioEnvironment()
        self.config_path = Path(config_path).resolve() if config_path else self.env.sync_config_path
        self.setup_gui_provider = setup_gui_provider
        self.force_setup_gui = bool(force_setup_gui)
        self.show_launcher_gui = bool(show_launcher_gui)
        self.shift_detector = shift_detector
        self._exec_fn = exec_fn or os.execv

        if config is not None:
            self.config = config
        else:
            loaded = ClientConfig.load_from_file(self.config_path)
            if loaded is not None:
                self.config = loaded
            else:
                self.config = ClientConfig.from_env()

        self.custom_core_bin = Path(custom_core_bin).resolve() if custom_core_bin else None

        if hub_client is not None:
            self.hub_client = hub_client
            self._owns_hub_client = False
        else:
            self.hub_client = HubClient(config=self.config)
            self._owns_hub_client = True

        self.role: RoleType = RoleType.NONE
        self.session_token: Optional[str] = None
        self.player_name: str = ""
        self.is_prepared: bool = False

        self.host_bridge: Optional[HostP2PBridge] = None
        self.host_workers: Optional[HostWorkersManager] = None
        self.guest_proxy: Optional[GuestP2PProxy] = None
        self.handover_watcher: Optional[GuestHandoverWatcher] = None
        self.game_process: Optional[subprocess.Popen] = None

        self._lock = threading.Lock()

    def __enter__(self) -> SessionOrchestrator:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.teardown()

    def locate_core_executable(self, custom_path: Optional[Path | str] = None) -> Path:
        """Определяет путь к оригинальному исполняемому файлу factorio_core.

        Проверяет:
        1. Явно переданный путь custom_path.
        2. self.custom_core_bin (из конструктора).
        3. Переменную окружения FACTORIO_CORE_BIN.
        4. Стандартные расположения относительно корня игры (bin/x64, MacOS, root).
        5. Fallback на factorio (без _core) для тестовых окружений.
        """
        candidates: List[Path] = []

        if custom_path:
            candidates.append(Path(custom_path).resolve())

        if self.custom_core_bin:
            candidates.append(self.custom_core_bin)

        env_bin = os.getenv("FACTORIO_CORE_BIN")
        if env_bin:
            candidates.append(Path(env_bin).resolve())

        current_os = platform.system()
        game_root = self.env.game_root
        if game_root is not None:
            if current_os == "Windows":
                candidates.extend([
                    game_root / "bin" / "x64" / "factorio_core.exe",
                    game_root / "factorio_core.exe",
                    game_root / "bin" / "x64" / "factorio.exe",
                    game_root / "factorio.exe",
                ])
            elif current_os == "Darwin":
                candidates.extend([
                    game_root / "Contents" / "MacOS" / "factorio_core",
                    game_root / "Factorio.app" / "Contents" / "MacOS" / "factorio_core",
                    game_root / "factorio_core",
                    game_root / "Contents" / "MacOS" / "factorio",
                    game_root / "Factorio.app" / "Contents" / "MacOS" / "factorio",
                    game_root / "factorio",
                ])
            else:  # Linux
                candidates.extend([
                    game_root / "bin" / "x64" / "factorio_core",
                    game_root / "factorio_core",
                    game_root / "bin" / "x64" / "factorio",
                    game_root / "factorio",
                ])

        # Стандартные системные пути установки игры (если game_root не задан или файл не найден в game_root)
        if current_os == "Windows":
            for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
                val = os.getenv(env_var)
                if val:
                    base = Path(val)
                    candidates.extend([
                        base / "Factorio" / "bin" / "x64" / "factorio_core.exe",
                        base / "Steam" / "steamapps" / "common" / "Factorio" / "bin" / "x64" / "factorio_core.exe",
                        base / "Programs" / "Factorio" / "bin" / "x64" / "factorio_core.exe",
                        base / "Factorio" / "bin" / "x64" / "factorio.exe",
                        base / "Steam" / "steamapps" / "common" / "Factorio" / "bin" / "x64" / "factorio.exe",
                        base / "Programs" / "Factorio" / "bin" / "x64" / "factorio.exe",
                    ])
            for drive in ("C:", "D:", "E:"):
                candidates.extend([
                    Path(f"{drive}/Games/Factorio/bin/x64/factorio_core.exe"),
                    Path(f"{drive}/Factorio/bin/x64/factorio_core.exe"),
                    Path(f"{drive}/Games/Factorio/bin/x64/factorio.exe"),
                    Path(f"{drive}/Factorio/bin/x64/factorio.exe"),
                ])
        elif current_os == "Darwin":
            candidates.extend([
                Path("/Applications/Factorio.app/Contents/MacOS/factorio_core"),
                Path.home() / "Applications" / "Factorio.app" / "Contents" / "MacOS" / "factorio_core",
                Path.home() / "Library/Application Support/Steam/steamapps/common/Factorio/Factorio.app/Contents/MacOS/factorio_core",
                Path("/Applications/Factorio/factorio_core"),
                Path("/Applications/Factorio.app/Contents/MacOS/factorio"),
                Path.home() / "Applications" / "Factorio.app" / "Contents" / "MacOS" / "factorio",
                Path.home() / "Library/Application Support/Steam/steamapps/common/Factorio/Factorio.app/Contents/MacOS/factorio",
            ])
        else:  # Linux
            candidates.extend([
                Path.home() / ".factorio" / "bin" / "x64" / "factorio_core",
                Path("/opt/factorio/bin/x64/factorio_core"),
                Path.home() / ".local/share/Steam/steamapps/common/Factorio/bin/x64/factorio_core",
                Path.home() / ".steam/steam/steamapps/common/Factorio/bin/x64/factorio_core",
                Path.home() / ".factorio" / "bin" / "x64" / "factorio",
                Path("/opt/factorio/bin/x64/factorio"),
                Path.home() / ".local/share/Steam/steamapps/common/Factorio/bin/x64/factorio",
                Path.home() / ".steam/steam/steamapps/common/Factorio/bin/x64/factorio",
            ])

        # Проверка кандидатов по файловой системе
        for cand in candidates:
            if cand.is_file():
                logger.info(f"Найден исполняемый файл Factorio: {cand}")
                return cand

        # Поиск через системный PATH
        for bin_name in ("factorio_core", "factorio"):
            which_res = shutil.which(bin_name)
            if which_res:
                p = Path(which_res).resolve()
                logger.info(f"Найден исполняемый файл в PATH: {p}")
                return p

        raise FileNotFoundError(
            f"Не удалось найти исполняемый файл игры (factorio_core или factorio). "
            f"Проверенные пути: {[str(c) for c in candidates]}"
        )

    def prepare_session(self) -> Dict[str, Any]:
        """Выполняет предстартовую подготовку, выбор роли и запуск сетевых служб.

        Алгоритм:
        1. Восстанавливает спрятанный сейв из прошлых сбоев (.cloud_stash/ -> saves/).
        2. Определяет никнейм игрока.
        3. Запрашивает статус сервера:
           - При ошибке сети: переход в режим OFFLINE.
           - При 'uploading_final': ожидает завершения выгрузки.
           - При 'idle': захватывает роль Хоста, докачивает сейв, запускает HostP2PBridge
             и HostWorkersManager.
           - При занятом сервере (или отказе): скрывает сейв cloud_world.zip у Гостя,
             копирует 127.0.0.1:34198 в буфер обмена и запускает GuestP2PProxy + GuestHandoverWatcher.
        """
        with self._lock:
            # Шаг 0: Первичная настройка через GUI или вызов по Shift / force_setup_gui / show_launcher_gui
            is_shift = (
                self.shift_detector()
                if self.shift_detector is not None
                else is_shift_key_pressed()
            )
            should_run_setup = (
                (not self.config.is_configured())
                or self.force_setup_gui
                or is_shift
                or self.show_launcher_gui
            )

            if should_run_setup:
                is_auto_connect = bool(self.config.is_configured() and not is_shift and not self.force_setup_gui)
                reason_log = (
                    "режим автоподключения (Launcher)"
                    if is_auto_connect
                    else (
                        "отсутствует device_token"
                        if not self.config.is_configured()
                        else ("флаг force_setup_gui" if self.force_setup_gui else "зажата клавиша Shift")
                    )
                )
                logger.info(f"Запуск окна настроек GUI ({reason_log})...")
                gui_runner = self.setup_gui_provider
                if gui_runner is None:
                    try:
                        from src.client.setup_gui import run_first_time_setup_gui
                        gui_runner = run_first_time_setup_gui
                    except ImportError:
                        gui_runner = None

                if gui_runner is not None:
                    try:
                        gui_kwargs: dict[str, Any] = {
                            "env": self.env,
                            "config_path": self.config_path,
                            "installer_mode": False,
                            "http_client": None,
                            "existing_config": self.config,
                            "auto_connect": is_auto_connect,
                        }
                        try:
                            import inspect
                            sig = inspect.signature(gui_runner)
                            if "use_subprocess" in sig.parameters or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                                gui_kwargs["use_subprocess"] = True
                        except Exception:
                            pass
                        new_config = gui_runner(**gui_kwargs)
                        if new_config and getattr(new_config, "was_uninstalled", False):
                            logger.info("Перехватчик был деинсталлирован пользователем через GUI. Завершение работы.")
                            self.role = RoleType.OFFLINE
                            self.is_prepared = True
                            return {
                                "role": self.role,
                                "reason": "uninstalled_by_user",
                            }

                        if new_config and getattr(new_config, "offline_mode", False):
                            logger.info("Пользователь запросил автономный запуск игры в GUI. Переход в OFFLINE.")
                            self.role = RoleType.OFFLINE
                            self.is_prepared = True
                            return {
                                "role": self.role,
                                "reason": "offline_requested_by_user",
                            }

                        if new_config and new_config.is_configured():
                            self.config = new_config
                            if self._owns_hub_client:
                                try:
                                    self.hub_client.close()
                                except Exception:
                                    pass
                                self.hub_client = HubClient(config=self.config)
                            logger.info("Настройка через GUI успешно завершена.")
                        else:
                            if not self.config.is_configured():
                                logger.warning(
                                    "Первичная настройка через GUI отменена пользователем. "
                                    "Переход в автономный режим (OFFLINE)."
                                )
                                self.role = RoleType.OFFLINE
                                self.is_prepared = True
                                return {
                                    "role": self.role,
                                    "reason": "setup_cancelled_by_user",
                                }
                            else:
                                logger.info(
                                    "Окно настроек закрыто, продолжаем запуск с текущей конфигурацией."
                                )
                    except Exception as gui_err:
                        logger.warning(f"Ошибка при вызове GUI настройки: {gui_err}")
                        if not self.config.is_configured():
                            self.role = RoleType.OFFLINE
                            self.is_prepared = True
                            return {
                                "role": self.role,
                                "reason": "setup_gui_failed",
                                "error": str(gui_err),
                            }

            # Шаг 1: Обеспечение директорий и восстановление от возможных прошлых сбоев
            self.env.ensure_dirs()
            restored = self.env.restore_hidden_cloud_save()
            if restored:
                logger.info(f"Восстановлено сохранение после прошлого сеанса Гостя: {restored}")

            # Шаг 2: Никнейм игрока (главный источник истины — player-data.json)
            fallback = self.config.player_name or os.getenv("FACTORIO_PLAYER_NAME")
            self.player_name = self.env.resolve_or_init_player_username(fallback_name=fallback)
            logger.info(f"Инициализация сессии для игрока: '{self.player_name}'")

            # Шаг 3: Проверка статуса сервера (щедрый таймаут 20с для стабильной работы по Wi-Fi)
            try:
                status = self.hub_client.get_status(timeout=20.0)
            except Exception as err:
                # Проверка на 403 Forbidden (токен устройства отозван администратором)
                is_forbidden = False
                if hasattr(err, "response") and getattr(err.response, "status_code", None) == 403:
                    is_forbidden = True
                elif "403" in str(err) or "Forbidden" in str(err):
                    is_forbidden = True

                if is_forbidden:
                    logger.warning(
                        "Токен устройства отозван или недействителен (403 Forbidden). "
                        "Сброс токена и повторная авторизация..."
                    )
                    self.config.device_token = ""
                    try:
                        self.config.save_to_file(self.config_path)
                    except Exception as save_err:
                        logger.warning(f"Не удалось обновить файл конфигурации при сбросе токена: {save_err}")

                    gui_runner = self.setup_gui_provider
                    if gui_runner is None:
                        try:
                            from src.client.setup_gui import run_first_time_setup_gui
                            gui_runner = run_first_time_setup_gui
                        except ImportError:
                            gui_runner = None

                    if gui_runner is not None:
                        try:
                            gui_kwargs = {
                                "env": self.env,
                                "config_path": self.config_path,
                                "installer_mode": False,
                                "http_client": None,
                                "prompt_message": "Доступ устройства был обновлен администратором. Пожалуйста, введите новый Код Дружбы.",
                            }
                            try:
                                import inspect
                                sig = inspect.signature(gui_runner)
                                if "use_subprocess" in sig.parameters or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                                    gui_kwargs["use_subprocess"] = True
                            except Exception:
                                pass
                            new_config = gui_runner(**gui_kwargs)
                            if new_config and getattr(new_config, "was_uninstalled", False):
                                logger.info("Перехватчик был деинсталлирован пользователем через GUI. Завершение работы.")
                                self.role = RoleType.OFFLINE
                                self.is_prepared = True
                                return {
                                    "role": self.role,
                                    "reason": "uninstalled_by_user",
                                }

                            if new_config and getattr(new_config, "offline_mode", False):
                                logger.info("Пользователь запросил автономный запуск игры в GUI. Переход в OFFLINE.")
                                self.role = RoleType.OFFLINE
                                self.is_prepared = True
                                return {
                                    "role": self.role,
                                    "reason": "offline_requested_by_user",
                                }

                            if new_config and new_config.is_configured():
                                self.config = new_config
                                if self._owns_hub_client:
                                    try:
                                        self.hub_client.close()
                                    except Exception:
                                        pass
                                    self.hub_client = HubClient(config=self.config)
                                status = self.hub_client.get_status()
                            else:
                                self.role = RoleType.OFFLINE
                                self.is_prepared = True
                                return {"role": self.role, "reason": "reauth_cancelled"}
                        except Exception as reauth_err:
                            logger.warning(f"Ошибка повторной авторизации через GUI: {reauth_err}")
                            self.role = RoleType.OFFLINE
                            self.is_prepared = True
                            return {"role": self.role, "reason": "reauth_failed", "error": str(reauth_err)}
                    else:
                        self.role = RoleType.OFFLINE
                        self.is_prepared = True
                        return {"role": self.role, "reason": "forbidden_no_gui", "error": str(err)}
                else:
                    logger.warning(
                        f"Сервер-сейф недоступен ({err}). Переход в автономный режим (OFFLINE)."
                    )
                    show_role_banner(
                        "Сервер-сейф недоступен. Игра запущена в автономном режиме.",
                        duration_sec=7.0,
                    )
                    self.role = RoleType.OFFLINE
                    self.is_prepared = True
                    return {
                        "role": self.role,
                        "reason": "server_unreachable",
                        "error": str(err),
                    }

            state = status.get("state") or status.get("session", {}).get("state", "idle")

            # Шаг 4: Если сервер в процессе финальной выгрузки другим игроком
            if state == "uploading_final":
                logger.info("Предыдущий хост выгружает сейф. Запуск ожидания передачи эстафеты...")
                show_role_banner(
                    "Предыдущий Хост сохраняет мир в облако, подождите пару секунд...",
                    duration_sec=5.0,
                )
                status = self.hub_client.wait_if_uploading_final(
                    max_wait_sec=self.config.request_timeout_seconds
                )
                state = status.get("state") or status.get("session", {}).get("state", "idle")

            # Шаг 5: Попытка захвата роли Хоста, если сервер свободен
            if state == "idle":
                claim_resp: Dict[str, Any] = {}
                try:
                    claim_resp = self.hub_client.claim_host(
                        player_name=self.player_name,
                        wait_for_upload=True,
                    )
                except Exception as claim_err:
                    logger.warning(f"Ошибка при запросе брони роли Хоста: {claim_err}")
                    claim_resp = {"claimed": False, "reason": "claim_failed"}

                if claim_resp.get("claimed"):
                    self.role = RoleType.HOST
                    self.session_token = str(claim_resp.get("session_token", ""))
                    logger.info(
                        f"Успешно получена роль ХОСТА! session_token={self.session_token[:8]}..."
                    )

                    # Синхронизация сохранения перед стартом
                    sync_res = self.hub_client.sync_save_before_host(self.env, status=status)
                    if sync_res.get("mods_mismatch"):
                        logger.warning(
                            f"ВНИМАНИЕ: На сервере активны моды, отсутствующие локально: {sync_res['mods_mismatch']}"
                        )

                    # Очистка устаревших автосейвов перед стартом Хоста
                    self.env.clean_autosaves_for_host()

                    # Настройка параметров мультиплеера для Хоста
                    self.env.prepare_multiplayer_settings(is_host=True)

                    # Баннер Хоста
                    show_role_banner(
                        "Вы — Хост! Зайдите в: Сетевая игра -> Запустить сохранённую игру -> cloud_world",
                        duration_sec=10.0,
                    )

                    # Запуск HostP2PBridge
                    self.host_bridge = HostP2PBridge(
                        session_token=self.session_token,
                        stun_host=self.config.hub_stun_host,
                        stun_port=self.config.hub_stun_port,
                        factorio_port=self.config.host_factorio_port,
                        device_token=self.config.device_token,
                    )
                    self.host_bridge.start()

                    # Запуск HostWorkersManager (LobbyDetector, Heartbeat, AutosaveWatcher)
                    self.host_workers = HostWorkersManager(
                        log_path=self.env.log_file_path,
                        saves_dir=self.env.saves_dir,
                        hub_client=self.hub_client,
                        session_token=self.session_token,
                        factorio_port=self.config.host_factorio_port,
                        mod_list=self.env.get_active_mods_manifest(),
                        target_save_name=self.env.cloud_save_name,
                    )
                    self.host_workers.start()

                    self.is_prepared = True
                    return {
                        "role": self.role,
                        "session_token": self.session_token,
                        "sync_result": sync_res,
                    }

            # Шаг 6: Ветка роли Гостя (сервер занят либо отказ в claim_host)
            self.role = RoleType.GUEST
            current_host = (
                status.get("current_host")
                or status.get("session", {}).get("host_name")
                or "Друг"
            )
            logger.info(f"Сервер занят хостом '{current_host}'. Переход в роль ГОСТЯ.")

            # КРИТИЧЕСКИЙ БАРЬЕР: Скрытие сейва у Гостя для предотвращения Split-Brain
            hidden_save = self.env.hide_cloud_save_for_guest()
            if hidden_save:
                logger.info(
                    f"Файл {self.env.cloud_save_name} перемещен в {hidden_save.parent.name} "
                    f"для защиты от случайного запуска Гостем."
                )

            proxy_address = f"127.0.0.1:{self.config.guest_proxy_port}"
            self.env.prepare_multiplayer_settings(is_host=False, proxy_address=proxy_address)
            copy_to_clipboard(proxy_address)

            show_role_banner(
                f"Сейчас хостит {current_host}! Зайдите в: Сетевая игра -> Прямое подключение -> Подключиться",
                duration_sec=10.0,
            )

            # Запуск локального UDP-прокси Гостя (127.0.0.1:34198 -> STUN P2P)
            self.guest_proxy = GuestP2PProxy(
                guest_name=self.player_name,
                stun_host=self.config.hub_stun_host,
                stun_port=self.config.hub_stun_port,
                proxy_port=self.config.guest_proxy_port,
                device_token=self.config.device_token,
            )
            self.guest_proxy.start()

            # Запуск фонового мониторинга эстафеты на случай ухода Хоста
            self.handover_watcher = GuestHandoverWatcher(
                hub_client=self.hub_client,
                env=self.env,
                initial_host=current_host,
            )
            self.handover_watcher.start()

            self.is_prepared = True
            return {
                "role": self.role,
                "host_name": current_host,
                "proxy_address": proxy_address,
                "save_hidden": hidden_save is not None,
            }

    def launch_game(
        self,
        executable_path: Optional[Path | str] = None,
        game_args: Optional[List[str]] = None,
    ) -> subprocess.Popen:
        """Запускает процесс игры Factorio с переданными аргументами."""
        core_bin = self.locate_core_executable(executable_path)
        cmd = [str(core_bin), *(game_args or [])]
        logger.info(f"Запуск процесса Factorio: {' '.join(cmd)}")

        proc = subprocess.Popen(cmd, cwd=str(core_bin.parent))
        self.game_process = proc
        return proc

    def wait_game(
        self,
        process: Optional[subprocess.Popen] = None,
        poll_interval: float = 0.5,
    ) -> int:
        """Ожидает завершения игрового процесса Factorio."""
        p = process or self.game_process
        if p is None:
            return 0

        logger.info(f"Ожидание завершения процесса Factorio (PID: {p.pid})...")
        while p.poll() is None:
            time.sleep(poll_interval)

        logger.info(f"Процесс Factorio завершился с кодом: {p.returncode}")
        return p.returncode

    def teardown(self) -> Dict[str, Any]:
        """Выполняет завершение сессии, очистку фоновых потоков и защиту сейвов."""
        with self._lock:
            report: Dict[str, Any] = {"role": self.role}

            # 1. Завершение для Хоста
            if self.role == RoleType.HOST:
                logger.info("Выход Хоста: финализация сейва и остановка служб...")
                if self.host_workers is not None:
                    try:
                        exit_report = self.host_workers.on_game_exit()
                        report["host_exit_report"] = exit_report
                        logger.info(f"Результат on_game_exit: {exit_report}")
                    except Exception as err:
                        logger.error(f"Ошибка при on_game_exit: {err}")
                        report["exit_error"] = str(err)
                    self.host_workers = None

                if self.host_bridge is not None:
                    try:
                        self.host_bridge.stop()
                    except Exception as err:
                        logger.warning(f"Ошибка при остановке HostP2PBridge: {err}")
                    self.host_bridge = None

            # 2. Завершение для Гостя
            elif self.role == RoleType.GUEST:
                logger.info("Выход Гостя: остановка прокси и восстановление сейва...")
                if self.handover_watcher is not None:
                    try:
                        self.handover_watcher.stop()
                    except Exception as err:
                        logger.warning(f"Ошибка при остановке GuestHandoverWatcher: {err}")
                    self.handover_watcher = None

                if self.guest_proxy is not None:
                    try:
                        self.guest_proxy.stop()
                    except Exception as err:
                        logger.warning(f"Ошибка при остановке GuestP2PProxy: {err}")
                    self.guest_proxy = None

                # КРИТИЧЕСКИЙ ШАГ: гарантированное восстановление сейва из .cloud_stash/
                try:
                    restored = self.env.restore_hidden_cloud_save()
                    report["restored_save"] = str(restored) if restored else None
                    logger.info(f"Скрытый сейв восстановлен в saves/: {restored}")
                except Exception as err:
                    logger.error(f"Ошибка при восстановлении скрытого сейва: {err}")

            # 3. Завершение для автономного режима
            elif self.role == RoleType.OFFLINE:
                try:
                    restored = self.env.restore_hidden_cloud_save()
                    report["restored_save"] = str(restored) if restored else None
                except Exception:
                    pass

            # Закрытие соединения с сервером
            if self._owns_hub_client and self.hub_client is not None:
                try:
                    self.hub_client.close()
                except Exception:
                    pass

            self.is_prepared = False
            return report

    def _run_offline(
        self,
        executable_path: Optional[Path | str] = None,
        game_args: Optional[List[str]] = None,
    ) -> int:
        """Запускает игру в автономном режиме (OFFLINE) без удержания перехватчика в памяти.

        На POSIX-системах (macOS, Linux) замещает процесс перехватчика оригинальным
        бинарником factorio_core через os.execv, мгновенно освобождая оперативную память
        и исключая зависание контекста Cocoa / статус 'Not Responding' в Activity Monitor.
        На Windows запускает процесс игры через subprocess.Popen и немедленно завершает перехватчик.
        """
        self.teardown()
        core_bin = self.locate_core_executable(executable_path)
        cmd = [str(core_bin), *(game_args or [])]

        is_posix = platform.system() != "Windows" and os.name != "nt"
        if is_posix:
            logger.info(
                f"Автономный запуск (OFFLINE): бесшовное замещение процесса через os.execv: {' '.join(cmd)}"
            )
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass

            if "PYTEST_CURRENT_TEST" in os.environ and self._exec_fn is os.execv:
                logger.info(
                    "Обнаружен запуск под pytest: пропуск реального вызова os.execv для защиты тестового процесса."
                )
                return 0

            try:
                self._exec_fn(str(core_bin), cmd)
                return 0
            except Exception as exec_err:
                logger.error(
                    f"Не удалось выполнить os.execv ({exec_err}). Fallback на стандартный запуск."
                )
                proc = self.launch_game(executable_path=executable_path, game_args=game_args)
                return self.wait_game(proc)
        else:
            logger.info(
                f"Автономный запуск (OFFLINE) на Windows: запуск {core_bin.name} и немедленный выход: {' '.join(cmd)}"
            )
            try:
                subprocess.Popen(cmd, cwd=str(core_bin.parent))
                return 0
            except Exception as popen_err:
                logger.error(f"Ошибка запуска процесса на Windows: {popen_err}")
                return 1

    def run(
        self,
        game_args: Optional[List[str]] = None,
        executable_path: Optional[Path | str] = None,
    ) -> int:
        """Запускает полный цикл работы перехватчика: подготовка -> запуск -> teardown."""
        try:
            prep_report = self.prepare_session()
            if prep_report.get("reason") == "uninstalled_by_user":
                logger.info("Перехватчик был деинсталлирован пользователем. Завершение работы без запуска игры.")
                return 0

            if self.role == RoleType.OFFLINE:
                return self._run_offline(
                    executable_path=executable_path,
                    game_args=game_args,
                )

            proc = self.launch_game(
                executable_path=executable_path,
                game_args=game_args,
            )
            return self.wait_game(proc)
        finally:
            self.teardown()
