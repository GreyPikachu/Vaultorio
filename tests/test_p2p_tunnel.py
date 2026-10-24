"""Тесты для модуля P2P UDP Hole Punching и локального UDP-прокси (src/client/p2p_tunnel.py).

Покрывает чек-лист проверок T9.1 - T9.6:
- T9.1: Кодирование и декодирование сетевых пакетов P2P (JSON, Data, Relay).
- T9.2: HostP2PBridge: Регистрация на STUN и обработка PEER_MATCH.
- T9.3: GuestP2PProxy: STUN-хэндшейк и прямое пробитие NAT.
- T9.4: Сквозная передача данных в прямом P2P (двусторонний loopback без искажений).
- T9.5: Мультиплексирование нескольких Гостей на Хосте на разные порты.
- T9.6: Fallback Relay при блокировке прямого канала и корректная очистка сокетов.
"""

import hashlib
import json
import os
import secrets
import socket
import threading
import time
from typing import Any, Dict, List, Tuple

import pytest

from src.client.p2p_tunnel import (
    HEADER_CONTROL,
    HEADER_DATA,
    HEADER_RELAY,
    GuestP2PProxy,
    HostP2PBridge,
    decode_control_packet,
    encode_control_packet,
    format_data_packet,
    parse_data_packet,
)


def _get_free_udp_port() -> int:
    """Возвращает свободный порт UDP для изоляции тестов."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class DummyStunServer:
    """Легковесный эмулятор STUN-координатора для юнит-тестов."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(("127.0.0.1", self.port))
        self.received_packets: List[Tuple[bytes, Tuple[str, int]]] = []
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=1.0)
        self.sock.close()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
                with self._lock:
                    self.received_packets.append((data, addr))
            except (BlockingIOError, OSError):
                time.sleep(0.01)

    def sendto(self, data: bytes, addr: Tuple[str, int]) -> None:
        self.sock.sendto(data, addr)

    def pop_packets(self) -> List[Tuple[bytes, Tuple[str, int]]]:
        with self._lock:
            pkts = list(self.received_packets)
            self.received_packets.clear()
            return pkts


# ============================================================================
# T9.1: Кодирование и декодирование сетевых пакетов P2P
# ============================================================================


def test_t9_1_1_control_packet_encoding_and_decoding():
    """T9.1.1: Проверка сигнатуры b'FP2P\\x01J', сериализации и десериализации JSON."""
    payload = {"cmd": "HOST_HELLO", "token": "abc123xyz", "port": 34197}
    encoded = encode_control_packet(payload)

    assert encoded.startswith(HEADER_CONTROL)
    assert encoded.startswith(b"FP2P\x01J")

    decoded = decode_control_packet(encoded)
    assert decoded == payload

    # Проверка на поврежденные пакеты
    assert decode_control_packet(b"") is None
    assert decode_control_packet(b"SHORT") is None
    assert decode_control_packet(b"FP2P\x01Xbad_type") is None
    assert decode_control_packet(b"FP2P\x01J{not_valid_json") is None


def test_t9_1_2_data_packet_formatting_and_parsing():
    """T9.1.2: Проверка упаковки guest_id (8 байт), режимов DIRECT и RELAY."""
    raw_payload = b"\x01\x02\x03\x04FactorioPacketData123456789"

    # Direct P2P
    pkt_direct = format_data_packet("g_123", raw_payload, is_relay=False)
    assert pkt_direct.startswith(HEADER_DATA)
    assert pkt_direct.startswith(b"FP2P\x01D")

    parsed_direct = parse_data_packet(pkt_direct)
    assert parsed_direct is not None
    guest_id, payload, is_relay = parsed_direct
    assert guest_id == "g_123"
    assert payload == raw_payload
    assert is_relay is False

    # Relay
    pkt_relay = format_data_packet("guest999_overflow", raw_payload, is_relay=True)
    assert pkt_relay.startswith(HEADER_RELAY)
    assert pkt_relay.startswith(b"FP2P\x01R")

    parsed_relay = parse_data_packet(pkt_relay)
    assert parsed_relay is not None
    g_id_r, payload_r, is_relay_r = parsed_relay
    # Длина guest_id обрезается ровно до 8 символов
    assert len(g_id_r) <= 8
    assert g_id_r == "guest999"[:8]
    assert payload_r == raw_payload
    assert is_relay_r is True

    # Некорректный заголовок
    assert parse_data_packet(b"too_short") is None
    assert parse_data_packet(b"FP2P\x01Z12345678payload") is None


