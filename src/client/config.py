"""Конфигурация клиента Factorio P2P Cloud Sync.

Содержит настройки подключения к VPS-хабу («Сейфу»), STUN-координатору
и локальным сетевым портам игры и прокси.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("factorio_client.config")


@dataclass
class ClientConfig:
    """Параметры подключения и сетевых сокетов клиента Factorio."""

    hub_http_url: str = "http://<YOUR_VPS_IP>:8088"
    hub_stun_host: str = "<YOUR_VPS_IP>"
    hub_stun_port: int = 14197
    cloud_save_name: str = "cloud_world.zip"
    host_factorio_port: int = 34197
    guest_proxy_port: int = 34198
    player_name: str = ""
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 30.0

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
                f"20000:50000/udp, зарезервированный iptables для Hysteria 2 на сервере <YOUR_VPS_IP>!"
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

    @classmethod
    def from_env(cls, **overrides: Any) -> ClientConfig:
        """Создает конфигурацию с учетом переменных окружения и явных переопределений."""
        try:
            http_url = os.getenv("FACTORIO_HUB_HTTP_URL", "http://<YOUR_VPS_IP>:8088")
            stun_host = os.getenv("FACTORIO_HUB_STUN_HOST", "<YOUR_VPS_IP>")
            
            raw_stun_port = os.getenv("FACTORIO_HUB_STUN_PORT")
            stun_port = int(raw_stun_port) if raw_stun_port else 14197

            cloud_save = os.getenv("FACTORIO_CLOUD_SAVE_NAME", "cloud_world.zip")

            raw_host_port = os.getenv("FACTORIO_HOST_PORT")
            host_port = int(raw_host_port) if raw_host_port else 34197

            raw_guest_port = os.getenv("FACTORIO_GUEST_PROXY_PORT")
            guest_port = int(raw_guest_port) if raw_guest_port else 34198

            player_name = os.getenv("FACTORIO_PLAYER_NAME", "")

            config_data: dict[str, Any] = {
                "hub_http_url": http_url,
                "hub_stun_host": stun_host,
                "hub_stun_port": stun_port,
                "cloud_save_name": cloud_save,
                "host_factorio_port": host_port,
                "guest_proxy_port": guest_port,
                "player_name": player_name,
            }

            config_data.update({k: v for k, v in overrides.items() if v is not None})
            return cls(**config_data)
        except Exception as e:
            logger.error(f"Ошибка при загрузке ClientConfig из переменных окружения: {e}")
            raise
