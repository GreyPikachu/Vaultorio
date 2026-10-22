"""Клиентский модуль Factorio P2P Cloud Sync & Coordinator.

Включает:
- Конфигурацию клиента (ClientConfig).
- Управление файловым окружением Factorio и защиту от Split-Brain (FactorioEnvironment).
- HTTP-клиент связи с Сейфом (HubClient, compute_file_sha256).
- Системные уведомления и работу с буфером обмена (copy_to_clipboard, show_role_banner).
- Фоновые рабочие потоки Хоста (LobbyStartDetector, HeartbeatWorker, AutosaveWatcher,
  find_latest_session_save, flush_final_save_on_exit, HostWorkersManager).
"""

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.host_workers import (
    AutosaveWatcher,
    HeartbeatWorker,
    HostWorkersManager,
    LobbyStartDetector,
    find_latest_session_save,
    flush_final_save_on_exit,
)
from src.client.hub_client import HubClient, compute_file_sha256
from src.client.ui_notifier import copy_to_clipboard, show_role_banner

__all__ = [
    "AutosaveWatcher",
    "ClientConfig",
    "FactorioEnvironment",
    "HeartbeatWorker",
    "HostWorkersManager",
    "HubClient",
    "LobbyStartDetector",
    "compute_file_sha256",
    "copy_to_clipboard",
    "find_latest_session_save",
    "flush_final_save_on_exit",
    "show_role_banner",
]
