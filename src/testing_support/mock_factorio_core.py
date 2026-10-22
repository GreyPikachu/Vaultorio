"""Имитатор оригинального исполняемого файла Factorio (factorio_core).

Используется в сквозных интеграционных тестах (E2E) и при автоматизированной проверке:
1. Режим Хоста (host):
   - Записывает маркер хостинга в factorio-current.log ('Hosting game at 0.0.0.0:34197').
   - Открывает локальный UDP-сокет на 127.0.0.1:34197.
   - Отвечает на сетевые датаграммы клиентов (эхо-ответ).
   - Создает тестовые архивы автосохранений _autosave1.zip в каталоге saves/.
2. Режим Гостя (guest):
   - Проверяет отсутствие cloud_world.zip в корне saves/ (защита от Split-Brain).
   - Читает адрес 127.0.0.1:34198 из player-data.json и шлет проверочные датаграммы в прокси.
3. Режим меню («Фальшивый Хост», idle_menu):
   - Имитирует нахождение игрока в главном меню (без запуска сервера и без сокета).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import select
import socket
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [mock_core] %(message)s")
logger = logging.getLogger("mock_factorio_core")


def create_dummy_save(path: Path, content: str = "mock_factorio_level_data") -> Path:
    """Создает валидный ZIP-архив сохранения Factorio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = path.with_suffix(".tmp")
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cloud_world/level.dat", content.encode("utf-8"))
        zf.writestr("cloud_world/info.json", b'{"save_name": "cloud_world", "version": 1}')
    os.replace(temp_zip, path)
    return path


