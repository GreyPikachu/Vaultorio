"""Клиентский модуль P2P UDP Hole Punching и локального UDP-прокси Factorio.

Включает:
- Утилиты упаковки/распаковки сетевых пакетов P2P (JSON сигнализация, Direct P2P и Relay).
- HostP2PBridge: Серверный мост на ПК Хоста, мультиплексирующий Гостей к Factorio 34197.
- GuestP2PProxy: Локальный UDP-прокси на ПК Гостя (127.0.0.1:34198), соединяющийся с Хостом напрямую.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("factorio_hub.p2p_tunnel")

# Константы протокола
MAGIC_PREFIX: bytes = b"FP2P\x01"
TYPE_CONTROL: bytes = b"J"
TYPE_DATA: bytes = b"D"
TYPE_RELAY: bytes = b"R"

HEADER_CONTROL: bytes = MAGIC_PREFIX + TYPE_CONTROL  # 6 байт: b"FP2P\x01J"
HEADER_DATA: bytes = MAGIC_PREFIX + TYPE_DATA        # 6 байт: b"FP2P\x01D"
HEADER_RELAY: bytes = MAGIC_PREFIX + TYPE_RELAY      # 6 байт: b"FP2P\x01R"

MAX_UDP_PACKET: int = 65507


def get_primary_physical_interface_darwin() -> Optional[str]:
    """Определяет имя первичного физического сетевого интерфейса на macOS (например, en0).

    Исключает виртуальные интерфейсы VPN и прокси (utun*, ppp*, lo*, bridge*).
    """
    if sys.platform != "darwin":
        return None

    # 1. Попытка через scutil --nwi (официальная утилита конфигурации сети macOS)
    try:
        out = subprocess.check_output(["scutil", "--nwi"], text=True, timeout=1.0)
        for line in out.splitlines():
            stripped = line.strip()
            if ":" in stripped:
                name_part = stripped.split(":")[0].strip()
                if (name_part.startswith("en") or name_part.startswith("eth")) and not name_part.startswith("utun"):
                    try:
                        socket.if_nametoindex(name_part)
                        return name_part
                    except Exception:
                        pass
    except Exception:
        pass

    # 2. Попытка через netstat -rn -f inet (поиск шлюза по умолчанию)
    try:
        out = subprocess.check_output(["netstat", "-rn", "-f", "inet"], text=True, timeout=1.0)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "default" and "G" in parts[2]:
                netif = parts[3]
                if (netif.startswith("en") or netif.startswith("eth")) and not netif.startswith("utun"):
                    try:
                        socket.if_nametoindex(netif)
                        return netif
                    except Exception:
                        pass
    except Exception:
        pass

    # 3. Резервный перебор стандартных интерфейсов en0-en4
    for candidate in ("en0", "en1", "en2", "en3", "en4"):
        try:
            socket.if_nametoindex(candidate)
            return candidate
        except Exception:
            pass

    return None


def bind_socket_to_physical_interface(sock: socket.socket) -> Optional[str]:
    """Привязывает внешний UDP-сокет к физическому интерфейсу на macOS (IP_BOUND_IF = 25).

    Это гарантирует, что P2P-трафик и STUN-пакеты отправляются напрямую через физический
    сетевой адаптер (Wi-Fi/Ethernet) в обход виртуальных VPN-туннелей (Shadowrocket, utun*),
    снижая пинг с 800+ мс до 15–25 мс.

    На других ОС или при ошибке вызова оставляет стандартную маршрутизацию без исключений.
    """
    if sys.platform != "darwin":
        return None

    if os.getenv("FACTORIO_DISABLE_IP_BOUND_IF") == "1":
        return None

    try:
        iface = get_primary_physical_interface_darwin()
        if not iface:
            return None

        if_idx = socket.if_nametoindex(iface)
        # 25 = IP_BOUND_IF в macOS XNU sys/netinet/in.h
        sock.setsockopt(socket.IPPROTO_IP, 25, struct.pack("I", if_idx))
        logger.info(
            f"Внешний сокет успешно привязан к физическому интерфейсу {iface} (index {if_idx}) для прямого обхода VPN."
        )
        return iface
    except Exception as err:
        logger.debug(f"Не удалось привязать сокет через IP_BOUND_IF: {err}")
        return None


def detect_physical_lan_ip(target_host: str = "1.1.1.1") -> Optional[str]:
    """Определяет реальный локальный LAN IP-адрес на физическом интерфейсе в обход VPN."""
    try:
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            bind_socket_to_physical_interface(temp_sock)
            temp_sock.connect((target_host, 80))
            ip = temp_sock.getsockname()[0]
            if ip and not ip.startswith("127.") and not ip.startswith("198.18."):
                return ip
        finally:
            temp_sock.close()
    except Exception:
        pass
    return None


def _configure_udp_socket(sock: socket.socket) -> None:
    """Настраивает UDP-сокет: отключает блокировку и подавляет WSAECONNRESET на Windows."""
    sock.setblocking(False)
    # На Windows при получении ICMP Port Unreachable сокет может выбросить ConnectionResetError.
    # Константа SIO_UDP_CONNRESET подавляет это поведение.
    if hasattr(socket, "SIO_UDP_CONNRESET") and hasattr(sock, "ioctl"):
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except OSError:
            pass


def encode_control_packet(payload: Dict[str, Any]) -> bytes:
    """Кодирует словарь в управляющий JSON-пакет с заголовком b'FP2P\\x01J'."""
    raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return HEADER_CONTROL + raw_json


def decode_control_packet(data: bytes) -> Dict[str, Any] | None:
    """Декодирует управляющий JSON-пакет.

    Возвращает словарь данных или None, если пакет поврежден или не имеет сигнатуры.
    """
    if len(data) < len(HEADER_CONTROL) or not data.startswith(HEADER_CONTROL):
        return None
    try:
        payload = json.loads(data[len(HEADER_CONTROL):].decode("utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception as err:
        logger.debug(f"Ошибка декодирования управляющего пакета: {err}")
    return None


def is_control_packet(data: bytes) -> bool:
    """Проверяет, является ли датаграмма управляющим пакетом (сигнатура HEADER_CONTROL)."""
    return len(data) >= len(HEADER_CONTROL) and data.startswith(HEADER_CONTROL)


def encode_relay_packet(guest_id: str, payload: bytes) -> bytes:
    """Оборачивает полезную нагрузку в пакет релея (HEADER_RELAY + guest_id + payload)."""
    return format_data_packet(guest_id, payload, is_relay=True)


def format_data_packet(guest_id: str, payload: bytes, is_relay: bool = False) -> bytes:
    """Формирует игровой пакет данных Factorio.

    Формат: HEADER (6 байт) + guest_id (8 байт ASCII) + payload.
    Общий размер заголовка: 14 байт.
    """
    header = HEADER_RELAY if is_relay else HEADER_DATA
    guest_id_clean = (guest_id or "").strip()[:8].ljust(8).encode("ascii", errors="replace")
    return header + guest_id_clean + payload


def parse_data_packet(data: bytes) -> Tuple[str, bytes, bool] | None:
    """Разбирает игровой пакет данных.

    Возвращает кортеж (guest_id, raw_payload, is_relay) или None.
    """
    if len(data) < 14:
        return None

    if data.startswith(HEADER_DATA):
        is_relay = False
    elif data.startswith(HEADER_RELAY):
        is_relay = True
    else:
        return None

    try:
        guest_id = data[6:14].decode("ascii", errors="replace").strip()
        payload = data[14:]
        return (guest_id, payload, is_relay)
    except Exception as err:
        logger.debug(f"Ошибка разбора пакета данных: {err}")
        return None


@dataclass
class ConnectedGuestState:
    """Состояние подключения конкретного Гостя на стороне Хоста."""

    guest_id: str
    guest_name: str = ""
    public_endpoint: Tuple[str, int] | None = None
    local_endpoint: Tuple[str, int] | None = None
    active_endpoint: Tuple[str, int] | None = None
    is_direct_p2p: bool = False
    is_relay_active: bool = False
    last_seen: float = field(default_factory=time.time)
    last_punch_sent: float = 0.0
    last_direct_time: float = 0.0
    local_factorio_sock: socket.socket | None = None
    rtt_ms: Optional[float] = None


class HostP2PBridge:
    """Сетевой P2P-мост на стороне Хоста.

    - Открывает внешний сокет S_host, регистрируется на STUN-координаторе VPS.
    - При получении PEER_MATCH выполняет UDP Hole Punching с Гостем.
    - Для каждого подключившегося Гостя динамически создаёт выделенный локальный
      UDP-сокет L_guest_i (127.0.0.1:0 -> 127.0.0.1:34197), мультиплексируя игроков.
    """

    def __init__(
        self,
        session_token: str,
        stun_host: str = "127.0.0.1",
        stun_port: int = 14197,
        factorio_port: int = 34197,
        local_ip: str | None = None,
        poll_interval: float = 0.05,
        device_token: str = "",
    ) -> None:
        if not session_token:
            raise ValueError("session_token не может быть пустым")

        self.session_token = session_token
        self.stun_host = stun_host
        self.stun_port = int(stun_port)
        self.factorio_port = int(factorio_port)
        self.local_ip = local_ip
        self.poll_interval = max(0.005, float(poll_interval))
        self.device_token = device_token or ""

        self._s_host: socket.socket | None = None
        self._guests: Dict[str, ConnectedGuestState] = {}
        self._sock_to_guest_id: Dict[socket.socket, str] = {}
        self._lock = threading.Lock()

        self._is_running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._public_ip: str | None = None
        self._public_port: int | None = None
        self._last_stun_hello: float = 0.0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    @property
    def active_guests_count(self) -> int:
        with self._lock:
            return sum(1 for g in self._guests.values() if g.is_direct_p2p or g.is_relay_active)

    def get_guest_status(self, guest_id: str) -> Dict[str, Any] | None:
        """Возвращает текущий статус указанного Гостя."""
        with self._lock:
            g = self._guests.get(guest_id)
            if not g:
                return None
            return {
                "guest_id": g.guest_id,
                "guest_name": g.guest_name,
                "is_direct_p2p": g.is_direct_p2p,
                "is_relay_active": g.is_relay_active,
                "active_endpoint": g.active_endpoint,
                "last_seen_ago": round(time.time() - g.last_seen, 2),
                "rtt_ms": round(g.rtt_ms, 1) if g.rtt_ms is not None else None,
            }

    def get_guests_metrics(self, stale_timeout_sec: float = 15.0) -> List[Dict[str, Any]]:
        """Возвращает метрики активных гостей (RTT и режим соединения P2P/Relay).

        Фильтрует неактивных гостей, у которых time.time() - last_seen > stale_timeout_sec.
        Сортирует список по имени гостя для детерминированного порядка.
        """
        now = time.time()
        metrics: List[Dict[str, Any]] = []

        with self._lock:
            for guest in self._guests.values():
                if (now - guest.last_seen) <= stale_timeout_sec:
                    ping_val = round(guest.rtt_ms, 1) if guest.rtt_ms is not None else None
                    metrics.append({
                        "name": guest.guest_name or guest.guest_id,
                        "guest_id": guest.guest_id,
                        "ping_ms": ping_val,
                        "direct_p2p": bool(guest.is_direct_p2p),
                        "mode": "P2P" if guest.is_direct_p2p else "Relay",
                    })

        metrics.sort(key=lambda m: str(m.get("name", "")).lower())
        return metrics

    def start(self) -> None:
        """Запускает сокет Хоста и рабочий поток обработки сетевых пакетов."""
        with self._lock:
            if self._is_running:
                return
            self._is_running = True

        self._s_host = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _configure_udp_socket(self._s_host)
        bind_socket_to_physical_interface(self._s_host)
        self._s_host.bind(("0.0.0.0", 0))

        if not self.local_ip:
            self.local_ip = detect_physical_lan_ip(self.stun_host)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, name="HostP2PBridgeWorker", daemon=True)
        self._thread.start()

        logger.info(f"HostP2PBridge успешно запущен на порту {self._s_host.getsockname()[1]}")

    def stop(self) -> None:
        """Останавливает мост и закрывает все внешние и локальные сокеты."""
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        with self._lock:
            if self._s_host:
                try:
                    self._s_host.close()
                except Exception:
                    pass
                self._s_host = None

            for sock in self._sock_to_guest_id.keys():
                try:
                    sock.close()
                except Exception:
                    pass
            self._sock_to_guest_id.clear()
            self._guests.clear()

        logger.info("HostP2PBridge остановлен")

    def _send_stun_hello(self) -> None:
        """Отправляет HOST_HELLO на STUN-координатор VPS."""
        if not self._s_host:
            return
        payload = {
            "cmd": "HOST_HELLO",
            "session_token": self.session_token,
            "local_ip": self.local_ip,
            "local_port": self.factorio_port,
            "device_token": self.device_token,
        }
        try:
            packet = encode_control_packet(payload)
            self._s_host.sendto(packet, (self.stun_host, self.stun_port))
            self._last_stun_hello = time.time()
            logger.debug(f"Отправлен HOST_HELLO на {self.stun_host}:{self.stun_port}")
        except Exception as err:
            logger.warning(f"Ошибка отправки HOST_HELLO: {err}")

    def _worker_loop(self) -> None:
        """Главный цикл мультиплексирования пакетов через select.select."""
        self._send_stun_hello()

        while not self._stop_event.is_set():
            now = time.time()

            # 1. Периодический keepalive к STUN-координатору каждые 15 сек
            if now - self._last_stun_hello >= 15.0:
                self._send_stun_hello()

            # 2. Периодические пробивочные пакеты PUNCH_SYN для ожидающих Гостей
            self._send_pending_punches(now)

            # 3. Собираем список сокетов для select
            with self._lock:
                if not self._s_host:
                    break
                read_sockets: List[socket.socket] = [self._s_host]
                read_sockets.extend(self._sock_to_guest_id.keys())

            try:
                readable, _, _ = select.select(read_sockets, [], [], self.poll_interval)
            except (ValueError, OSError):
                break

            for sock in readable:
                if sock is self._s_host:
                    self._handle_external_socket_read()
                else:
                    self._handle_local_factorio_read(sock)

    def _send_pending_punches(self, now: float) -> None:
        """Шлёт серию встречных PUNCH_SYN ожидающим гостям."""
        with self._lock:
            if not self._s_host:
                return
            for guest in list(self._guests.values()):
                # Если прямой канал ещё не установлен, продолжаем пробитие (0.2с вначале, 1.0с при активном релее)
                punch_interval = 0.2 if not guest.is_relay_active else 1.0
                if not guest.is_direct_p2p and (now - guest.last_punch_sent >= punch_interval):
                    syn_pkt = encode_control_packet({
                        "cmd": "PUNCH_SYN",
                        "sender_role": "host",
                        "guest_id": guest.guest_id,
                        "ts": now,
                    })
                    if guest.public_endpoint:
                        try:
                            self._s_host.sendto(syn_pkt, guest.public_endpoint)
                        except Exception:
                            pass
                    if guest.local_endpoint and guest.local_endpoint != guest.public_endpoint:
                        try:
                            self._s_host.sendto(syn_pkt, guest.local_endpoint)
                        except Exception:
                            pass
                    guest.last_punch_sent = now

    def _handle_external_socket_read(self) -> None:
        """Обрабатывает входящие датаграммы на внешнем сокете Хоста (S_host)."""
        if not self._s_host:
            return
        try:
            data, addr = self._s_host.recvfrom(MAX_UDP_PACKET)
        except (BlockingIOError, ConnectionResetError, OSError):
            return

        # А. Управляющий пакет JSON
        if data.startswith(HEADER_CONTROL):
            ctrl = decode_control_packet(data)
            if not ctrl:
                return
            cmd = ctrl.get("cmd")

            if cmd == "HOST_ACK":
                self._public_ip = ctrl.get("public_ip")
                self._public_port = ctrl.get("public_port")
                logger.info(f"STUN подтвердил адрес Хоста: {self._public_ip}:{self._public_port}")

            elif cmd == "PEER_MATCH":
                self._handle_peer_match(ctrl)

            elif cmd == "PUNCH_SYN":
                self._handle_punch_syn(ctrl, addr)

            elif cmd == "PUNCH_ACK":
                self._handle_punch_ack(ctrl, addr)

            elif cmd == "P2P_KEEPALIVE":
                self._handle_p2p_keepalive(ctrl, addr, via_relay=False)

        # Б. Пакет игровых данных (Direct или Relay)
        elif data.startswith(HEADER_DATA) or data.startswith(HEADER_RELAY):
            parsed = parse_data_packet(data)
            if not parsed:
                return
            guest_id, raw_payload, is_relay = parsed

            # Перехват управляющих пакетов внутри туннеля (никогда не пересылать в Factorio)
            if is_control_packet(raw_payload):
                inner_ctrl = decode_control_packet(raw_payload)
                if inner_ctrl:
                    inner_cmd = inner_ctrl.get("cmd")
                    if inner_cmd == "P2P_KEEPALIVE":
                        self._handle_p2p_keepalive(inner_ctrl, addr, via_relay=True, relay_guest_id=guest_id)
                        return
                    elif inner_cmd == "PUNCH_SYN":
                        self._handle_punch_syn(inner_ctrl, addr)
                        return
                    elif inner_cmd == "PUNCH_ACK":
                        self._handle_punch_ack(inner_ctrl, addr)
                        return
                return

            self._route_game_data_to_factorio(guest_id, raw_payload, is_relay, addr)

    def _handle_p2p_keepalive(
        self,
        ctrl: Dict[str, Any],
        addr: Tuple[str, int],
        via_relay: bool = False,
        relay_guest_id: str = "",
    ) -> None:
        """Обрабатывает P2P_KEEPALIVE от Гостя, сохраняет ping_M и отправляет P2P_KEEPALIVE_ACK."""
        guest_id = str(ctrl.get("guest_id") or relay_guest_id).strip()
        if not guest_id:
            return

        now_ts = time.time()
        guest_name = str(ctrl.get("guest_name") or "")
        ping_m = ctrl.get("ping_M")

        is_from_stun = (addr[0] == self.stun_host and addr[1] == self.stun_port)
        is_relay_packet = via_relay or is_from_stun

        with self._lock:
            if guest_id in self._guests:
                guest = self._guests[guest_id]
                if guest_name:
                    guest.guest_name = guest_name
            else:
                guest = ConnectedGuestState(
                    guest_id=guest_id,
                    guest_name=guest_name or guest_id,
                )
                self._guests[guest_id] = guest

            guest.last_seen = now_ts

            if is_relay_packet:
                # Если прямой P2P уже активен и недавно подтвержден (< 5 сек), не сбрасываем
                if not guest.is_direct_p2p or (now_ts - guest.last_direct_time > 5.0):
                    guest.is_direct_p2p = False
                    guest.is_relay_active = True
                    guest.active_endpoint = None
            else:
                guest.is_direct_p2p = True
                guest.is_relay_active = False
                guest.active_endpoint = addr
                guest.last_direct_time = now_ts

            if isinstance(ping_m, (int, float)) and 0.0 <= float(ping_m) <= 9999.0:
                guest.rtt_ms = round(float(ping_m), 1)

        # Отправляем ответ P2P_KEEPALIVE_ACK
        if self._s_host:
            echo_ts = ctrl.get("ts")
            ack_payload = {
                "cmd": "P2P_KEEPALIVE_ACK",
                "guest_id": guest_id,
                "echo_ts": echo_ts,
            }
            ack_bytes = encode_control_packet(ack_payload)
            try:
                if is_relay_packet:
                    relay_ack_bytes = format_data_packet(guest_id, ack_bytes, is_relay=True)
                    self._s_host.sendto(relay_ack_bytes, (self.stun_host, self.stun_port))
                else:
                    self._s_host.sendto(ack_bytes, addr)
            except Exception as err:
                logger.debug(f"Ошибка отправки P2P_KEEPALIVE_ACK для {guest_id}: {err}")

    def _handle_peer_match(self, payload: Dict[str, Any]) -> None:
        """Обрабатывает PEER_MATCH от STUN-координатора."""
        guest_id = str(payload.get("guest_id", "")).strip()
        if not guest_id:
            return

        guest_name = str(payload.get("guest_name", "Guest"))
        g_pub = payload.get("guest_public")
        g_loc = payload.get("guest_local")

        pub_ep = (g_pub[0], int(g_pub[1])) if g_pub and len(g_pub) == 2 and g_pub[0] else None
        loc_ep = (g_loc[0], int(g_loc[1])) if g_loc and len(g_loc) == 2 and g_loc[0] else None

        with self._lock:
            if guest_id not in self._guests:
                self._guests[guest_id] = ConnectedGuestState(
                    guest_id=guest_id,
                    guest_name=guest_name,
                    public_endpoint=pub_ep,
                    local_endpoint=loc_ep,
                    is_relay_active=True,
                )
            else:
                g = self._guests[guest_id]
                # При смене координат или повторном матчинге сбрасываем старый прямой канал
                if g.public_endpoint != pub_ep or g.local_endpoint != loc_ep or not g.is_direct_p2p:
                    logger.info(
                        f"PEER_MATCH: обновление координат гостя '{guest_name}' ({guest_id}): "
                        f"pub={pub_ep}, loc={loc_ep}. Сброс прямого канала для повторного рукопожатия."
                    )
                    g.active_endpoint = None
                    g.is_direct_p2p = False
                    g.is_relay_active = True
                    g.last_punch_sent = 0.0

                g.public_endpoint = pub_ep
                g.local_endpoint = loc_ep
                g.guest_name = guest_name

        logger.info(f"PEER_MATCH для гостя '{guest_name}' ({guest_id}). Публичный: {pub_ep}, Локальный: {loc_ep}")

    def _handle_punch_syn(self, payload: Dict[str, Any], addr: Tuple[str, int]) -> None:
        """Отвечает PUNCH_ACK на встречный пробивочный пакет Гостя."""
        guest_id = str(payload.get("guest_id", "")).strip()
        now = time.time()

        with self._lock:
            if guest_id in self._guests:
                g = self._guests[guest_id]
                g.active_endpoint = addr
                g.is_direct_p2p = True
                g.is_relay_active = False
                g.last_seen = now
                g.last_direct_time = now
            else:
                self._guests[guest_id] = ConnectedGuestState(
                    guest_id=guest_id,
                    active_endpoint=addr,
                    is_direct_p2p=True,
                    is_relay_active=False,
                    last_seen=now,
                    last_direct_time=now,
                )

        if self._s_host:
            ack_pkt = encode_control_packet({
                "cmd": "PUNCH_ACK",
                "sender_role": "host",
                "guest_id": guest_id,
                "ts": payload.get("ts", now),
            })
            try:
                self._s_host.sendto(ack_pkt, addr)
                logger.info(f"PUNCH_SYN получен от {addr}. Отправлен PUNCH_ACK. Прямой канал зафиксирован!")
            except Exception as err:
                logger.debug(f"Ошибка отправки PUNCH_ACK: {err}")

    def _handle_punch_ack(self, payload: Dict[str, Any], addr: Tuple[str, int]) -> None:
        """Фиксирует успешный прямой канал при получении PUNCH_ACK."""
        guest_id = str(payload.get("guest_id", "")).strip()
        now = time.time()
        with self._lock:
            if guest_id in self._guests:
                g = self._guests[guest_id]
                g.active_endpoint = addr
                g.is_direct_p2p = True
                g.is_relay_active = False
                g.last_seen = now
                g.last_direct_time = now
                logger.info(f"PUNCH_ACK получен от {addr}. Прямой P2P-канал с гостем {guest_id} активен!")

    def _get_or_create_guest_socket(self, guest: ConnectedGuestState) -> socket.socket | None:
        """Возвращает или создаёт выделенный локальный сокет L_guest_i для данного Гостя."""
        if guest.local_factorio_sock is not None:
            return guest.local_factorio_sock

        try:
            local_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _configure_udp_socket(local_sock)
            local_sock.bind(("127.0.0.1", 0))

            guest.local_factorio_sock = local_sock
            self._sock_to_guest_id[local_sock] = guest.guest_id

            logger.info(
                f"Создан локальный сокет {local_sock.getsockname()[1]} для мультиплексирования гостя {guest.guest_id}"
            )
            return local_sock
        except Exception as err:
            logger.error(f"Не удалось создать локальный сокет для гостя {guest.guest_id}: {err}")
            return None

    def _route_game_data_to_factorio(
        self,
        guest_id: str,
        raw_payload: bytes,
        is_relay: bool,
        addr: Tuple[str, int],
    ) -> None:
        """Пересылает датаграмму от Гостя в локальную Factorio через сокет L_guest_i."""
        now = time.time()
        with self._lock:
            if guest_id not in self._guests:
                self._guests[guest_id] = ConnectedGuestState(
                    guest_id=guest_id,
                    active_endpoint=addr if not is_relay else None,
                    is_direct_p2p=not is_relay,
                    is_relay_active=is_relay,
                    last_seen=now,
                    last_direct_time=now if not is_relay else 0.0,
                )
            guest = self._guests[guest_id]
            guest.last_seen = now
            if is_relay:
                # Если прямой P2P уже активен и недавно подтвержден (< 5 сек назад),
                # НЕ сбрасываем прямой канал из-за остаточного пакета релея из буфера VPS!
                if not guest.is_direct_p2p or (now - guest.last_direct_time > 5.0):
                    guest.is_relay_active = True
                    guest.is_direct_p2p = False
                    guest.active_endpoint = None
            else:
                guest.active_endpoint = addr
                guest.is_direct_p2p = True
                guest.is_relay_active = False
                guest.last_direct_time = now

            local_sock = self._get_or_create_guest_socket(guest)

        if local_sock:
            try:
                local_sock.sendto(raw_payload, ("127.0.0.1", self.factorio_port))
            except Exception as err:
                logger.debug(f"Ошибка пересылки датаграммы в Factorio (34197): {err}")

    def _handle_local_factorio_read(self, sock: socket.socket) -> None:
        """Читает ответ от локальной Factorio и отправляет его обратно Гостю."""
        with self._lock:
            guest_id = self._sock_to_guest_id.get(sock)
            if not guest_id or guest_id not in self._guests:
                return
            guest = self._guests[guest_id]

        try:
            data, _ = sock.recvfrom(MAX_UDP_PACKET)
        except (BlockingIOError, ConnectionResetError, OSError):
            return

        if not self._s_host:
            return

        # Если доступен прямой P2P-канал
        if guest.is_direct_p2p and guest.active_endpoint:
            pkt = format_data_packet(guest_id, data, is_relay=False)
            try:
                self._s_host.sendto(pkt, guest.active_endpoint)
            except Exception as err:
                logger.debug(f"Ошибка прямой отправки гостю {guest_id}: {err}")

        # Иначе пересылаем через резервный релей на VPS
        elif guest.is_relay_active:
            pkt = format_data_packet(guest_id, data, is_relay=True)
            try:
                self._s_host.sendto(pkt, (self.stun_host, self.stun_port))
            except Exception as err:
                logger.debug(f"Ошибка отправки через релей для {guest_id}: {err}")


class GuestP2PProxy:
    """Локальный UDP-прокси на стороне Гостя.

    - Открывает локальный слушающий сокет L_proxy на 127.0.0.1:34198 (для Factorio Гостя).
    - Открывает внешний сокет S_guest, отправляет GUEST_HELLO и пробивает NAT к Хосту.
    - Пересылает трафик напрямую (15–25 мс) либо через STUN-релей при жестком CGNAT.
    """

    def __init__(
        self,
        guest_name: str,
        guest_id: str | None = None,
        stun_host: str = "127.0.0.1",
        stun_port: int = 14197,
        proxy_port: int = 34198,
        local_ip: str | None = None,
        punch_timeout_sec: float = 5.0,
        poll_interval: float = 0.05,
        device_token: str = "",
    ) -> None:
        self.guest_name = str(guest_name).strip() or "Guest"
        self.guest_id = str(guest_id).strip()[:8] if guest_id else secrets.token_hex(4)
        self.stun_host = stun_host
        self.stun_port = int(stun_port)
        self.proxy_port = int(proxy_port)
        self.local_ip = local_ip
        self.punch_timeout_sec = max(0.05, float(punch_timeout_sec))
        self.poll_interval = max(0.005, float(poll_interval))
        self.device_token = device_token or ""

        self._l_proxy: socket.socket | None = None
        self._s_guest: socket.socket | None = None
        self._lock = threading.Lock()

        self._is_running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._is_connected = False
        self._is_direct_p2p = False
        self._is_relay_active = False
        self._rtt_ms: float | None = None

        self._host_public: Tuple[str, int] | None = None
        self._host_local: Tuple[str, int] | None = None
        self._host_active_endpoint: Tuple[str, int] | None = None
        self._factorio_client_addr: Tuple[str, int] | None = None

        self._peer_match_received = False
        self._punch_start_time: float = 0.0
        self._last_punch_sent: float = 0.0
        self._last_stun_hello: float = 0.0
        self._last_keepalive: float = 0.0
        self._last_incoming_packet_time: float = 0.0
        self._last_factorio_packet_time: float = 0.0
        self._last_reconnect_time: float = 0.0
        self._last_direct_p2p_time: float = 0.0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._is_connected

    @property
    def is_direct_p2p(self) -> bool:
        with self._lock:
            return self._is_direct_p2p

    @property
    def is_relay_active(self) -> bool:
        with self._lock:
            return self._is_relay_active

    @property
    def rtt_ms(self) -> float | None:
        with self._lock:
            return self._rtt_ms

    def start(self) -> None:
        """Запускает сокеты прокси и рабочий поток передачи данных."""
        with self._lock:
            if self._is_running:
                return
            self._is_running = True

        # Локальный сокет, слушающий 127.0.0.1:34198
        self._l_proxy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _configure_udp_socket(self._l_proxy)
        self._l_proxy.bind(("127.0.0.1", self.proxy_port))

        # Внешний сокет для связи с VPS и Хостом (обход VPN через физический адаптер)
        self._s_guest = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _configure_udp_socket(self._s_guest)
        bind_socket_to_physical_interface(self._s_guest)
        self._s_guest.bind(("0.0.0.0", 0))

        if not self.local_ip:
            self.local_ip = detect_physical_lan_ip(self.stun_host)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, name="GuestP2PProxyWorker", daemon=True)
        self._thread.start()

        logger.info(
            f"GuestP2PProxy запущен (слушает 127.0.0.1:{self.proxy_port}, внешний порт: {self._s_guest.getsockname()[1]})"
        )

    def stop(self) -> None:
        """Останавливает прокси и закрывает все сокеты."""
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        with self._lock:
            if self._l_proxy:
                try:
                    self._l_proxy.close()
                except Exception:
                    pass
                self._l_proxy = None

            if self._s_guest:
                try:
                    self._s_guest.close()
                except Exception:
                    pass
                self._s_guest = None

        logger.info("GuestP2PProxy остановлен")

    def _send_guest_hello(self) -> None:
        """Отправляет GUEST_HELLO на STUN-координатор VPS."""
        if not self._s_guest:
            return
        payload = {
            "cmd": "GUEST_HELLO",
            "guest_id": self.guest_id,
            "guest_name": self.guest_name,
            "local_ip": self.local_ip,
            "local_port": self.proxy_port,
            "device_token": self.device_token,
        }
        try:
            packet = encode_control_packet(payload)
            self._s_guest.sendto(packet, (self.stun_host, self.stun_port))
            self._last_stun_hello = time.time()
            logger.debug(f"Отправлен GUEST_HELLO на {self.stun_host}:{self.stun_port}")
        except Exception as err:
            logger.warning(f"Ошибка отправки GUEST_HELLO: {err}")

    def _reconnect_external_network(self, now: float, reason: str = "") -> None:
        """Пересоздает внешний сокет и запрашивает новое рукопожатие на STUN при смене сети."""
        with self._lock:
            logger.info(
                f"GuestP2PProxy: обнаружена смена сети или потеря связи ({reason}). "
                "Автоматическое переподключение внешнего сокета..."
            )
            if self._s_guest:
                try:
                    self._s_guest.close()
                except Exception:
                    pass
                self._s_guest = None

            try:
                self._s_guest = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _configure_udp_socket(self._s_guest)
                bind_socket_to_physical_interface(self._s_guest)
                self._s_guest.bind(("0.0.0.0", 0))
            except Exception as err:
                logger.error(f"Не удалось пересоздать внешний сокет: {err}")
                return

            self._peer_match_received = False
            self._is_direct_p2p = False
            self._is_relay_active = True
            self._host_active_endpoint = None
            self._last_incoming_packet_time = 0.0
            self._last_direct_p2p_time = 0.0
            self._last_reconnect_time = now
            self._punch_start_time = now
            self.local_ip = detect_physical_lan_ip(self.stun_host)

        self._send_guest_hello()

    def _worker_loop(self) -> None:
        """Главный цикл мультиплексирования прокси через select.select."""
        self._send_guest_hello()

        while not self._stop_event.is_set():
            now = time.time()

            # До получения PEER_MATCH отправляем GUEST_HELLO каждые 3 сек
            if not self._peer_match_received:
                if now - self._last_stun_hello >= 3.0:
                    self._send_guest_hello()
            else:
                # Автоматическая детекция смены сети / потери пакетов:
                # Если Factorio пытается отправлять данные, но от Хоста нет входящих пакетов:
                # - Если на сокете ещё не получено ни одного входящего пакета, даём 25 сек на рукопожатие, NAT-пробитие и релей.
                # - Если связь уже была, но пропала, ждём 20 сек тишины от Хоста (чтобы автосейв карты 48 МБ не вызывал ложный сброс).
                silence_timeout = 20.0 if self._last_incoming_packet_time > 0 else 25.0
                time_since_last_incoming = (
                    now - self._last_incoming_packet_time
                    if self._last_incoming_packet_time > 0
                    else now - self._last_reconnect_time
                )

                if (
                    self._last_factorio_packet_time > 0
                    and (now - self._last_factorio_packet_time < 3.0)
                    and (time_since_last_incoming >= silence_timeout)
                    and (now - self._last_reconnect_time >= 15.0)
                ):
                    self._reconnect_external_network(now, reason="no_response_from_host")

                # После получения PEER_MATCH шлём PUNCH_SYN
                if not self.is_direct_p2p:
                    # Проверяем таймаут перехода на резервный релей
                    if (now - self._punch_start_time >= self.punch_timeout_sec) and not self.is_relay_active:
                        with self._lock:
                            self._is_relay_active = True
                            self._is_connected = True
                        logger.warning(
                            "Прямое пробитие NAT не ответило вовремя. Переключаемся на резервный STUN-релей через VPS."
                        )

                    # Шлём пробивочные пакеты: 0.2с до подключения релея, 1.0с во время релея для бесшовного возврата в P2P
                    punch_interval = 0.2 if not self.is_relay_active else 1.0
                    if now - self._last_punch_sent >= punch_interval:
                        self._send_punch_syn(now)

                # Keepalive отправляется каждые 3 сек как в режиме прямого P2P, так и при Relay
                if (self.is_direct_p2p or self.is_relay_active) and (now - self._last_keepalive >= 3.0):
                    self._send_keepalive(now)

            with self._lock:
                if not self._l_proxy or not self._s_guest:
                    break
                read_sockets = [self._l_proxy, self._s_guest]

            try:
                readable, _, _ = select.select(read_sockets, [], [], self.poll_interval)
            except (ValueError, OSError):
                break

            for sock in readable:
                if sock is self._l_proxy:
                    self._handle_local_factorio_read()
                elif sock is self._s_guest:
                    self._handle_external_socket_read()

    def _send_punch_syn(self, now: float) -> None:
        """Отправляет PUNCH_SYN на публичный и локальный адреса Хоста."""
        if not self._s_guest:
            return
        pkt = encode_control_packet({
            "cmd": "PUNCH_SYN",
            "sender_role": "guest",
            "guest_id": self.guest_id,
            "ts": now,
        })
        if self._host_public:
            try:
                self._s_guest.sendto(pkt, self._host_public)
            except Exception:
                pass
        if self._host_local and self._host_local != self._host_public:
            try:
                self._s_guest.sendto(pkt, self._host_local)
            except Exception:
                pass
        self._last_punch_sent = now

    def _send_keepalive(self, now: float | None = None) -> None:
        """Шлёт keepalive-пакет Хосту (напрямую или через релей) для поддержания NAT и замера RTT."""
        if not self._s_guest:
            return
        if now is None:
            now = time.time()

        with self._lock:
            current_rtt = round(self._rtt_ms, 1) if self._rtt_ms is not None else None

        pkt = encode_control_packet({
            "cmd": "P2P_KEEPALIVE",
            "sender_role": "guest",
            "guest_id": self.guest_id,
            "guest_name": self.guest_name,
            "ts": now,
            "ping_M": current_rtt,
        })

        try:
            # Если активен прямой P2P-канал
            if self.is_direct_p2p and self._host_active_endpoint:
                self._s_guest.sendto(pkt, self._host_active_endpoint)
                self._last_keepalive = now
            # Если активен Relay
            elif self.is_relay_active:
                relay_pkt = format_data_packet(self.guest_id, pkt, is_relay=True)
                self._s_guest.sendto(relay_pkt, (self.stun_host, self.stun_port))
                self._last_keepalive = now
        except Exception as err:
            logger.debug(f"Ошибка отправки P2P_KEEPALIVE: {err}")

    def _handle_local_factorio_read(self) -> None:
        """Читает дату от локальной Factorio (127.0.0.1:34198) и пересылает Хосту."""
        if not self._l_proxy or not self._s_guest:
            return

        try:
            data, addr = self._l_proxy.recvfrom(MAX_UDP_PACKET)
        except (BlockingIOError, ConnectionResetError, OSError):
            return

        self._factorio_client_addr = addr
        self._last_factorio_packet_time = time.time()

        # Если установлен прямой P2P-канал
        if self.is_direct_p2p and self._host_active_endpoint:
            pkt = format_data_packet(self.guest_id, data, is_relay=False)
            try:
                self._s_guest.sendto(pkt, self._host_active_endpoint)
            except OSError as err:
                logger.debug(f"Ошибка прямой отправки датаграммы Хосту: {err}")
                self._reconnect_external_network(time.time(), reason=f"socket_send_error:{err}")

        # Иначе отправляем через релей на VPS
        else:
            pkt = format_data_packet(self.guest_id, data, is_relay=True)
            try:
                self._s_guest.sendto(pkt, (self.stun_host, self.stun_port))
            except OSError as err:
                logger.debug(f"Ошибка отправки через релей на VPS: {err}")
                self._reconnect_external_network(time.time(), reason=f"relay_send_error:{err}")

    def _handle_external_socket_read(self) -> None:
        """Читает входящие пакеты из интернета (от Хоста напрямую или от STUN-релея)."""
        if not self._s_guest:
            return

        try:
            data, addr = self._s_guest.recvfrom(MAX_UDP_PACKET)
        except (BlockingIOError, ConnectionResetError, OSError):
            return

        with self._lock:
            self._last_incoming_packet_time = time.time()

        # А. Управляющий пакет JSON
        if data.startswith(HEADER_CONTROL):
            ctrl = decode_control_packet(data)
            if not ctrl:
                return
            cmd = ctrl.get("cmd")

            if cmd == "PEER_MATCH":
                self._handle_peer_match(ctrl)

            elif cmd == "PUNCH_SYN":
                self._handle_punch_syn(ctrl, addr)

            elif cmd == "PUNCH_ACK":
                self._handle_punch_ack(ctrl, addr)

            elif cmd == "P2P_KEEPALIVE_ACK":
                self._handle_keepalive_ack(ctrl)

        # Б. Игровой пакет данных
        elif data.startswith(HEADER_DATA) or data.startswith(HEADER_RELAY):
            parsed = parse_data_packet(data)
            if not parsed:
                return
            guest_id, raw_payload, is_relay = parsed
            now = time.time()

            # Перехват управляющих пакетов внутри туннеля (никогда не пересылать в Factorio)
            if is_control_packet(raw_payload):
                inner_ctrl = decode_control_packet(raw_payload)
                if inner_ctrl:
                    inner_cmd = inner_ctrl.get("cmd")
                    if inner_cmd == "P2P_KEEPALIVE_ACK":
                        self._handle_keepalive_ack(inner_ctrl)
                        return
                    elif inner_cmd == "PUNCH_ACK":
                        self._handle_punch_ack(inner_ctrl, addr)
                        return
                return

            # Обновляем состояние канала
            with self._lock:
                self._is_connected = True
                self._last_incoming_packet_time = now
                if is_relay:
                    # Если прямой P2P уже активен и недавно подтвержден (< 5 сек назад),
                    # НЕ сбрасываем прямой канал из-за остаточного пакета релея из буфера VPS!
                    if not self._is_direct_p2p or (now - self._last_direct_p2p_time > 5.0):
                        self._is_relay_active = True
                        self._is_direct_p2p = False
                        self._host_active_endpoint = None
                else:
                    self._is_direct_p2p = True
                    self._is_relay_active = False
                    self._host_active_endpoint = addr
                    self._last_direct_p2p_time = now

            # Пересылаем чистую датаграмму локальному клиенту Factorio
            if self._l_proxy and self._factorio_client_addr:
                try:
                    self._l_proxy.sendto(raw_payload, self._factorio_client_addr)
                except Exception as err:
                    logger.debug(f"Ошибка отправки данных в локальную Factorio: {err}")

    def _handle_peer_match(self, payload: Dict[str, Any]) -> None:
        """Обрабатывает PEER_MATCH от STUN-координатора."""
        h_pub = payload.get("host_public")
        h_loc = payload.get("host_local")

        pub_ep = (h_pub[0], int(h_pub[1])) if h_pub and len(h_pub) == 2 and h_pub[0] else None
        loc_ep = (h_loc[0], int(h_loc[1])) if h_loc and len(h_loc) == 2 and h_loc[0] else None

        with self._lock:
            self._host_public = pub_ep
            self._host_local = loc_ep
            self._peer_match_received = True
            self._punch_start_time = time.time()

        logger.info(f"PEER_MATCH получен! Координаты Хоста: публичный {pub_ep}, локальный {loc_ep}")
        self._send_punch_syn(time.time())

    def _handle_punch_syn(self, payload: Dict[str, Any], addr: Tuple[str, int]) -> None:
        """Отвечает PUNCH_ACK на встречный пробивочный пакет от Хоста."""
        now = time.time()
        with self._lock:
            self._host_active_endpoint = addr
            self._is_connected = True
            self._is_direct_p2p = True
            self._is_relay_active = False
            self._last_direct_p2p_time = now
            self._last_incoming_packet_time = now

        if self._s_guest:
            ack_pkt = encode_control_packet({
                "cmd": "PUNCH_ACK",
                "sender_role": "guest",
                "guest_id": self.guest_id,
                "ts": payload.get("ts", now),
            })
            try:
                self._s_guest.sendto(ack_pkt, addr)
            except Exception:
                pass

    def _handle_punch_ack(self, payload: Dict[str, Any], addr: Tuple[str, int]) -> None:
        """Подтверждает установление прямого P2P-канала при получении PUNCH_ACK от Хоста."""
        syn_ts = payload.get("ts", 0)
        now = time.time()
        rtt: float | None = None
        if isinstance(syn_ts, (int, float)) and syn_ts > 0:
            raw_rtt = (now - float(syn_ts)) * 1000.0
            if 0.05 <= raw_rtt <= 9999.0:
                rtt = round(raw_rtt, 1)

        with self._lock:
            self._host_active_endpoint = addr
            self._is_connected = True
            self._is_direct_p2p = True
            self._is_relay_active = False
            if rtt is not None:
                if self._rtt_ms is None:
                    self._rtt_ms = rtt
                else:
                    self._rtt_ms = round(0.7 * self._rtt_ms + 0.3 * rtt, 1)
            self._last_direct_p2p_time = now
            self._last_incoming_packet_time = now

        logger.info(f"Прямой P2P-туннель с Хостом установлен! Адрес: {addr}, Пинг: {self._rtt_ms} мс")

    def _handle_keepalive_ack(self, payload: Dict[str, Any]) -> None:
        """Обрабатывает P2P_KEEPALIVE_ACK от Хоста, вычисляет RTT по локальным часам и применяет EMA."""
        echo_ts = payload.get("echo_ts")
        if not isinstance(echo_ts, (int, float)):
            return

        now = time.time()
        raw_rtt = (now - float(echo_ts)) * 1000.0

        # Фильтр аномалий (NTP-скачки, подвисание ОС, отрицательный RTT)
        if 0.05 <= raw_rtt <= 9999.0:
            with self._lock:
                if self._rtt_ms is None:
                    self._rtt_ms = round(raw_rtt, 1)
                else:
                    smoothed = 0.7 * self._rtt_ms + 0.3 * raw_rtt
                    self._rtt_ms = round(smoothed, 1)
            logger.debug(f"GuestP2PProxy: RTT={raw_rtt:.1f} мс, EMA RTT={self._rtt_ms} мс")
        else:
            logger.debug(f"GuestP2PProxy: отброшено аномальное значение RTT={raw_rtt:.1f} мс")