# ============================================================================
# T9.2: HostP2PBridge: Регистрация на STUN и обработка PEER_MATCH
# ============================================================================


def test_t9_2_1_host_bridge_registration_and_host_ack():
    """T9.2.1 & T9.2.2: Отправка HOST_HELLO при старте и прием HOST_ACK от STUN."""
    stun_port = _get_free_udp_port()
    stun = DummyStunServer(stun_port)
    stun.start()

    host_bridge = HostP2PBridge(
        session_token="token_host_test",
        stun_host="127.0.0.1",
        stun_port=stun_port,
        poll_interval=0.01,
    )

    try:
        host_bridge.start()
        time.sleep(0.1)

        # Проверяем, что STUN получил HOST_HELLO
        pkts = stun.pop_packets()
        assert len(pkts) >= 1
        data, host_addr = pkts[0]
        ctrl = decode_control_packet(data)
        assert ctrl is not None
        assert ctrl["cmd"] == "HOST_HELLO"
        assert ctrl["session_token"] == "token_host_test"

        # Отправляем HOST_ACK обратно Хосту
        ack_pkt = encode_control_packet({
            "cmd": "HOST_ACK",
            "public_ip": host_addr[0],
            "public_port": host_addr[1],
        })
        stun.sendto(ack_pkt, host_addr)
        time.sleep(0.05)

        assert host_bridge._public_ip == host_addr[0]
        assert host_bridge._public_port == host_addr[1]

    finally:
        host_bridge.stop()
        stun.stop()


def test_t9_2_3_host_bridge_handles_peer_match_and_sends_syn():
    """T9.2.3: Обработка PEER_MATCH от STUN и отправка PUNCH_SYN на адреса Гостя."""
    stun_port = _get_free_udp_port()
    stun = DummyStunServer(stun_port)
    stun.start()

    guest_test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    guest_test_sock.setblocking(False)
    guest_test_sock.bind(("127.0.0.1", 0))
    guest_port = guest_test_sock.getsockname()[1]

    host_bridge = HostP2PBridge(
        session_token="token_match_test",
        stun_host="127.0.0.1",
        stun_port=stun_port,
        poll_interval=0.01,
    )

    try:
        host_bridge.start()
        time.sleep(0.05)
        _, host_addr = stun.pop_packets()[0]

        # STUN отправляет PEER_MATCH Хосту
        match_pkt = encode_control_packet({
            "cmd": "PEER_MATCH",
            "role": "host",
            "guest_id": "g_test1",
            "guest_name": "TestPlayer",
            "guest_public": ["127.0.0.1", guest_port],
            "guest_local": ["127.0.0.1", guest_port],
        })
        stun.sendto(match_pkt, host_addr)
        time.sleep(0.1)

        # Гость должен получить PUNCH_SYN от Хоста
        received_syn = False
        start_wait = time.time()
        while time.time() - start_wait < 1.0:
            try:
                g_data, _ = guest_test_sock.recvfrom(4096)
                ctrl = decode_control_packet(g_data)
                if ctrl and ctrl.get("cmd") == "PUNCH_SYN" and ctrl.get("guest_id") == "g_test1":
                    received_syn = True
                    break
            except (BlockingIOError, OSError):
                time.sleep(0.02)

        assert received_syn is True

    finally:
        host_bridge.stop()
        guest_test_sock.close()
        stun.stop()


