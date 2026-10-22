"""Клиентский модуль Factorio P2P Cloud Sync & Coordinator.

Включает:
- Конфигурацию клиента (ClientConfig).
- Управление файловым окружением Factorio и защиту от Split-Brain (FactorioEnvironment).
- Системные уведомления и работу с буфером обмена (copy_to_clipboard, show_role_banner).
"""

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.ui_notifier import copy_to_clipboard, show_role_banner

__all__ = [
    "ClientConfig",
    "FactorioEnvironment",
    "copy_to_clipboard",
    "show_role_banner",
]
