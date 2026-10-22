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
        player_name: str,
        wrapper_binary_path: Optional[Path | str] = None,
        hub_url: Optional[str] = None,
        stun_host: Optional[str] = None,
        stun_port: Optional[int] = None,
        min_original_size_bytes: int = 1024,
        system_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Выполняет безопасную установку перехватчика в каталог Factorio.

        Args:
            game_dir: Путь к корню игры Factorio.
            player_name: Уникальный никнейм игрока.
            wrapper_binary_path: Путь к собранному бинарнику перехватчика (опционально).
            hub_url: URL сервера-сейфа.
            stun_host: Хост STUN/P2P-координатора.
            stun_port: Порт STUN/P2P-координатора.
            min_original_size_bytes: Минимальный допустимый размер оригинального бинарника.
            system_override: Принудительное указание ОС для тестирования.

        Returns:
            Отчет об установке.
        """
        # Шаг 1: Валидация никнейма игрока
        clean_name = str(player_name).strip()
        if not clean_name:
            raise ValueError("Никнейм игрока не может быть пустым.")
        if clean_name.lower() in ("player", "игрок"):
            raise ValueError(
                f"Недопустимый никнейм '{player_name}'. Укажите уникальный ник "
                f"(например: Alex, Dima), чтобы игра не путала сохранения и роли."
            )

        gdir = Path(game_dir).resolve()
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

        # Шаг 3: Настройка окружения Factorio и сетевых параметров
        env = FactorioEnvironment(custom_game_root=gdir)
        env.ensure_dirs()
        env.set_player_username(clean_name)
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
            "is_update": is_update,
        }
        sync_cfg_path = gdir / "factorio_sync_config.json"
        sync_cfg_path.write_text(json.dumps(config_data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Конфигурация синхронизации сохранена в {sync_cfg_path.name}")

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
        if is_windows:
            # Для Windows создаем batch-обертку
            cmd_script = target_bin.with_suffix(".cmd")
            cmd_script.write_text(
                f"@echo off\n"
                f"echo [Factorio Coop Wrapper] Starting session...\n"
                f'"{core_bin}" %*\n',
                encoding="utf-8",
            )
            # В качестве заглушки копируем ядро
            shutil.copy2(core_bin, target_bin)
        else:
            # Для macOS/Linux создаем шелл-скрипт
            target_bin.write_text(
                f"#!/bin/sh\n"
                f"# Factorio P2P Cloud Sync Wrapper\n"
                f'exec "{core_bin}" "$@"\n',
                encoding="utf-8",
            )

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
            logger.info(f"Оригинальный бинарник игры успешно восстановлен в {game_bin.name}")

            # Удаление конфигурационного файла factorio_sync_config.json
            sync_cfg = gdir / "factorio_sync_config.json"
            sync_cfg.unlink(missing_ok=True)

            # Восстановление спрятанных сейвов из .cloud_stash/ обратно в корень saves/
            env = FactorioEnvironment(custom_game_root=gdir)
            env.restore_hidden_cloud_save()

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

    args = parser.parse_args(argv)
    installer = FactorioWrapperInstaller(
        default_hub_url=args.hub_url or "http://127.0.0.1:8088",
    )

    # Если параметры не переданы — запуск интерактивного меню
    if not args.install and not args.uninstall:
        return installer.interactive_menu()

    try:
        game_path = installer.find_game_installation(custom_path=args.game_dir)

        if args.install:
            if not args.player_name:
                print("ОШИБКА: Для установки требуется указать --player-name <имя>.")
                return 1
            res = installer.install(
                game_dir=game_path,
                player_name=args.player_name,
                wrapper_binary_path=args.wrapper_bin,
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