# ============================================================================
# T9.3: GuestP2PProxy: STUN-хэндшейк и прямое пробитие NAT
# ============================================================================


def test_t9_3_1_guest_proxy_registration_and_peer_match():
    """T9.3.1 & T9.3.2: Отправка GUEST_HELLO и реакция на PEER_MATCH."""
    stun_port = _get_free_udp_port()
    stun = DummyStunServer(stun_port)
    stun.start()

    guest_proxy = GuestP2PProxy(
        guest_name="Alice",
        guest_id="alice_01",
        stun_host="127.0.0.1",
        stun_port=stun_port,
        proxy_port=_get_free_udp_port(),
        poll_interval=0.01,
        punch_timeout_sec=2.0,
    )

    host_test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    host_test_sock.setblocking(False)
    host_test_sock.bind(("127.0.0.1", 0))
    host_port = host_test_sock.getsockname()[1]

    try:
        guest_proxy.start()
        time.sleep(0.05)

        # STUN получает GUEST_HELLO
        pkts = stun.pop_packets()
        assert len(pkts) >= 1
        data, guest_addr = pkts[0]
        ctrl = decode_control_packet(data)
        assert ctrl is not None
        assert ctrl["cmd"] == "GUEST_HELLO"
        assert ctrl["guest_id"] == "alice_01"

        # STUN отправляет PEER_MATCH Гостю
        match_pkt = encode_control_packet({
            "cmd": "PEER_MATCH",
            "role": "guest",
            "host_public": ["127.0.0.1", host_port],
            "host_local": ["127.0.0.1", host_port],
            "guest_id": "alice_01",
        })
        stun.sendto(match_pkt, guest_addr)
        time.sleep(0.1)

        # Хост должен получить PUNCH_SYN от Гостя
        received_syn = False
        start_wait = time.time()
        syn_addr = None
        while time.time() - start_wait < 1.0:
            try:
                h_data, addr = host_test_sock.recvfrom(4096)
                c = decode_control_packet(h_data)
                if c and c.get("cmd") == "PUNCH_SYN" and c.get("guest_id") == "alice_01":
                    received_syn = True
                    syn_addr = addr
                    break
            except (BlockingIOError, OSError):
                time.sleep(0.02)

        assert received_syn is True
        assert syn_addr is not None

        # T9.3.3: Хост отвечает PUNCH_ACK -> Гость фиксирует DIRECT_P2P
        ack_pkt = encode_control_packet({
            "cmd": "PUNCH_ACK",
            "sender_role": "host",
            "guest_id": "alice_01",
            "ts": time.time() - 0.015,  # симуляция 15 мс RTT
        })
        host_test_sock.sendto(ack_pkt, syn_addr)
        time.sleep(0.05)

        assert guest_proxy.is_connected is True
        assert guest_proxy.is_direct_p2p is True
        assert guest_proxy.is_relay_active is False
        assert guest_proxy.rtt_ms is not None
        assert guest_proxy.rtt_ms >= 10.0

    finally:
        guest_proxy.stop()
        host_test_sock.close()
        stun.stop()


# ============================================================================
# T9.4: Сквозная передача данных в прямом P2P (Loopback Mock)
# ============================================================================


