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
from src.client.chronicle_mod import ensure_chronicle_mod_installed
from src.client.chronicle_watcher import ChronicleWatcher
from src.client.factorio_env import FactorioEnvironment
from src.client.factorio_wrapper import main, setup_client_logging
from src.client.host_workers import (
    AutosaveWatcher,
    HeartbeatWorker,
    HostWorkersManager,
    LobbyStartDetector,
    find_latest_session_save,
    flush_final_save_on_exit,
)
from src.client.hub_client import HubClient, compute_file_sha256
from src.client.p2p_tunnel import (
    GuestP2PProxy,
    HostP2PBridge,
    decode_control_packet,
    encode_control_packet,
    encode_relay_packet,
    format_data_packet,
    is_control_packet,
    parse_data_packet,
)
from src.client.session_orchestrator import (
    GuestHandoverWatcher,
    RoleType,
    SessionOrchestrator,
)
from src.client.setup_gui import (
    FirstRunSetupWindow,
    publish_selected_world_to_server,
    run_first_time_setup_gui,
)
from src.client.ui_notifier import copy_to_clipboard, show_role_banner

__all__ = [
    "AutosaveWatcher",
    "ChronicleWatcher",
    "ClientConfig",
    "FactorioEnvironment",
    "FirstRunSetupWindow",
    "GuestHandoverWatcher",
    "GuestP2PProxy",
    "HeartbeatWorker",
    "HostP2PBridge",
    "HostWorkersManager",
    "HubClient",
    "LobbyStartDetector",
    "RoleType",
    "SessionOrchestrator",
    "compute_file_sha256",
    "copy_to_clipboard",
    "decode_control_packet",
    "encode_control_packet",
    "encode_relay_packet",
    "ensure_chronicle_mod_installed",
    "find_latest_session_save",
    "flush_final_save_on_exit",
    "format_data_packet",
    "is_control_packet",
    "main",
    "parse_data_packet",
    "publish_selected_world_to_server",
    "run_first_time_setup_gui",
    "setup_client_logging",
    "show_role_banner",
]
