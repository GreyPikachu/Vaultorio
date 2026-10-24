"""Одноразовый установщик и деинсталлятор клиентского перехватчика Factorio.

Обеспечивает:
1. Автоматический поиск папки с игрой Factorio (Windows, macOS, Linux, Steam, Portable).
2. Безопасную установку (install):
   - Защиту оригинального бинарника игры: переименование factorio -> factorio_core.
   - Защиту от повторной установки: если factorio_core уже существует, он НИКОГДА
     не перезаписывается поверх (чтобы не затереть настоящую игру!), а просто обновляется перехватчик.
   - Проверку минимального размера исполняемого файла игры (защита от поврежденных файлов).
   - Интеграцию с player-data.json: уникальный никнейм игрока, verify-user-identity: false,
     предзаполнение адреса прямого подключения 127.0.0.1:34198.
   - Создание конфигурационного файла factorio_sync_config.json.
3. Полный откат (uninstall):
   - Удаление файла перехватчика и возвращение оригинального factorio_core -> factorio.
   - Удаление factorio_sync_config.json и возврат спрятанных сохранений.
   - Идемпотентность при повторном вызове.
4. Консольный интерактивный и автоматический (CLI flags) режимы работы.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.client.factorio_env import FactorioEnvironment

logger = logging.getLogger("factorio_installer")


class FactorioWrapperInstaller:
    """Установщик и менеджер отката перехватчика Factorio."""

    def __init__(
        self,
        default_hub_url: str = "http://127.0.0.1:8088",
        default_stun_host: str = "127.0.0.1",
        default_stun_port: int = 14197,
    ) -> None:
        self.default_hub_url = default_hub_url
        self.default_stun_host = default_stun_host
        self.default_stun_port = int(default_stun_port)

    def find_game_installation(
        self,
        custom_path: Optional[Path | str] = None,
        system_override: Optional[str] = None,
    ) -> Path:
        """Находит директорию установки Factorio.

        Проверяет:
        1. Явно переданный путь custom_path.
        2. Текущую рабочую директорию.
        3. Типовые системные пути в зависимости от ОС.
        """
        current_os = system_override or platform.system()

        # 1. Если передан явный путь
        if custom_path is not None:
            candidate = Path(custom_path).resolve()
            if candidate.is_file():
                # Если указан конкретный исполняемый файл
                if candidate.name.lower().startswith("factorio"):
                    # bin/x64/factorio.exe -> корень игры на 2 уровня выше
                    if candidate.parent.name.lower() == "x64" and candidate.parent.parent.name.lower() == "bin":
                        return candidate.parent.parent.parent
                    if candidate.parent.name == "MacOS" and "Contents" in str(candidate):
                        # Factorio.app/Contents/MacOS/factorio -> корень Factorio.app
                        p = candidate.parent
                        while p and not p.name.endswith(".app"):
                            p = p.parent
                        if p:
                            return p
                    return candidate.parent
            elif candidate.is_dir():
                return candidate

        # 2. Проверка текущей рабочей директории
        cwd = Path.cwd().resolve()
        if self._is_potential_game_dir(cwd, current_os):
            logger.info(f"Найдена игра в текущей директории: {cwd}")
            return cwd

        # 3. Типовые системные пути по умолчанию
        search_paths: List[Path] = []
        if current_os == "Windows":
            for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
                val = os.getenv(env_var)
                if val:
                    search_paths.append(Path(val) / "Factorio")
                    search_paths.append(Path(val) / "Steam" / "steamapps" / "common" / "Factorio")
            search_paths.extend([
                Path("C:/Games/Factorio"),
                Path("D:/Games/Factorio"),
                Path("E:/Games/Factorio"),
                Path("C:/Factorio"),
                Path("D:/Factorio"),
            ])
        elif current_os == "Darwin":
            search_paths.extend([
                Path("/Applications/Factorio.app"),
                Path.home() / "Applications" / "Factorio.app",
                Path.home() / "Library" / "Application Support" / "Steam" / "steamapps" / "common" / "Factorio" / "Factorio.app",
                Path("/Applications/Factorio"),
            ])
        else:  # Linux
            search_paths.extend([
                Path.home() / ".factorio",
                Path("/opt/factorio"),
                Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "Factorio",
                Path.home() / ".steam" / "steam" / "steamapps" / "common" / "Factorio",
            ])

        for path in search_paths:
            if path.exists() and self._is_potential_game_dir(path, current_os):
                logger.info(f"Найдена установка Factorio: {path}")
                return path.resolve()

        raise FileNotFoundError(
            "Не удалось автоматически найти установку игры Factorio. "
            "Пожалуйста, укажите путь к папке игры вручную через --game-dir."
        )

    def _is_potential_game_dir(self, directory: Path, current_os: str) -> bool:
        """Проверяет наличие бинарных файлов Factorio внутри каталога."""
        if not directory.is_dir():
            return False

        # Прямая проверка .app на macOS
        if current_os == "Darwin" and directory.name.endswith(".app"):
            macos_bin = directory / "Contents" / "MacOS" / "factorio"
            macos_core = directory / "Contents" / "MacOS" / "factorio_core"
            return macos_bin.is_file() or macos_core.is_file()

        # Проверка bin/x64
        bin_x64 = directory / "bin" / "x64"
        if bin_x64.is_dir():
            if any((bin_x64 / name).is_file() for name in ("factorio.exe", "factorio", "factorio_core.exe", "factorio_core")):
                return True

        # Проверка в корне каталога
        if any((directory / name).is_file() for name in ("factorio.exe", "factorio", "factorio_core.exe", "factorio_core")):
            return True

        # Проверка Factorio.app внутри папки
        app_inside = directory / "Factorio.app"
        if app_inside.is_dir():
            macos_bin = app_inside / "Contents" / "MacOS" / "factorio"
            macos_core = app_inside / "Contents" / "MacOS" / "factorio_core"
            if macos_bin.is_file() or macos_core.is_file():
                return True

        return False

    def get_binary_paths(
        self,
        game_dir: Path | str,
        system_override: Optional[str] = None,
    ) -> Dict[str, Path]:
        """Возвращает пути к каталогу бинарников, оригинальному и перехватывающему файлам.

        Returns:
            Словарь с ключами:
            - 'bin_dir': Папка с исполняемыми файлами.
            - 'game_binary': Путь к бинарнику, запускаемому пользователем (factorio.exe / factorio).
            - 'core_binary': Путь к переименованному оригиналу игры (factorio_core.exe / factorio_core).
        """
        current_os = system_override or platform.system()
        gdir = Path(game_dir).resolve()

        bin_dir: Path
        exe_suffix = ".exe" if current_os == "Windows" else ""
        orig_name = f"factorio{exe_suffix}"
        core_name = f"factorio_core{exe_suffix}"

        # Обработка macOS .app бандла
        if current_os == "Darwin" and gdir.name.endswith(".app"):
            bin_dir = gdir / "Contents" / "MacOS"
        elif current_os == "Darwin" and (gdir / "Factorio.app").is_dir():
            bin_dir = gdir / "Factorio.app" / "Contents" / "MacOS"
        elif (gdir / "bin" / "x64").is_dir():
            bin_dir = gdir / "bin" / "x64"
        elif (gdir / orig_name).is_file() or (gdir / core_name).is_file():
            bin_dir = gdir
        else:
            # По умолчанию для Windows/Linux предполагаем структуру bin/x64
            bin_dir = gdir / "bin" / "x64"

        return {
            "bin_dir": bin_dir,
            "game_binary": bin_dir / orig_name,
            "core_binary": bin_dir / core_name,
        }

    def is_installed(
        self,
        game_dir: Path | str,
        system_override: Optional[str] = None,
    ) -> bool:
        """Проверяет, установлен ли перехватчик в указанной папке игры."""
        paths = self.get_binary_paths(game_dir, system_override=system_override)
        return paths["core_binary"].is_file()

    def install(
        self,
        game_dir: Path | str,
        player_name: Optional[str] = None,
        wrapper_binary_path: Optional[Path | str] = None,
        hub_url: Optional[str] = None,
        stun_host: Optional[str] = None,
        stun_port: Optional[int] = None,
        min_original_size_bytes: int = 1024,
        system_override: Optional[str] = None,
        device_token: Optional[str] = None,
        device_id: Optional[str] = None,
        client_config: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Выполняет безопасную установку перехватчика в каталог Factorio.

        Args:
            game_dir: Путь к корню игры Factorio.
            player_name: Уникальный никнейм игрока (опционально; если не указан,
                         разрешается существующий никнейм из профиля Factorio).
            wrapper_binary_path: Путь к собранному бинарнику перехватчика (опционально).
            hub_url: URL сервера-сейфа.
            stun_host: Хост STUN/P2P-координатора.
            stun_port: Порт STUN/P2P-координатора.
            min_original_size_bytes: Минимальный допустимый размер оригинального бинарника.
            system_override: Принудительное указание ОС для тестирования.
            device_token: Постоянный токен авторизации устройства.
            device_id: Идентификатор устройства.
            client_config: Готовый объект ClientConfig (если активирован инвайт).

        Returns:
            Отчет об установке.
        """
        gdir = Path(game_dir).resolve()
        if not gdir.is_dir():
            raise FileNotFoundError(f"Каталог игры не найден: {gdir}")

        # Валидация явно переданного никнейма (если передан)
        if player_name is not None:
            clean_input = str(player_name).strip()
            if not clean_input:
                raise ValueError("Никнейм игрока не может быть пустым.")
            if clean_input.lower() in ("player", "игрок"):
                raise ValueError(
                    f"Недопустимый никнейм '{player_name}'. Укажите уникальный ник "
                    f"(например: Alex, Dima), чтобы игра не путала сохранения и роли."
                )

        # Инициализируем окружение Factorio с учетом system_override
        env = FactorioEnvironment(custom_game_root=gdir, system_override=system_override)
        env.ensure_dirs()

        # Извлечение параметров из client_config, если передан
        if client_config is not None:
            hub_url = getattr(client_config, "hub_http_url", hub_url)
            stun_host = getattr(client_config, "hub_stun_host", stun_host)
            stun_port = getattr(client_config, "hub_stun_port", stun_port)
            device_token = getattr(client_config, "device_token", device_token)
            device_id = getattr(client_config, "device_id", device_id)
            cfg_pname = getattr(client_config, "player_name", None)
            if cfg_pname and str(cfg_pname).strip():
                player_name = str(cfg_pname).strip()

        # Шаг 1: Определение и сохранение никнейма игрока
        # Если в player-data.json уже есть валидный никнейм, он никогда не перезаписывается!
        clean_name = env.resolve_or_init_player_username(fallback_name=player_name)
        if not clean_name:
            raise ValueError("Не удалось определить никнейм игрока.")
        logger.info(f"Используется никнейм игрока: '{clean_name}'")

        paths = self.get_binary_paths(gdir, system_override=system_override)
        bin_dir = paths["bin_dir"]
        game_bin = paths["game_binary"]
        core_bin = paths["core_binary"]

        bin_dir.mkdir(parents=True, exist_ok=True)

        is_update = False

        # Шаг 2: Защита оригинального файла игры
        # СЛУЧАЙ А: factorio_core УЖЕ существует (повторная установка / обновление)
        if core_bin.is_file():
            is_update = True
            logger.info(
                f"Обнаружен ранее установленный перехватчик (найден {core_bin.name}). "
                f"Оригинальный файл игры НЕ перезаписывается. Выполняется обновление перехватчика."
            )
            # Копируем новый бинарник перехватчика поверх текущего game_bin
            if wrapper_binary_path is not None:
                src_wrapper = Path(wrapper_binary_path).resolve()
                if not src_wrapper.is_file():
                    raise FileNotFoundError(f"Файл перехватчика для установки не найден: {src_wrapper}")
                shutil.copy2(src_wrapper, game_bin)
                os.chmod(game_bin, 0o755)
                logger.info(f"Файл перехватчика {game_bin.name} успешно обновлен.")

        # СЛУЧАЙ Б: Первичная установка (factorio_core еще нет)
        else:
            if not game_bin.is_file():
                raise FileNotFoundError(
                    f"Оригинальный исполняемый файл игры {game_bin} не найден. "
                    f"Убедитесь, что указана корректная директория Factorio."
                )

            # Проверка размера оригинального файла
            orig_size = game_bin.stat().st_size
            if orig_size < min_original_size_bytes:
                raise ValueError(
                    f"Файл {game_bin} имеет подозрительно малый размер ({orig_size} байт). "
                    f"Оригинальный бинарник поврежден или является заглушкой."
                )

            # Безопасное переименование оригинала игры: factorio -> factorio_core
            logger.info(f"Резервирование оригинального бинарника игры: {game_bin.name} -> {core_bin.name}")
            shutil.move(str(game_bin), str(core_bin))
            os.chmod(core_bin, 0o755)

            # Размещение файла перехватчика
            if wrapper_binary_path is not None:
                src_wrapper = Path(wrapper_binary_path).resolve()
                if not src_wrapper.is_file():
                    raise FileNotFoundError(f"Файл перехватчика для установки не найден: {src_wrapper}")
                shutil.copy2(src_wrapper, game_bin)
            else:
                # Если бинарник не передан, создаем скрипт-обертку запуска
                self._create_fallback_wrapper(game_bin, core_bin)

            os.chmod(game_bin, 0o755)
            logger.info(f"Перехватчик успешно установлен на место {game_bin.name}")

        # Шаг 3: Настройка окружения Factorio и сетевых параметров (без перезаписи ника)
        env.prepare_multiplayer_settings(
            is_host=False,
            proxy_address="127.0.0.1:34198",
            autosave_interval_minutes=2,
        )

        # Шаг 4: Запись конфигурационного файла factorio_sync_config.json
        config_data = {
            "version": "1.0.0",
            "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "hub_http_url": hub_url or self.default_hub_url,
            "hub_stun_host": stun_host or self.default_stun_host,
            "hub_stun_port": stun_port or self.default_stun_port,
            "player_name": clean_name,
            "device_token": device_token or "",
            "device_id": device_id or "",
            "is_update": is_update,
        }
        sync_cfg_path = gdir / "factorio_sync_config.json"
        sync_cfg_path.write_text(json.dumps(config_data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Конфигурация синхронизации сохранена в {sync_cfg_path.name}")

        # Также сохраняем в user_data_dir (если пути отличаются)
        try:
            user_cfg_path = env.sync_config_path
            if user_cfg_path.resolve() != sync_cfg_path.resolve():
                user_cfg_path.parent.mkdir(parents=True, exist_ok=True)
                user_cfg_path.write_text(json.dumps(config_data, indent=2, ensure_ascii=False), encoding="utf-8")
                logger.info(f"Конфигурация синхронизации также сохранена в {user_cfg_path}")
        except Exception as sync_write_err:
            logger.debug(f"Не удалось дополнительно сохранить конфиг в user_data_dir: {sync_write_err}")

        return {
            "status": "installed",
            "is_update": is_update,
            "game_dir": str(gdir),
            "bin_dir": str(bin_dir),
            "game_binary": str(game_bin),
            "core_binary": str(core_bin),
            "player_name": clean_name,
            "config_path": str(sync_cfg_path),
        }

    def _create_fallback_wrapper(self, target_bin: Path, core_bin: Path) -> None:
        """Создает резервный исполняемый скрипт-перехватчик, если нет скомпилированного PyInstaller-бинарника."""
        is_windows = target_bin.suffix.lower() == ".exe"
        python_bin = sys.executable
        project_root = Path(__file__).resolve().parent.parent.parent
        if is_windows:
            # Для Windows создаем batch-обертку
            cmd_script = target_bin.with_suffix(".cmd")
            cmd_script.write_text(
                f"@echo off\n"
                f'set "PYTHONPATH={project_root};%PYTHONPATH%"\n'
                f'"{python_bin}" -m src.client.factorio_wrapper --wrapper-core-bin "{core_bin}" %*\n',
                encoding="utf-8",
            )
            # В качестве заглушки копируем ядро
            shutil.copy2(core_bin, target_bin)
        else:
            # Для macOS/Linux создаем шелл-скрипт запуска перехватчика
            target_bin.write_text(
                f"#!/bin/bash\n"
                f"# Factorio P2P Cloud Sync Wrapper\n"
                f'export PYTHONPATH="{project_root}:${{PYTHONPATH}}"\n'
                f'exec "{python_bin}" -m src.client.factorio_wrapper --wrapper-core-bin "{core_bin}" "$@"\n',
                encoding="utf-8",
            )
            # На macOS подписываем ad-hoc ядро factorio_core, чтобы AMFI / Hardened Runtime разрешали запуск
            if sys.platform == "darwin" and shutil.which("codesign"):
                try:
                    subprocess.run(
                        ["codesign", "-s", "-", "--force", str(core_bin)],
                        check=False,
                        capture_output=True,
                    )
                except Exception as cs_err:
                    logger.debug(f"codesign не выполнен для {core_bin}: {cs_err}")

    def uninstall(
        self,
        game_dir: Path | str,
        system_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Выполняет деинсталляцию перехватчика и полный возврат к чистой игре.

        Args:
            game_dir: Путь к корню игры Factorio.
            system_override: Принудительное указание ОС для тестирования.

        Returns:
            Отчет о деинсталляции.
        """
        gdir = Path(game_dir).resolve()
        paths = self.get_binary_paths(gdir, system_override=system_override)
        game_bin = paths["game_binary"]
        core_bin = paths["core_binary"]

        # Если factorio_core существует — возвращаем его на место factorio
        if core_bin.is_file():
            logger.info(f"Восстановление оригинальной игры: удаляем {game_bin.name} и возвращаем {core_bin.name}")
            game_bin.unlink(missing_ok=True)
            shutil.move(str(core_bin), str(game_bin))
            os.chmod(game_bin, 0o755)
            if sys.platform == "darwin" and shutil.which("codesign"):
                try:
                    subprocess.run(
                        ["codesign", "-s", "-", "--force", str(game_bin)],
                        check=False,
                        capture_output=True,
                    )
                except Exception:
                    pass
            logger.info(f"Оригинальный бинарник игры успешно восстановлен в {game_bin.name}")

            # Удаление конфигурационного файла factorio_sync_config.json
            sync_cfg = gdir / "factorio_sync_config.json"
            sync_cfg.unlink(missing_ok=True)

            # Восстановление спрятанных сейвов из .cloud_stash/ обратно в корень saves/
            env = FactorioEnvironment(custom_game_root=gdir)
            try:
                env.sync_config_path.unlink(missing_ok=True)
            except Exception:
                pass
            env.restore_hidden_cloud_save()

            # Удаление временной директории .cloud_stash (если осталась пустой)
            cloud_stash_dir = env.saves_dir / ".cloud_stash"
            if cloud_stash_dir.exists() and cloud_stash_dir.is_dir():
                try:
                    for f in cloud_stash_dir.iterdir():
                        shutil.move(str(f), str(env.saves_dir / f.name))
                    cloud_stash_dir.rmdir()
                except Exception as stash_err:
                    logger.warning(f"Ошибка при очистке .cloud_stash: {stash_err}")

            # Удаление Windows fallback-скрипта (.cmd)
            game_bin.with_suffix(".cmd").unlink(missing_ok=True)

            # Удаление лог-файлов перехватчика
            for log_name in ["factorio_wrapper.log", "factorio_sync.log"]:
                (gdir / log_name).unlink(missing_ok=True)
                if getattr(env, "user_data_dir", None) and env.user_data_dir != gdir:
                    (env.user_data_dir / log_name).unlink(missing_ok=True)

            return {
                "status": "uninstalled",
                "game_dir": str(gdir),
                "restored_binary": str(game_bin),
                "message": "Перехватчик успешно удален. Игра полностью возвращена в исходное состояние.",
            }

        # Если factorio_core нет, а factorio есть — игра уже чистая
        if game_bin.is_file():
            logger.info(f"Файл {core_bin.name} не обнаружен. Игра уже находится в исходном состоянии.")
            return {
                "status": "already_uninstalled",
                "game_dir": str(gdir),
                "game_binary": str(game_bin),
                "message": "Оригинальная игра уже в чистом состоянии (перехватчик не был установлен).",
            }

        raise FileNotFoundError(
            f"Не найдены исполняемые файлы игры в {paths['bin_dir']}. "
            f"Проверьте правильность указания директории игры."
        )

    def run_gui(
        self,
        custom_game_dir: Optional[Path | str] = None,
        wrapper_bin: Optional[Path | str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Запускает графический установщик FirstRunSetupWindow в режиме installer_mode=True."""
        try:
            from src.client.setup_gui import run_first_time_setup_gui
        except ImportError:
            return None

        game_path: Optional[Path] = None
        try:
            game_path = self.find_game_installation(custom_path=custom_game_dir)
        except Exception:
            pass

        env = FactorioEnvironment(custom_game_root=game_path)
        install_result: Dict[str, Any] = {}

        def _do_install(target_game_dir: Path, player_name: str, cfg: Any) -> None:
            res = self.install(
                game_dir=target_game_dir,
                player_name=player_name,
                wrapper_binary_path=wrapper_bin,
                client_config=cfg,
            )
            install_result.update(res)

        def _do_uninstall(target_game_dir: Path) -> Any:
            return self.uninstall(game_dir=target_game_dir)

        cfg_res = run_first_time_setup_gui(
            env=env,
            installer_mode=True,
            on_install_callback=_do_install,
            on_uninstall_callback=_do_uninstall,
        )

        if cfg_res is not None or install_result:
            return install_result or {"status": "installed", "config": cfg_res}

        return None

    def interactive_menu(self) -> int:
        """Интерактивное консольное меню для пользователей."""
        print("=" * 65)
        print("   Factorio P2P Cloud Sync & Coordinator — Установщик")
        print("=" * 65)
        print("1. Установить / Обновить перехватчик для совместной игры")
        print("2. Удалить перехватчик (вернуть чистую оригинальную Factorio)")
        print("3. Выход")
        print("-" * 65)

        try:
            choice = input("Выберите действие [1/2/3]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nОтмена операции.")
            return 0

        if choice == "3":
            print("Выход.")
            return 0

        # Поиск папки с игрой
        try:
            game_dir = self.find_game_installation()
            print(f"Обнаружена папка с игрой: {game_dir}")
            change = input("Использовать эту папку? [Y/n]: ").strip().lower()
            if change in ("n", "no", "нет", "н"):
                user_path = input("Введите полный путь к папке игры Factorio: ").strip()
                game_dir = Path(user_path).resolve()
        except Exception:
            user_path = input("Введите полный путь к папке игры Factorio: ").strip()
            game_dir = Path(user_path).resolve()

        if choice == "1":
            try:
                name = input("Введите ваш уникальный никнейм в игре (например: Alex, Dima): ").strip()
                result = self.install(game_dir=game_dir, player_name=name)
                print("\nУСТАНОВКА УСПЕШНО ЗАВЕРШЕНА!")
                print(f"- Игрок: {result['player_name']}")
                print(f"- Статус: {'Обновлено' if result['is_update'] else 'Установлено'}")
                print(f"- Запуск игры: через стандартный ярлык Factorio")
                return 0
            except Exception as e:
                print(f"\nОШИБКА УСТАНОВКИ: {e}")
                return 1

        elif choice == "2":
            try:
                result = self.uninstall(game_dir=game_dir)
                print(f"\n{result['message']}")
                return 0
            except Exception as e:
                print(f"\nОШИБКА ДЕИНСТАЛЛЯЦИИ: {e}")
                return 1

        else:
            print("Некорректный выбор.")
            return 1


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа CLI установщика Factorio Coop Setup."""
    parser = argparse.ArgumentParser(
        description="Factorio P2P Cloud Sync — Установщик и деинсталлятор",
    )
    parser.add_argument("--install", action="store_true", help="Выполнить установку перехватчика")
    parser.add_argument("--uninstall", action="store_true", help="Выполнить удаление перехватчика и возврат игры")
    parser.add_argument("--game-dir", type=str, default=None, help="Путь к папке игры Factorio")
    parser.add_argument("--player-name", type=str, default=None, help="Уникальный никнейм игрока")
    parser.add_argument("--wrapper-bin", type=str, default=None, help="Путь к скомпилированному бинарнику перехватчика")
    parser.add_argument("--hub-url", type=str, default=None, help="URL сервера-сейфа")
    parser.add_argument("--invite-code", type=str, default=None, help="Одноразовый Код Дружбы FCT1-...")
    parser.add_argument("--passphrase", type=str, default=None, help="Секретное слово инвайта")
    parser.add_argument("--cli", action="store_true", help="Принудительно использовать консольный интерфейс вместо GUI")

    args = parser.parse_args(argv)
    installer = FactorioWrapperInstaller(
        default_hub_url=args.hub_url or "http://127.0.0.1:8088",
    )

    # Если параметры не переданы — запуск GUI установщика (при отсутствии дисплея откат на консоль)
    if not args.install and not args.uninstall:
        if not args.cli:
            gui_res = installer.run_gui(custom_game_dir=args.game_dir, wrapper_bin=args.wrapper_bin)
            if gui_res is not None:
                print("Установка через графический интерфейс успешно завершена.")
                return 0
        return installer.interactive_menu()

    try:
        game_path = installer.find_game_installation(custom_path=args.game_dir)

        if args.install:
            clean_player = args.player_name
            cfg = None

            temp_env = FactorioEnvironment(custom_game_root=game_path)
            if not clean_player:
                clean_player = temp_env.resolve_or_init_player_username()

            if args.invite_code and args.passphrase:
                from src.client.hub_client import HubClient
                cfg = HubClient.activate_with_invite_code(
                    invite_code=args.invite_code,
                    passphrase=args.passphrase,
                    player_name=clean_player,
                )

            res = installer.install(
                game_dir=game_path,
                player_name=clean_player,
                wrapper_binary_path=args.wrapper_bin,
                client_config=cfg,
            )
            print(f"Успешно установлено: {res['game_binary']} (Игрок: {res['player_name']})")
            return 0

        if args.uninstall:
            res = installer.uninstall(game_dir=game_path)
            print(f"Успешно удалено: {res['message']}")
            return 0

    except Exception as err:
        logger.error(f"Ошибка выполнения установщика: {err}")
        print(f"ОШИБКА: {err}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
