"""Скрипт автоматизированной сборки standalone-исполняемых файлов с помощью PyInstaller.

Собирает:
1. `factorio_wrapper` — автономный перехватчик запуска Factorio (подменяет factorio.exe/factorio).
2. `Factorio Coop Setup` — одноразовый установщик и деинсталлятор для друзей.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_client")


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_pyinstaller_command() -> List[str]:
    """Возвращает базовую команду вызова PyInstaller в текущем Python окружении."""
    return [sys.executable, "-m", "PyInstaller"]


def build_wrapper(
    dist_dir: Path,
    work_dir: Path,
    clean: bool = True,
    icon_path: Optional[Path] = None,
) -> Path:
    """Собирает standalone-бинарник клиентского перехватчика factorio_wrapper."""
    logger.info("=" * 60)
    logger.info("Сборка исполняемого перехватчика: factorio_wrapper")
    logger.info("=" * 60)

    dist_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    entry_point = PROJECT_ROOT / "src" / "client" / "factorio_wrapper.py"
    if not entry_point.is_file():
        raise FileNotFoundError(f"Точка входа {entry_point} не найдена!")

    cmd = get_pyinstaller_command() + [
        "--onefile",
        "--noconsole",
        "--name", "factorio_wrapper",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir / "wrapper_build"),
        "--specpath", str(work_dir / "wrapper_spec"),
        "--paths", str(PROJECT_ROOT),
        "--hidden-import", "src",
        "--hidden-import", "src.client",
        "--hidden-import", "src.client.config",
        "--hidden-import", "src.client.factorio_env",
        "--hidden-import", "src.client.hub_client",
        "--hidden-import", "src.client.host_workers",
        "--hidden-import", "src.client.p2p_tunnel",
        "--hidden-import", "src.client.session_orchestrator",
        "--hidden-import", "src.client.setup_gui",
        "--hidden-import", "src.client.ui_notifier",
        "--hidden-import", "src.client.shift_detector",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        "--hidden-import", "httpx",
    ]

    # Внедрение оригинальной иконки Factorio, чтобы factorio.exe не имел значок Python
    if icon_path is None:
        default_icon = PROJECT_ROOT / "resources" / "factorio.ico"
        if default_icon.is_file():
            icon_path = default_icon

    if icon_path is not None and Path(icon_path).is_file():
        cmd.extend(["--icon", str(icon_path)])
        logger.info(f"Внедрение иконки в перехватчик: {icon_path}")

    if clean:
        cmd.append("--clean")

    cmd.append(str(entry_point))

    logger.info(f"Выполнение команды: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        raise RuntimeError(f"PyInstaller завершился с ошибкой (код {result.returncode}) при сборке wrapper.")

    exe_suffix = ".exe" if platform.system() == "Windows" else ""
    output_bin = dist_dir / f"factorio_wrapper{exe_suffix}"
    logger.info(f"Перехватчик успешно собран: {output_bin}")
    return output_bin


def build_installer(
    dist_dir: Path,
    work_dir: Path,
    wrapper_binary: Optional[Path] = None,
    clean: bool = True,
    icon_path: Optional[Path] = None,
) -> Path:
    """Собирает standalone-установщик Factorio Coop Setup."""
    logger.info("=" * 60)
    logger.info("Сборка автономного установщика: Factorio Coop Setup")
    logger.info("=" * 60)

    dist_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    entry_point = PROJECT_ROOT / "src" / "installer" / "installer.py"
    if not entry_point.is_file():
        raise FileNotFoundError(f"Точка входа {entry_point} не найдена!")

    cmd = get_pyinstaller_command() + [
        "--onefile",
        "--noconsole",
        "--name", "Factorio Coop Setup",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir / "installer_build"),
        "--specpath", str(work_dir / "installer_spec"),
        "--paths", str(PROJECT_ROOT),
        "--hidden-import", "src",
        "--hidden-import", "src.installer",
        "--hidden-import", "src.installer.installer",
        "--hidden-import", "src.client",
        "--hidden-import", "src.client.config",
        "--hidden-import", "src.client.factorio_env",
        "--hidden-import", "src.client.hub_client",
        "--hidden-import", "src.client.setup_gui",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
    ]

    # Внедрение иконки в установщик
    if icon_path is None:
        default_icon = PROJECT_ROOT / "resources" / "factorio.ico"
        if default_icon.is_file():
            icon_path = default_icon

    if icon_path is not None and Path(icon_path).is_file():
        cmd.extend(["--icon", str(icon_path)])
        logger.info(f"Внедрение иконки в установщик: {icon_path}")

    # Если собран бинарник перехватчика, упаковываем его внутрь установщика
    if wrapper_binary is not None and wrapper_binary.is_file():
        sep = ";" if platform.system() == "Windows" else ":"
        cmd.extend(["--add-data", f"{wrapper_binary}{sep}."])

    if clean:
        cmd.append("--clean")

    cmd.append(str(entry_point))

    logger.info(f"Выполнение команды: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        raise RuntimeError(f"PyInstaller завершился с ошибкой (код {result.returncode}) при сборке installer.")

    exe_suffix = ".exe" if platform.system() == "Windows" else ""
    output_bin = dist_dir / f"Factorio Coop Setup{exe_suffix}"
    logger.info(f"Установщик успешно собран: {output_bin}")
    return output_bin


def main(argv: Optional[List[str]] = None) -> int:
    """Главная точка входа скрипта сборки."""
    parser = argparse.ArgumentParser(description="Сборка standalone-бинарников Factorio P2P")
    parser.add_argument(
        "--target",
        choices=["wrapper", "installer", "all"],
        default="all",
        help="Цель сборки (wrapper, installer или all)",
    )
    parser.add_argument(
        "--dist-dir",
        type=str,
        default=str(PROJECT_ROOT / "dist"),
        help="Каталог вывода собранных бинарников",
    )
    parser.add_argument(
        "--work-dir",
        type=str,
        default=str(PROJECT_ROOT / "build_temp"),
        help="Временный рабочий каталог PyInstaller",
    )
    parser.add_argument("--clean", action="store_true", default=True, help="Очищать временный кэш сборки")
    parser.add_argument("--icon", type=str, default=None, help="Путь к файлу иконки .ico")

    args = parser.parse_args(argv)
    dist = Path(args.dist_dir).resolve()
    work = Path(args.work_dir).resolve()
    icon = Path(args.icon).resolve() if args.icon else None

    try:
        wrapper_bin: Optional[Path] = None
        if args.target in ("wrapper", "all"):
            wrapper_bin = build_wrapper(dist_dir=dist, work_dir=work, clean=args.clean, icon_path=icon)

        if args.target in ("installer", "all"):
            build_installer(dist_dir=dist, work_dir=work, wrapper_binary=wrapper_bin, clean=args.clean, icon_path=icon)

        logger.info("Сборка всех компонентов успешно завершена!")
        return 0

    except Exception as err:
        logger.error(f"Ошибка при сборке: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