def test_t9_4_end_to_end_data_transfer_direct_p2p():
    """T9.4.1 - T9.4.3: Сквозная двусторонняя передача данных Factorio в прямом P2P."""
    stun_port = _get_free_udp_port()
    stun = DummyStunServer(stun_port)
    stun.start()

    # 1. Поднимаем эмулятор Factorio Server (слушает 127.0.0.1:factorio_srv_port)
    factorio_srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    factorio_srv.setblocking(False)
    factorio_srv.bind(("127.0.0.1", 0))
    srv_port = factorio_srv.getsockname()[1]

    # 2. Поднимаем эмулятор Factorio Client
    factorio_client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    factorio_client.setblocking(False)

    proxy_port = _get_free_udp_port()

    host_bridge = HostP2PBridge(
        session_token="token_e2e_direct",
        stun_host="127.0.0.1",
        stun_port=stun_port,
        factorio_port=srv_port,
        poll_interval=0.01,
    )

    guest_proxy = GuestP2PProxy(
        guest_name="DirectPlayer",
        guest_id="guest_01",
        stun_host="127.0.0.1",
        stun_port=stun_port,
        proxy_port=proxy_port,
        poll_interval=0.01,
    )

    try:
        host_bridge.start()
        guest_proxy.start()
        time.sleep(0.05)

        # Получаем адреса из STUN
        host_s_addr = host_bridge._s_host.getsockname()
        guest_s_addr = guest_proxy._s_guest.getsockname()

        # STUN рассылает PEER_MATCH Хосту и Гостю
        stun.sendto(
            encode_control_packet({
                "cmd": "PEER_MATCH",
                "role": "host",
                "guest_id": "guest_01",
                "guest_public": ["127.0.0.1", guest_s_addr[1]],
                "guest_local": ["127.0.0.1", guest_s_addr[1]],
            }),
            ("127.0.0.1", host_s_addr[1]),
        )
        stun.sendto(
            encode_control_packet({
                "cmd": "PEER_MATCH",
                "role": "guest",
                "host_public": ["127.0.0.1", host_s_addr[1]],
                "host_local": ["127.0.0.1", host_s_addr[1]],
                "guest_id": "guest_01",
            }),
            ("127.0.0.1", guest_s_addr[1]),
        )

        # Ждем, пока мост и прокси обменяются PUNCH_SYN / PUNCH_ACK
        start_wait = time.time()
        while time.time() - start_wait < 2.0:
            if guest_proxy.is_direct_p2p and host_bridge.get_guest_status("guest_01") and host_bridge.get_guest_status("guest_01")["is_direct_p2p"]:
                break
            time.sleep(0.02)

        assert guest_proxy.is_direct_p2p is True

        # --- ТЕСТ 1: Клиент Factorio отправляет пакет -> Сервер Factorio получает ---
        client_payload = secrets.token_bytes(256)
        factorio_client.sendto(client_payload, ("127.0.0.1", proxy_port))

        received_by_server = None
        server_client_addr = None
        start_wait = time.time()
        while time.time() - start_wait < 2.0:
            try:
                received_by_server, server_client_addr = factorio_srv.recvfrom(65535)
                break
            except (BlockingIOError, OSError):
                time.sleep(0.02)

        assert received_by_server is not None
        # Проверяем точность до байта (SHA-256)
        assert hashlib.sha256(received_by_server).hexdigest() == hashlib.sha256(client_payload).hexdigest()

        # --- ТЕСТ 2: Сервер Factorio отвечает -> Клиент Factorio получает ---
        server_reply = secrets.token_bytes(512)
        factorio_srv.sendto(server_reply, server_client_addr)

        received_by_client = None
        start_wait = time.time()
        while time.time() - start_wait < 2.0:
            try:
                received_by_client, _ = factorio_client.recvfrom(65535)
                break
            except (BlockingIOError, OSError):
                time.sleep(0.02)

        assert received_by_client is not None
        assert hashlib.sha256(received_by_client).hexdigest() == hashlib.sha256(server_reply).hexdigest()

    finally:
        guest_proxy.stop()
        host_bridge.stop()
        factorio_srv.close()
        factorio_client.close()
        stun.stop()


# ============================================================================
# T9.5: Мультиплексирование нескольких Гостей на Хосте
# ============================================================================


