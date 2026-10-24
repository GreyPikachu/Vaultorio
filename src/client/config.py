"""Конфигурация клиента Factorio P2P Cloud Sync.

Содержит настройки подключения к VPS-хабу («Сейфу»), STUN-координатору,
авторизационные токены устройства (device_token) и порты игры/прокси.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("factorio_client.config")


@dataclass
class ClientConfig:
    """Параметры подключения и сетевых сокетов клиента Factorio."""

    hub_http_url: str = "http://127.0.0.1:8088"
    hub_stun_host: str = "127.0.0.1"
    hub_stun_port: int = 14197
    cloud_save_name: str = "cloud_world.zip"
    host_factorio_port: int = 34197
    guest_proxy_port: int = 34198
    player_name: str = ""
    device_token: str = ""
    device_id: str = ""
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 30.0
    was_uninstalled: bool = False
    offline_mode: bool = False

    def __post_init__(self) -> None:
        """Валидирует параметры конфигурации после инициализации."""
        self._validate()

    def _validate(self) -> None:
        """Проверяет корректность сетевых настроек и защитных барьеров."""
        if not self.hub_http_url or not (
            self.hub_http_url.startswith("http://") or self.hub_http_url.startswith("https://")
        ):
            raise ValueError(
                f"Некорректный hub_http_url: '{self.hub_http_url}'. Должен начинаться с http:// или https://"
            )

        if not (1 <= self.hub_stun_port <= 65535):
            raise ValueError(
                f"hub_stun_port должен быть в диапазоне 1..65535 (получено: {self.hub_stun_port})"
            )

        # Критический барьер безопасности VPS: диапазон 20000:50000 занят Hysteria 2
        if 20000 <= self.hub_stun_port <= 50000:
            raise ValueError(
                f"КРИТИЧЕСКИЙ КОНФЛИКТ ПОРТОВ: hub_stun_port={self.hub_stun_port} попадает в диапазон "
                f"20000:50000/udp, зарезервированный iptables для Hysteria 2 на VPS-сервере!"
            )

        if not (1 <= self.host_factorio_port <= 65535):
            raise ValueError(
                f"host_factorio_port должен быть в диапазоне 1..65535 (получено: {self.host_factorio_port})"
            )

        if not (1 <= self.guest_proxy_port <= 65535):
            raise ValueError(
                f"guest_proxy_port должен быть в диапазоне 1..65535 (получено: {self.guest_proxy_port})"
            )

        if self.host_factorio_port == self.guest_proxy_port:
            raise ValueError(
                f"host_factorio_port ({self.host_factorio_port}) не может совпадать с "
                f"guest_proxy_port ({self.guest_proxy_port}) во избежание конфликта сокетов."
            )

        if not self.cloud_save_name or not self.cloud_save_name.endswith(".zip"):
            raise ValueError(
                f"cloud_save_name должен оканчиваться на .zip (получено: '{self.cloud_save_name}')"
            )

    def is_configured(self) -> bool:
        """Проверяет, выполнена ли первичная настройка клиента (наличие токена и валидного адреса)."""
        has_token = bool(self.device_token and self.device_token.strip())
        has_url = bool(self.hub_http_url and self.hub_http_url.strip())
        return has_token and has_url

    def to_dict(self) -> dict[str, Any]:
        """Сериализует параметры конфигурации в словарь."""
        return asdict(self)

    def save_to_file(self, path: Path | str) -> None:
        """Атомарно сохраняет конфигурацию в JSON-файл.

        Args:
            path: Путь к файлу конфигурации (например: factorio_sync_config.json).
        """
        dest_path = Path(path).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            temp_file = dest_path.with_suffix(".tmp")
            content = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
            temp_file.write_text(content, encoding="utf-8")
            os.replace(temp_file, dest_path)
            logger.info(f"Конфигурация клиента сохранена в {dest_path}")
        except Exception as e:
            logger.error(f"Ошибка при сохранении конфигурации в {dest_path}: {e}")
            raise

    @classmethod
    def load_from_file(cls, path: Path | str) -> Optional[ClientConfig]:
        """Загружает конфигурацию из JSON-файла.

        Args:
            path: Путь к файлу конфигурации.

        Returns:
            Экземпляр ClientConfig, либо None если файл не найден или поврежден.
        """
        src_path = Path(path).resolve()
        if not src_path.is_file():
            return None

        try:
            content = src_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict):
                logger.warning(f"Файл {src_path} не содержит JSON-объект.")
                return None
            import dataclasses
            valid_fields = {f.name for f in dataclasses.fields(cls)}
            filtered_data = {k: v for k, v in data.items() if k in valid_fields}
            return cls(**filtered_data)
        except Exception as e:
            logger.warning(f"Не удалось загрузить конфигурацию из {src_path}: {e}")
            return None

    @classmethod
    def from_env(cls, **overrides: Any) -> ClientConfig:
        """Создает конфигурацию с учетом переменных окружения и явных переопределений."""
        try:
            http_url = os.getenv("FACTORIO_HUB_HTTP_URL", "http://127.0.0.1:8088")
            stun_host = os.getenv("FACTORIO_HUB_STUN_HOST", "127.0.0.1")

            raw_stun_port = os.getenv("FACTORIO_HUB_STUN_PORT")
            stun_port = int(raw_stun_port) if raw_stun_port else 14197

            cloud_save = os.getenv("FACTORIO_CLOUD_SAVE_NAME", "cloud_world.zip")

            raw_host_port = os.getenv("FACTORIO_HOST_PORT")
            host_port = int(raw_host_port) if raw_host_port else 34197

            raw_guest_port = os.getenv("FACTORIO_GUEST_PROXY_PORT")
            guest_port = int(raw_guest_port) if raw_guest_port else 34198

            player_name = os.getenv("FACTORIO_PLAYER_NAME", "")
            device_token = os.getenv("FACTORIO_DEVICE_TOKEN", "")
            device_id = os.getenv("FACTORIO_DEVICE_ID", "")

            config_data: dict[str, Any] = {
                "hub_http_url": http_url,
                "hub_stun_host": stun_host,
                "hub_stun_port": stun_port,
                "cloud_save_name": cloud_save,
                "host_factorio_port": host_port,
                "guest_proxy_port": guest_port,
                "player_name": player_name,
                "device_token": device_token,
                "device_id": device_id,
            }

            config_data.update({k: v for k, v in overrides.items() if v is not None})
            return cls(**config_data)
        except Exception as e:
            logger.error(f"Ошибка при загрузке ClientConfig из переменных окружения: {e}")
            raise
