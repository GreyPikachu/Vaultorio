"""Модуль управления файловым окружением Factorio и защиты от Split-Brain.

Обеспечивает:
1. Автоопределение путей Factorio (Windows, macOS, Linux, Portable).
2. Защиту от Split-Brain («Физическое скрытие сейва у Гостя»).
3. Настройку player-data.json (ник, отключение verify-user-identity, история адресов).
4. Настройку config.ini (интервал автосейвов 2 мин, non-blocking-saving).
5. Чтение манифеста активных модов (mod-list.json).
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import platform
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("factorio_client.env")


class FactorioEnvironment:
    """Управление путями, конфигурацией и защитой файлов сохранения Factorio."""

    def __init__(
        self,
        custom_game_root: Path | str | None = None,
        custom_user_data_dir: Path | str | None = None,
        cloud_save_name: str = "cloud_world.zip",
    ) -> None:
        """Инициализирует окружение Factorio.

        Args:
            custom_game_root: Явный путь к корню игры (если известен).
            custom_user_data_dir: Явный путь к пользовательским данным (saves, mods, config).
            cloud_save_name: Имя основного облачного файла сохранения.
        """
        self.cloud_save_name = cloud_save_name
        self._init_paths(custom_game_root, custom_user_data_dir)

    def _init_paths(
        self,
        custom_game_root: Path | str | None,
        custom_user_data_dir: Path | str | None,
    ) -> None:
        """Определяет пути к файлам и каталогам игры."""
        if custom_user_data_dir is not None:
            self.user_data_dir = Path(custom_user_data_dir).resolve()
            self.game_root = Path(custom_game_root).resolve() if custom_game_root else None
        else:
            paths = self.detect_paths(executable_dir=custom_game_root)
            self.game_root = paths.get("game_root")
            self.user_data_dir = paths["user_data_dir"]

        self.saves_dir = self.user_data_dir / "saves"
        self.cloud_stash_dir = self.saves_dir / ".cloud_stash"
        self.mods_dir = self.user_data_dir / "mods"
        self.config_dir = self.user_data_dir / "config"
        self.config_ini_path = self.config_dir / "config.ini"
        self.player_data_path = self.user_data_dir / "player-data.json"
        self.log_file_path = self.user_data_dir / "factorio-current.log"

    @property
    def sync_config_path(self) -> Path:
        """Возвращает актуальный путь к файлу конфигурации factorio_sync_config.json.

        Приоритет:
        1. Если файл уже существует в game_root — использовать его.
        2. Иначе стандартный путь в user_data_dir.
        """
        if self.game_root and (self.game_root / "factorio_sync_config.json").is_file():
            return self.game_root / "factorio_sync_config.json"
        return self.user_data_dir / "factorio_sync_config.json"

    def ensure_dirs(self) -> None:
        """Создает необходимые служебные директории, если они не существуют."""
        try:
            self.saves_dir.mkdir(parents=True, exist_ok=True)
            self.cloud_stash_dir.mkdir(parents=True, exist_ok=True)
            self.mods_dir.mkdir(parents=True, exist_ok=True)
            self.config_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Директории проверены/созданы в {self.user_data_dir}")
        except Exception as e:
            logger.error(f"Ошибка при создании директорий Factorio: {e}")
            raise

    @staticmethod
    def detect_paths(
        executable_dir: Path | str | None = None,
        system_override: str | None = None,
    ) -> dict[str, Path | None]:
        """Определяет расположение директорий игры и пользовательских данных.

        Поддерживает:
        - Windows (%APPDATA%\\Factorio или portable).
        - macOS (~/Library/Application Support/factorio или portable).
        - Linux (~/.factorio или portable).

        Args:
            executable_dir: Директория с исполняемым файлом (или корень игры).
            system_override: Принудительное указание ОС для тестирования ('Windows', 'Darwin', 'Linux').

        Returns:
            Словарь с путями 'game_root' и 'user_data_dir'.
        """
        current_os = system_override or platform.system()
        game_root: Path | None = None
        user_data_dir: Path | None = None

        if executable_dir is not None:
            exec_path = Path(executable_dir).resolve()
            # Поиск корня игры из bin/x64 или Factorio.app
            if exec_path.name.lower() == "x64" and exec_path.parent.name.lower() == "bin":
                game_root = exec_path.parent.parent
            elif exec_path.name == "MacOS" and "Factorio.app" in str(exec_path):
                # .../Factorio.app/Contents/MacOS -> корень над приложением
                parts = list(exec_path.parts)
                while parts and not parts[-1].endswith(".app"):
                    parts.pop()
                if parts:
                    parts.pop()
                    game_root = Path(*parts)
            else:
                game_root = exec_path

            # Проверка portable-установки через config-path.cfg
            if game_root is not None:
                config_path_cfg = game_root / "config-path.cfg"
                if config_path_cfg.is_file():
                    try:
                        content = config_path_cfg.read_text(encoding="utf-8", errors="ignore")
                        for line in content.splitlines():
                            line_stripped = line.strip()
                            if line_stripped.startswith("use-system-read-write-data-directories"):
                                val = line_stripped.split("=")[-1].strip().lower()
                                if val == "false":
                                    # Portable-режим: данные хранятся прямо в корне игры
                                    user_data_dir = game_root
                                    logger.info(f"Обнаружен portable-режим Factorio: {game_root}")
                                    break
                    except Exception as e:
                        logger.warning(f"Не удалось прочитать {config_path_cfg}: {e}")

        # Если не portable, вычисляем стандартный системный путь
        if user_data_dir is None:
            if current_os == "Windows":
                appdata = os.getenv("APPDATA")
                if appdata:
                    user_data_dir = Path(appdata) / "Factorio"
                else:
                    user_data_dir = Path.home() / "AppData" / "Roaming" / "Factorio"
            elif current_os == "Darwin":
                user_data_dir = Path.home() / "Library" / "Application Support" / "factorio"
            else:  # Linux и прочие
                user_data_dir = Path.home() / ".factorio"

        return {
            "game_root": game_root,
            "user_data_dir": user_data_dir.resolve(),
        }

    # =========================================================================
    # Раздел 2: Split-Brain Shield (Скрытие сейва у Гостя)
    # =========================================================================

    def hide_cloud_save_for_guest(self, save_name: str | None = None) -> Path | None:
        """Перемещает файл сохранения в скрытую подпапку .cloud_stash/.

        Движок Factorio отображает в меню «Загрузить игру» только .zip архивы
        из корня папки saves/. Перемещение файла в подпапку физически скрывает его
        из игрового интерфейса Гостя, предотвращая случайный запуск параллельного
        хоста (Split-Brain Prevention).

        Args:
            save_name: Имя файла сейва (по умолчанию self.cloud_save_name).

        Returns:
            Путь к скрытому файлу в .cloud_stash/ или None, если файл не найден.
        """
        target_name = save_name or self.cloud_save_name
        src_file = self.saves_dir / target_name
        stash_file = self.cloud_stash_dir / target_name

        try:
            self.cloud_stash_dir.mkdir(parents=True, exist_ok=True)

            if src_file.is_file():
                # Атомарное перемещение внутри одной файловой системы
                os.replace(src_file, stash_file)
                logger.info(
                    f"Файл {target_name} временно скрыт в .cloud_stash/ для защиты от "
                    f"случайного запуска Гостем (Split-Brain protection)."
                )
                return stash_file

            if stash_file.is_file():
                logger.debug(f"Файл {target_name} уже скрыт в .cloud_stash/.")
                return stash_file

            logger.debug(f"Файл {target_name} отсутствует в saves/ и в .cloud_stash/.")
            return None
        except Exception as e:
            logger.error(f"Ошибка при скрытии сейва {target_name} для Гостя: {e}")
            raise

    def restore_hidden_cloud_save(self, save_name: str | None = None) -> Path | None:
        """Возвращает скрытый файл сохранения из .cloud_stash/ обратно в корень saves/.

        Вызывается при завершении сеанса Гостя, перехвате роли Хоста,
        а также при первичном запуске перехватчика (защита от сбоя прошлого сеанса).

        Args:
            save_name: Имя файла сейва (по умолчанию self.cloud_save_name).

        Returns:
            Путь к восстановленному файлу в saves/ или None, если скрытого сейва не было.
        """
        target_name = save_name or self.cloud_save_name
        src_file = self.saves_dir / target_name
        stash_file = self.cloud_stash_dir / target_name

        try:
            self.saves_dir.mkdir(parents=True, exist_ok=True)

            if stash_file.is_file():
                if src_file.is_file():
                    # Если оба файла существуют, проверяем время изменения
                    stash_mtime = stash_file.stat().st_mtime
                    src_mtime = src_file.stat().st_mtime
                    if stash_mtime >= src_mtime:
                        os.replace(stash_file, src_file)
                        logger.info(
                            f"Скрытый сейв {target_name} возвращён в корень saves/ (новее текущего)."
                        )
                    else:
                        logger.info(
                            f"Файл {target_name} в saves/ новее спрятанного. Удаляем устаревший .cloud_stash/."
                        )
                        stash_file.unlink(missing_ok=True)
                else:
                    os.replace(stash_file, src_file)
                    logger.info(f"Скрытый сейв {target_name} восстановлен в корень saves/.")

                return src_file

            if src_file.is_file():
                return src_file

            return None
        except Exception as e:
            logger.error(f"Ошибка при восстановлении скрытого сейва {target_name}: {e}")
            raise

    # =========================================================================
    # Раздел 3: Идентификация игрока (player-data.json)
    # =========================================================================

    def _read_player_data(self) -> dict[str, Any]:
        """Безопасно считывает player-data.json."""
        if not self.player_data_path.is_file():
            return {}
        try:
            content = self.player_data_path.read_text(encoding="utf-8")
            return json.loads(content) if content.strip() else {}
        except Exception as e:
            logger.warning(f"Не удалось прочитать {self.player_data_path}: {e}")
            return {}

    def _write_player_data(self, data: dict[str, Any]) -> None:
        """Атомарно записывает player-data.json с созданием резервной копии."""
        try:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            temp_file = self.player_data_path.with_suffix(".tmp")
            content = json.dumps(data, indent=2, ensure_ascii=False)
            temp_file.write_text(content, encoding="utf-8")
            os.replace(temp_file, self.player_data_path)
            logger.debug(f"Успешно сохранен {self.player_data_path}")
        except Exception as e:
            logger.error(f"Ошибка при записи {self.player_data_path}: {e}")
            raise

    def get_player_username(self) -> str | None:
        """Возвращает никнейм игрока из player-data.json или config.ini."""
        try:
            data = self._read_player_data()
            name = data.get("service-username") or data.get("username")
            if name and isinstance(name, str) and name.strip():
                return name.strip()

            # Fallback: проверка config.ini
            if self.config_ini_path.is_file():
                for line in self.config_ini_path.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines():
                    if line.strip().startswith("service-username=") or line.strip().startswith(
                        "username="
                    ):
                        val = line.split("=", 1)[-1].strip()
                        if val:
                            return val

            return None
        except Exception as e:
            logger.warning(f"Ошибка при получении никнейма игрока: {e}")
            return None

    def set_player_username(self, username: str) -> None:
        """Устанавливает уникальный никнейм игрока в player-data.json.

        Args:
            username: Желаемый никнейм игрока.
        """
        clean_name = username.strip()
        if not clean_name:
            raise ValueError("Никнейм игрока не может быть пустым.")

        try:
            data = self._read_player_data()
            data["service-username"] = clean_name
            data["username"] = clean_name
            self._write_player_data(data)
            logger.info(f"Установлен никнейм игрока: '{clean_name}'")
        except Exception as e:
            logger.error(f"Не удалось установить никнейм '{username}': {e}")
            raise

    def resolve_or_init_player_username(self, fallback_name: str | None = None) -> str:
        """Определяет актуальный никнейм игрока либо инициализирует его.

        Алгоритм:
        1. Читает никнейм из player-data.json / config.ini.
        2. Если имя задано, не пустое, не равно дефолтному 'Player' и длина >= 2 символов — возвращает его.
        3. Если имя отсутствует или равно 'Player':
           - Использует fallback_name (если он валиден и != 'Player').
           - Иначе вычисляет имя учетной записи ОС (getpass.getuser() / Path.home().name),
             очищает от спецсимволов. Если результат пустой или равен 'Player' — использует 'Engineer'.
        4. Автоматически сохраняет вычисленное имя в player-data.json, чтобы игра и сервер
           использовали единый валидный никнейм.

        Args:
            fallback_name: Опциональный резервный никнейм.

        Returns:
            Валидный никнейм игрока.
        """
        current_name = self.get_player_username()
        if current_name and current_name.strip() and current_name.strip().lower() != "player" and len(current_name.strip()) >= 2:
            return current_name.strip()

        # Проверяем fallback_name
        chosen_name: str | None = None
        if fallback_name and fallback_name.strip() and fallback_name.strip().lower() != "player" and len(fallback_name.strip()) >= 2:
            chosen_name = fallback_name.strip()

        # Если fallback не подошел, берем имя пользователя ОС
        if not chosen_name:
            try:
                os_user = getpass.getuser()
            except Exception:
                os_user = Path.home().name

            clean_os_user = re.sub(r"[^a-zA-Z0-9_\-]", "", os_user or "").strip()
            if clean_os_user and clean_os_user.lower() != "player" and len(clean_os_user) >= 2:
                chosen_name = clean_os_user
            else:
                chosen_name = "Engineer"

        try:
            self.set_player_username(chosen_name)
        except Exception as err:
            logger.warning(f"Не удалось автоматически записать никнейм '{chosen_name}' в player-data.json: {err}")

        return chosen_name

    # =========================================================================
    # Раздел 4: Конфигурация мультиплеера и автосохранений
    # =========================================================================

    def prepare_multiplayer_settings(
        self,
        is_host: bool = True,
        proxy_address: str = "127.0.0.1:34198",
        autosave_interval_minutes: int = 2,
    ) -> None:
        """Настраивает параметры игры для бесконфликтного прямого подключения.

        1. В player-data.json:
           - verify-user-identity: false (для standalone / non-Steam версий).
           - public-visible: false (локальное лобби).
           - latest-multiplayer-connections: помещает proxy_address на первое место.
        2. В config.ini:
           - autosave-interval: autosave_interval_minutes (каждые 2 минуты вместо 5).
           - non-blocking-saving: true (плавные фоновые автосейвы без лагов).
        """
        self._tune_player_data(proxy_address)
        self._tune_config_ini(autosave_interval_minutes)

    def _tune_player_data(self, proxy_address: str) -> None:
        """Обновляет сетевые флаги и историю подключений в player-data.json."""
        try:
            data = self._read_player_data()
            data["verify-user-identity"] = False
            data["public-visible"] = False

            connections = data.get("latest-multiplayer-connections", [])
            if not isinstance(connections, list):
                connections = []

            # Помещаем proxy_address во главу списка последних подключений
            if proxy_address in connections:
                connections.remove(proxy_address)
            connections.insert(0, proxy_address)
            data["latest-multiplayer-connections"] = connections[:10]

            self._write_player_data(data)
            logger.info(
                f"Настройки player-data.json обновлены (verify-user-identity=false, proxy={proxy_address})"
            )
        except Exception as e:
            logger.error(f"Ошибка при настройке player-data.json: {e}")
            raise

    def _tune_config_ini(self, autosave_interval_minutes: int) -> None:
        """Настраивает параметры автосейвов в config.ini с сохранением комментариев."""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)

            lines: list[str] = []
            if self.config_ini_path.is_file():
                # Создаем резервную копию перед модификацией
                backup_path = self.config_ini_path.with_suffix(".ini.bak")
                shutil.copy2(self.config_ini_path, backup_path)
                lines = self.config_ini_path.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()

            # Обрабатываем секцию [other] с сохранением структуры файла
            other_section_found = False
            in_other_section = False
            autosave_set = False
            nonblocking_set = False
            new_lines: list[str] = []

            for line in lines:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    if in_other_section:
                        # Вставляем недостающие параметры перед выходом из секции [other]
                        if not autosave_set:
                            new_lines.append(f"autosave-interval={autosave_interval_minutes}")
                            autosave_set = True
                        if not nonblocking_set:
                            new_lines.append("non-blocking-saving=true")
                            nonblocking_set = True
                        in_other_section = False

                    if stripped.lower() == "[other]":
                        other_section_found = True
                        in_other_section = True
                    new_lines.append(line)
                    continue

                if in_other_section:
                    if re.match(r"^autosave-interval\s*=", stripped):
                        new_lines.append(f"autosave-interval={autosave_interval_minutes}")
                        autosave_set = True
                        continue
                    if re.match(r"^non-blocking-saving\s*=", stripped):
                        new_lines.append("non-blocking-saving=true")
                        nonblocking_set = True
                        continue

                new_lines.append(line)

            # Если файл закончился внутри [other]
            if in_other_section:
                if not autosave_set:
                    new_lines.append(f"autosave-interval={autosave_interval_minutes}")
                if not nonblocking_set:
                    new_lines.append("non-blocking-saving=true")

            # Если секции [other] вообще не было
            if not other_section_found:
                if new_lines and new_lines[-1].strip():
                    new_lines.append("")
                new_lines.append("[other]")
                new_lines.append(f"autosave-interval={autosave_interval_minutes}")
                new_lines.append("non-blocking-saving=true")

            temp_ini = self.config_ini_path.with_suffix(".tmp")
            temp_ini.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            os.replace(temp_ini, self.config_ini_path)
            logger.info(
                f"Параметры config.ini обновлены (autosave-interval={autosave_interval_minutes}, non-blocking-saving=true)"
            )
        except Exception as e:
            logger.error(f"Ошибка при настройке config.ini: {e}")
            raise

    # =========================================================================
    # Раздел 5: Манифест активных модов (mod-list.json)
    # =========================================================================

    def get_active_mods_manifest(self) -> list[dict[str, Any]]:
        """Считывает список активных модов из mods/mod-list.json.

        Returns:
            Список словарей с активными модами (enabled == True).
        """
        mod_list_path = self.mods_dir / "mod-list.json"
        if not mod_list_path.is_file():
            logger.debug(f"Файл {mod_list_path} не найден, список модов пуст.")
            return []

        try:
            content = mod_list_path.read_text(encoding="utf-8")
            data = json.loads(content)
            mods_raw = data.get("mods", [])
            if not isinstance(mods_raw, list):
                return []

            active_mods: list[dict[str, Any]] = []
            for item in mods_raw:
                if isinstance(item, dict) and item.get("enabled", False) is True:
                    active_mods.append(item)

            logger.info(f"Найдено {len(active_mods)} активных модов в {mod_list_path}")
            return active_mods
        except Exception as e:
            logger.warning(f"Ошибка при парсинге {mod_list_path}: {e}")
            return []