def test_t9_5_host_bridge_multiplexes_multiple_guests():
    """T9.5.1 - T9.5.3: Хост создает независимые локальные сокеты для разных гостей."""
    factorio_srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    factorio_srv.setblocking(False)
    factorio_srv.bind(("127.0.0.1", 0))
    srv_port = factorio_srv.getsockname()[1]

    host_bridge = HostP2PBridge(
        session_token="token_multi_guest",
        stun_host="127.0.0.1",
        stun_port=_get_free_udp_port(),
        factorio_port=srv_port,
        poll_interval=0.01,
    )

    host_bridge.start()
    time.sleep(0.05)
    host_addr = ("127.0.0.1", host_bridge._s_host.getsockname()[1])

    # Создаем два сокета, имитирующих внешние сокеты двух разных гостей
    guest1_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    guest1_sock.setblocking(False)
    guest1_sock.bind(("127.0.0.1", 0))

    guest2_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    guest2_sock.setblocking(False)
    guest2_sock.bind(("127.0.0.1", 0))

    try:
        # Гость 1 шлет PUNCH_SYN
        guest1_sock.sendto(
            encode_control_packet({"cmd": "PUNCH_SYN", "guest_id": "g_one", "ts": time.time()}),
            host_addr,
        )
        # Гость 2 шлет PUNCH_SYN
        guest2_sock.sendto(
            encode_control_packet({"cmd": "PUNCH_SYN", "guest_id": "g_two", "ts": time.time()}),
            host_addr,
        )
        time.sleep(0.05)

        # Дренируем PUNCH_ACK из сокетов гостей
        time.sleep(0.05)
        for g_sock in (guest1_sock, guest2_sock):
            try:
                while True:
                    g_sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                pass

        # Гость 1 отправляет игровые данные
        payload_1 = b"DATA_FROM_GUEST_ONE"
        guest1_sock.sendto(format_data_packet("g_one", payload_1), host_addr)

        # Гость 2 отправляет игровые данные
        payload_2 = b"DATA_FROM_GUEST_TWO"
        guest2_sock.sendto(format_data_packet("g_two", payload_2), host_addr)

        # Сервер Factorio должен получить два пакета с РАЗНЫХ портов
        srv_packets: List[Tuple[bytes, Tuple[str, int]]] = []
        start_wait = time.time()
        while time.time() - start_wait < 1.0 and len(srv_packets) < 2:
            try:
                d, a = factorio_srv.recvfrom(4096)
                srv_packets.append((d, a))
            except (BlockingIOError, OSError):
                time.sleep(0.02)

        assert len(srv_packets) == 2
        data1, addr1 = srv_packets[0]
        data2, addr2 = srv_packets[1]

        # Порты должны отличаться! (мультиплексирование L_guest_1 и L_guest_2)
        assert addr1[1] != addr2[1]
        assert {data1, data2} == {payload_1, payload_2}

        # Ответ сервера первому гостю доставляется только первому гостю
        factorio_srv.sendto(b"REPLY_TO_ONE", addr1 if data1 == payload_1 else addr2)
        time.sleep(0.05)

        # Читаем у Гостя 1
        g1_data, _ = guest1_sock.recvfrom(4096)
        parsed_1 = parse_data_packet(g1_data)
        assert parsed_1 is not None
        assert parsed_1[1] == b"REPLY_TO_ONE"

        # У Гостя 2 нет данных
        with pytest.raises((BlockingIOError, OSError)):
            guest2_sock.recvfrom(4096)

    finally:
        host_bridge.stop()
        guest1_sock.close()
        guest2_sock.close()
        factorio_srv.close()


# ============================================================================
# T9.6: Fallback Relay при жестком CGNAT и очистка ресурсов
# ============================================================================


