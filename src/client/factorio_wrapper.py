"""Точка входа исполняемого перехватчика Factorio (FactorioWrapper).

Служит прозрачной заменой оригинального factorio.exe (или Factorio.app/Contents/MacOS/factorio).
При запуске:
1. Инициализирует логирование в консоль и в файл factorio_wrapper.log.
2. Загружает конфигурацию клиента и окружение путей.
3. Передает управление SessionOrchestrator для выбора роли (Хост / Гость)
   и запуска сетевых служб.
4. Пробрасывает оригинальные параметры командной строки в factorio_core.
5. Завершает процесс с кодом выхода игры и освобождает все ресурсы.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.session_orchestrator import RoleType, SessionOrchestrator

logger = logging.getLogger("factorio_wrapper")


def setup_client_logging(
    log_dir: Optional[Path | str] = None,
    level: int = logging.INFO,
) -> Optional[Path]:
    """Настраивает потоковый вывод в stdout и ротируемый лог-файл перехватчика.

    Args:
        log_dir: Каталог для размещения factorio_wrapper.log (например, user_data_dir).
        level: Уровень логирования.

    Returns:
        Путь к созданному лог-файлу или None.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Проверка, не настроены ли уже обработчики
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
        console_handler = logging.StreamHandler(sys.stdout)
        console_fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(console_fmt)
        root_logger.addHandler(console_handler)

    log_file_path: Optional[Path] = None
    if log_dir is not None:
        try:
            target_dir = Path(log_dir).resolve()
            target_dir.mkdir(parents=True, exist_ok=True)
            log_file_path = target_dir / "factorio_wrapper.log"

            file_handler = logging.handlers.RotatingFileHandler(
                filename=log_file_path,
                maxBytes=5 * 1024 * 1024,  # 5 МБ
                backupCount=2,
                encoding="utf-8",
            )
            file_fmt = logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            file_handler.setFormatter(file_fmt)
            root_logger.addHandler(file_handler)
            logger.debug(f"Логирование перехватчика настроено в {log_file_path}")
        except Exception as err:
            logger.warning(f"Не удалось инициализировать файл лога перехватчика: {err}")

    return log_file_path


def parse_wrapper_args(argv: List[str]) -> Tuple[argparse.Namespace, List[str]]:
    """Разделяет собственные параметры перехватчика и аргументы игрового движка Factorio.

    Собственные параметры начинаются с префикса --wrapper- (или известные сервисные ключи),
    все остальные аргументы без изменений передаются в factorio_core.
    """
    parser = argparse.ArgumentParser(
        description="Factorio P2P Cloud Sync & Coordinator Wrapper",
        add_help=False,
    )
    parser.add_argument("--wrapper-game-root", type=str, default=None, help="Корень игры Factorio")
    parser.add_argument(
        "--wrapper-core-bin",
        type=str,
        default=None,
        help="Путь к оригинальному бинарнику factorio_core",
    )
    parser.add_argument("--wrapper-hub-url", type=str, default=None, help="URL сервера-сейфа")
    parser.add_argument("--wrapper-player-name", type=str, default=None, help="Никнейм игрока")
    parser.add_argument(
        "--wrapper-setup",
        action="store_true",
        help="Принудительно открыть окно настроек и выбора мира перед запуском",
    )
    parser.add_argument(
        "--wrapper-help",
        action="store_true",
        help="Показать справку перехватчика",
    )

    known_args, game_args = parser.parse_known_args(argv)
    return known_args, game_args


def main(argv: Optional[List[str]] = None) -> int:
    """Главная точка входа исполняемого перехватчика Factorio."""
    args_list = argv if argv is not None else sys.argv[1:]
    wrapper_args, game_args = parse_wrapper_args(args_list)

    if wrapper_args.wrapper_help:
        print("Factorio P2P Cloud Sync & Coordinator Wrapper")
        print("Использование: factorio [опции игры...] [--wrapper-game-root ПУТЬ] ...")
        return 0

    # Инициализация окружения путей
    env = FactorioEnvironment(
        custom_game_root=wrapper_args.wrapper_game_root,
    )

    # Инициализация логирования
    setup_client_logging(log_dir=env.user_data_dir)

    logger.info("=" * 60)
    logger.info("Запуск Factorio P2P Wrapper («Сейф + Перехватчик»)")
    logger.info("=" * 60)

    # Проверка наличия сохраненного файла конфигурации factorio_sync_config.json
    config_path = env.sync_config_path
    loaded_config = ClientConfig.load_from_file(config_path)

    # Загрузка конфигурации клиента с переопределениями
    config_overrides = {}
    if wrapper_args.wrapper_hub_url:
        config_overrides["hub_http_url"] = wrapper_args.wrapper_hub_url
    if wrapper_args.wrapper_player_name:
        config_overrides["player_name"] = wrapper_args.wrapper_player_name

    try:
        if loaded_config is not None:
            config = loaded_config
            for k, v in config_overrides.items():
                if v is not None:
                    setattr(config, k, v)
        else:
            config = ClientConfig.from_env(**config_overrides)
    except Exception as conf_err:
        logger.error(f"Фатальная ошибка конфигурации клиента: {conf_err}")
        return 1

    orchestrator = SessionOrchestrator(
        config=config,
        env=env,
        custom_core_bin=wrapper_args.wrapper_core_bin,
        config_path=config_path,
        force_setup_gui=wrapper_args.wrapper_setup,
    )

    try:
        exit_code = orchestrator.run(
            game_args=game_args,
        )
        logger.info(f"Сессия Factorio штатно завершена с кодом: {exit_code}")
        return exit_code
    except KeyboardInterrupt:
        logger.info("Получен сигнал прерывания (Ctrl+C). Остановка...")
        orchestrator.teardown()
        return 130
    except Exception as fatal_err:
        logger.exception(f"Критическая ошибка выполнения перехватчика: {fatal_err}")
        orchestrator.teardown()
        return 1


if __name__ == "__main__":
    sys.exit(main())