class MockFactorioCore:
    """Имитатор движка игры Factorio."""

    def __init__(
        self,
        mode: str = "host",
        user_data_dir: Optional[Path | str] = None,
        duration_sec: float = 1.0,
        factorio_port: int = 34197,
        guest_proxy_port: int = 34198,
        generate_autosaves: int = 1,
    ) -> None:
        self.mode = mode.lower()
        self.user_data_dir = Path(user_data_dir).resolve() if user_data_dir else Path.cwd()
        self.duration_sec = max(0.1, float(duration_sec))
        self.factorio_port = int(factorio_port)
        self.guest_proxy_port = int(guest_proxy_port)
        self.generate_autosaves = int(generate_autosaves)

        self.saves_dir = self.user_data_dir / "saves"
        self.log_file = self.user_data_dir / "factorio-current.log"
        self.player_data_file = self.user_data_dir / "player-data.json"

    def run(self) -> int:
        """Главный цикл работы имитатора."""
        logger.info(f"Запуск mock_factorio_core в режиме '{self.mode}' (длительность: {self.duration_sec}с)")
        self.saves_dir.mkdir(parents=True, exist_ok=True)

        if self.mode == "host":
            return self._run_host_mode()
        elif self.mode == "guest":
            return self._run_guest_mode()
        elif self.mode == "idle_menu":
            return self._run_idle_menu_mode()
        else:
            logger.error(f"Неизвестный режим работы: '{self.mode}'")
            return 1

    def _run_host_mode(self) -> int:
        """Поведение Хоста: лог хостинга, сокет 34197, генерация _autosave1.zip."""
        # 1. Запись в лог factorio-current.log
        startup_log = (
            "   0.000 2026-10-23 00:00:00; Factorio 2.0.0 (build 0, macOS, non-steam)\n"
            "   0.001 Operating system: macOS\n"
            f"   0.050 Hosting game at 0.0.0.0:{self.factorio_port}\n"
        )
        self.log_file.write_text(startup_log, encoding="utf-8")
        logger.info(f"Лог хостинга записан в {self.log_file}")

        # 2. Открытие игрового сокета Factorio
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind(("127.0.0.1", self.factorio_port))
            server_sock.setblocking(False)
            logger.info(f"Игровой сервер Factorio слушает на 127.0.0.1:{self.factorio_port}")
        except Exception as e:
            logger.error(f"Не удалось открыть сокет сервера на порту {self.factorio_port}: {e}")
            server_sock.close()
            return 1

        # 3. Генерация периодических автосейвов
        autosave_file = self.saves_dir / "_autosave1.zip"
        create_dummy_save(autosave_file, "autosave_content_mock_1")
        logger.info(f"Сгенерирован файл автосохранения: {autosave_file}")

        # 4. Цикл обработки сетевых датаграмм и ожидания
        start_time = time.monotonic()
        while time.monotonic() - start_time < self.duration_sec:
            try:
                readable, _, _ = select.select([server_sock], [], [], 0.05)
                for s in readable:
                    data, addr = s.recvfrom(65535)
                    logger.debug(f"Получена датаграмма от {addr}: {len(data)} байт")
                    # Отправляем подтверждение клиенту
                    resp = b"MOCK_FACTORIO_ACK:" + data
                    s.sendto(resp, addr)
            except Exception as err:
                logger.debug(f"Ошибка чтения сокета: {err}")

        server_sock.close()
        logger.info("Сессия Хоста Factorio завершена.")
        return 0

    def _run_guest_mode(self) -> int:
        """Поведение Гостя: проверка отсутствия cloud_world.zip в корне saves/, отправка пакетов."""
        # 1. Запись стартового лога клиента
        self.log_file.write_text("   0.000 Initializing Factorio Guest Client...\n", encoding="utf-8")

        # 2. Проверка защиты от Split-Brain: файл cloud_world.zip должен быть скрыт!
        cloud_save = self.saves_dir / "cloud_world.zip"
        stashed_save = self.saves_dir / ".cloud_stash" / "cloud_world.zip"

        if cloud_save.is_file():
            logger.warning(
                "ПРЕДУПРЕЖДЕНИЕ: cloud_world.zip обнаружен в корне saves/! "
                "Split-Brain Shield должен был скрыть его в .cloud_stash/."
            )
        elif stashed_save.is_file():
            logger.info("УСПЕХ Split-Brain Shield: cloud_world.zip надежно спрятан в .cloud_stash/.")

        # 3. Чтение player-data.json
        preset_addr = "127.0.0.1:34198"
        if self.player_data_file.is_file():
            try:
                pdata = json.loads(self.player_data_file.read_text(encoding="utf-8"))
                connections = pdata.get("latest-multiplayer-connections", [])
                if connections:
                    preset_addr = connections[0]
            except Exception:
                pass

        logger.info(f"Гость подключается по адресу из player-data.json: {preset_addr}")

        # 4. Отправка тестового пакета в локальный прокси
        parts = preset_addr.split(":")
        proxy_ip = parts[0]
        proxy_port = int(parts[1]) if len(parts) > 1 else self.guest_proxy_port

        client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            client_sock.sendto(b"PING_FACTORIO_GUEST", (proxy_ip, proxy_port))
        except Exception as err:
            logger.debug(f"Ошибка отправки PING на прокси: {err}")
        finally:
            client_sock.close()

        time.sleep(self.duration_sec)
        logger.info("Сессия Гостя Factorio завершена.")
        return 0

    def _run_idle_menu_mode(self) -> int:
        """Поведение Фальшивого Хоста: игрок в меню (нет лога хостинга, нет сетевого сокета)."""
        self.log_file.write_text(
            "   0.000 Initializing Factorio...\n"
            "   0.500 Entered Main Menu. Sitting idle...\n",
            encoding="utf-8",
        )
        logger.info("Игрок находится в главном меню Factorio (хостинг не запущен).")
        time.sleep(self.duration_sec)
        logger.info("Игрок закрыл главное меню Factorio.")
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа mock_factorio_core."""
    parser = argparse.ArgumentParser(description="Mock Factorio Core Engine")
    parser.add_argument(
        "--mode",
        choices=["host", "guest", "idle_menu"],
        default="host",
        help="Режим имитации (host, guest или idle_menu)",
    )
    parser.add_argument("--user-data-dir", type=str, default=None, help="Каталог пользовательских данных Factorio")
    parser.add_argument("--duration", type=float, default=0.5, help="Время работы процесса в секундах")
    parser.add_argument("--port", type=int, default=34197, help="Порт игрового сервера")
    parser.add_argument("--proxy-port", type=int, default=34198, help="Порт прокси Гостя")

    known, _ = parser.parse_known_args(argv)

    core = MockFactorioCore(
        mode=known.mode,
        user_data_dir=known.user_data_dir,
        duration_sec=known.duration,
        factorio_port=known.port,
        guest_proxy_port=known.proxy_port,
    )
    return core.run()


if __name__ == "__main__":
    sys.exit(main())