def test_t9_6_1_fallback_relay_triggers_on_punch_timeout():
    """T9.6.1: Переключение на резервный релей при отсутствии прямого ответа."""
    stun_port = _get_free_udp_port()
    stun = DummyStunServer(stun_port)
    stun.start()

    guest_proxy = GuestP2PProxy(
        guest_name="CgnatUser",
        guest_id="cgnat_01",
        stun_host="127.0.0.1",
        stun_port=stun_port,
        proxy_port=_get_free_udp_port(),
        punch_timeout_sec=0.15,  # быстрый таймаут
        poll_interval=0.01,
    )

    try:
        guest_proxy.start()
        time.sleep(0.05)

        # STUN отвечает PEER_MATCH с недоступным адресом Хоста (симулируем симметричный CGNAT)
        pkts = stun.pop_packets()
        assert len(pkts) >= 1
        _, g_addr = pkts[0]

        stun.sendto(
            encode_control_packet({
                "cmd": "PEER_MATCH",
                "role": "guest",
                "host_public": ["192.0.2.1", 54321],  # RFC 5737 TEST-NET (недоступно)
                "guest_id": "cgnat_01",
            }),
            g_addr,
        )

        # Ждем истечения punch_timeout_sec (0.15с)
        time.sleep(0.25)

        assert guest_proxy.is_connected is True
        assert guest_proxy.is_relay_active is True
        assert guest_proxy.is_direct_p2p is False

    finally:
        guest_proxy.stop()
        stun.stop()


def test_t9_6_2_end_to_end_data_via_relay():
    """T9.6.2: Сквозная передача данных Factorio через STUN-релей."""
    stun_port = _get_free_udp_port()
    # Реализуем relay-форвардинг в тестовом STUN-сервере
    relay_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    relay_server.setblocking(False)
    relay_server.bind(("127.0.0.1", stun_port))

    factorio_srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    factorio_srv.setblocking(False)
    factorio_srv.bind(("127.0.0.1", 0))
    srv_port = factorio_srv.getsockname()[1]

    proxy_port = _get_free_udp_port()

    host_bridge = HostP2PBridge(
        session_token="token_relay",
        stun_host="127.0.0.1",
        stun_port=stun_port,
        factorio_port=srv_port,
        poll_interval=0.01,
    )
    guest_proxy = GuestP2PProxy(
        guest_name="RelayUser",
        guest_id="r_guest1",
        stun_host="127.0.0.1",
        stun_port=stun_port,
        proxy_port=proxy_port,
        punch_timeout_sec=0.1,
        poll_interval=0.01,
    )

    factorio_client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    factorio_client.setblocking(False)

    try:
        host_bridge.start()
        guest_proxy.start()
        time.sleep(0.05)

        host_s_addr = ("127.0.0.1", host_bridge._s_host.getsockname()[1])
        guest_s_addr = ("127.0.0.1", guest_proxy._s_guest.getsockname()[1])

        # Принудительно переводим обоих в relay-режим
        with guest_proxy._lock:
            guest_proxy._is_connected = True
            guest_proxy._is_relay_active = True
            guest_proxy._is_direct_p2p = False

        # Клиент шлет пакет
        client_data = b"RELAY_FACTORIO_PACKET_TEST"
        factorio_client.sendto(client_data, ("127.0.0.1", proxy_port))
        time.sleep(0.05)

        # STUN-релей ловит пакет от Гостя и пересылает Хосту
        relay_pkt = None
        start_wait = time.time()
        while time.time() - start_wait < 1.5:
            try:
                pkt, _ = relay_server.recvfrom(4096)
                if pkt.startswith(HEADER_RELAY):
                    relay_pkt = pkt
                    break
            except (BlockingIOError, OSError):
                time.sleep(0.01)

        assert relay_pkt is not None
        assert relay_pkt.startswith(HEADER_RELAY)
        relay_server.sendto(relay_pkt, host_s_addr)
        time.sleep(0.05)

        # Сервер Factorio должен получить распакованный пакет
        srv_data = None
        srv_from = None
        start_wait = time.time()
        while time.time() - start_wait < 1.5:
            try:
                srv_data, srv_from = factorio_srv.recvfrom(4096)
                break
            except (BlockingIOError, OSError):
                time.sleep(0.01)

        assert srv_data == client_data

        # Сервер отвечает
        srv_reply = b"RELAY_SERVER_REPLY_OK"
        factorio_srv.sendto(srv_reply, srv_from)
        time.sleep(0.05)

        # STUN-релей ловит пакет от Хоста и пересылает Гостю
        reply_relay_pkt = None
        start_wait = time.time()
        while time.time() - start_wait < 1.5:
            try:
                pkt, _ = relay_server.recvfrom(4096)
                if pkt.startswith(HEADER_RELAY):
                    reply_relay_pkt = pkt
                    break
            except (BlockingIOError, OSError):
                time.sleep(0.01)

        assert reply_relay_pkt is not None
        assert reply_relay_pkt.startswith(HEADER_RELAY)
        relay_server.sendto(reply_relay_pkt, guest_s_addr)
        time.sleep(0.05)

        # Клиент Factorio получает чистый ответ
        client_reply, _ = factorio_client.recvfrom(4096)
        assert client_reply == srv_reply

    finally:
        guest_proxy.stop()
        host_bridge.stop()
        factorio_srv.close()
        factorio_client.close()
        relay_server.close()


def test_t9_6_3_clean_resource_cleanup_on_stop():
    """T9.6.3: При вызове stop() все потоки и сокеты закрываются без утечек."""
    host_bridge = HostP2PBridge(
        session_token="token_clean",
        stun_host="127.0.0.1",
        stun_port=_get_free_udp_port(),
    )
    guest_proxy = GuestP2PProxy(
        guest_name="CleanUser",
        stun_host="127.0.0.1",
        stun_port=_get_free_udp_port(),
        proxy_port=_get_free_udp_port(),
    )

    host_bridge.start()
    guest_proxy.start()
    time.sleep(0.05)

    assert host_bridge.is_running is True
    assert guest_proxy.is_running is True

    host_bridge.stop()
    guest_proxy.stop()

    assert host_bridge.is_running is False
    assert guest_proxy.is_running is False
    assert not host_bridge._thread.is_alive()
    assert not guest_proxy._thread.is_alive()
    assert host_bridge._s_host is None
    assert guest_proxy._l_proxy is None
    assert guest_proxy._s_guest is None


# ============================================================================
# T9.7: macOS VPN Bypass via IP_BOUND_IF (опция сокета 25)
# ============================================================================


def test_t9_7_1_get_primary_physical_interface_darwin():
    """T9.7.1: Определение первичного физического интерфейса (en0 / en1) на macOS."""
    from unittest.mock import patch
    from src.client.p2p_tunnel import get_primary_physical_interface_darwin

    # 1. Если не Darwin — возвращает None
    with patch("sys.platform", "win32"):
        assert get_primary_physical_interface_darwin() is None

    # 2. Успешный парсинг scutil --nwi
    fake_scutil_output = """Network information
IPv4 network interface information
   utun4 : flags      : 0x7 (IPv4,IPv6,DNS)
           address    : 198.18.0.1
     en0 : flags      : 0x5 (IPv4,DNS)
           address    : 192.168.1.50
           reach      : 0x00000002 (Reachable)
"""
    with patch("sys.platform", "darwin"):
        with patch("subprocess.check_output", return_value=fake_scutil_output):
            with patch("socket.if_nametoindex", return_value=4):
                iface = get_primary_physical_interface_darwin()
                assert iface == "en0"

    # 3. Парсинг через netstat, если scutil не дал результат
    fake_netstat_output = """Routing tables
Destination        Gateway            Flags               Netif Expire
default            link#19            UCSg                utun4       
default            192.168.1.1        UGScIg                en1       
"""
    with patch("sys.platform", "darwin"):
        with patch("subprocess.check_output", side_effect=[Exception("scutil failed"), fake_netstat_output]):
            with patch("socket.if_nametoindex", return_value=5):
                iface = get_primary_physical_interface_darwin()
                assert iface == "en1"


def test_t9_7_2_bind_socket_to_physical_interface_darwin():
    """T9.7.2: Вызов setsockopt(IPPROTO_IP, 25, if_index) для привязки сокета."""
    import struct
    from unittest.mock import MagicMock, patch
    from src.client.p2p_tunnel import bind_socket_to_physical_interface

    mock_sock = MagicMock()

    with patch("sys.platform", "darwin"):
        with patch("src.client.p2p_tunnel.get_primary_physical_interface_darwin", return_value="en0"):
            with patch("socket.if_nametoindex", return_value=7):
                res = bind_socket_to_physical_interface(mock_sock)
                assert res == "en0"
                mock_sock.setsockopt.assert_called_once_with(
                    socket.IPPROTO_IP,
                    25,
                    struct.pack("I", 7),
                )


def test_t9_7_3_bind_socket_skips_non_darwin_or_disabled():
    """T9.7.3: На non-Darwin ОС или при FACTORIO_DISABLE_IP_BOUND_IF привязка не вызывается."""
    from unittest.mock import MagicMock, patch
    from src.client.p2p_tunnel import bind_socket_to_physical_interface

    mock_sock = MagicMock()

    # non-Darwin
    with patch("sys.platform", "linux"):
        res = bind_socket_to_physical_interface(mock_sock)
        assert res is None
        assert not mock_sock.setsockopt.called

    # Darwin, но выключено флагом
    with patch("sys.platform", "darwin"):
        with patch.dict(os.environ, {"FACTORIO_DISABLE_IP_BOUND_IF": "1"}):
            res = bind_socket_to_physical_interface(mock_sock)
            assert res is None
            assert not mock_sock.setsockopt.called


def test_t9_7_4_detect_physical_lan_ip():
    """T9.7.4: Определение локального LAN IP с фильтрацией петли и VPN-адресов."""
    from unittest.mock import MagicMock, patch
    from src.client.p2p_tunnel import detect_physical_lan_ip

    # Успешное определение реального локального IP
    mock_sock = MagicMock()
    mock_sock.getsockname.return_value = ("192.168.1.100", 54321)

    with patch("socket.socket", return_value=mock_sock):
        with patch("src.client.p2p_tunnel.bind_socket_to_physical_interface"):
            ip = detect_physical_lan_ip("1.1.1.1")
            assert ip == "192.168.1.100"

    # Фильтрация fake VPN IP (198.18.*)
    mock_vpn_sock = MagicMock()
    mock_vpn_sock.getsockname.return_value = ("198.18.0.1", 54321)
    with patch("socket.socket", return_value=mock_vpn_sock):
        with patch("src.client.p2p_tunnel.bind_socket_to_physical_interface"):
            ip_vpn = detect_physical_lan_ip("1.1.1.1")
            assert ip_vpn is None


def test_t9_7_5_guest_proxy_only_binds_external_socket():
    """T9.7.5: В GuestP2PProxy только внешний сокет _s_guest привязывается к интерфейсу, а _l_proxy нет."""
    from unittest.mock import patch

    bound_sockets = []

    def mock_bind(sock):
        bound_sockets.append(sock)
        return "en0"

    guest_proxy = GuestP2PProxy(
        guest_name="TestBypass",
        stun_host="127.0.0.1",
        stun_port=_get_free_udp_port(),
        proxy_port=_get_free_udp_port(),
    )

    with patch("src.client.p2p_tunnel.bind_socket_to_physical_interface", side_effect=mock_bind):
        guest_proxy.start()
        time.sleep(0.05)

        try:
            assert guest_proxy._s_guest in bound_sockets
            assert guest_proxy._l_proxy not in bound_sockets
        finally:
            guest_proxy.stop()

