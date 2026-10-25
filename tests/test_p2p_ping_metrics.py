"""Модульные и интеграционные тесты для замера RTT, режима P2P/Relay и передачи метрик в /heartbeat.

Задача 033:
- T33.1: Замер RTT по эхо-таймстемпам и EMA-сглаживание в прямом P2P.
- T33.2: Замер RTT и перехват управляющих пакетов в режиме Relay.
- T33.3: Защита от рассинхрона системных часов (Clock Skew Immunity).
- T33.4: Фильтрация аномальных и отрицательных значений RTT (NTP, заморозка ОС).
- T33.5: Фильтрация устаревших гостей (stale_timeout_sec) и обработка отсутствия пинга (None).
- T33.6: Полная изоляция сокетов Factorio: управляющие пакеты никогда не попадают в порт игры.
- T33.7: Проброс guests_metrics в HeartbeatWorker, HubClient и отказоустойчивость при сбоях.
- T33.8: Интеграционная связка SessionOrchestrator -> HostP2PBridge.get_guests_metrics.
"""

from __future__ import annotations

import json
import socket
import time
from unittest.mock import MagicMock, patch

import pytest
import httpx

from src.client.host_workers import HeartbeatWorker, HostWorkersManager
from src.client.hub_client import HubClient
from src.client.p2p_tunnel import (
    HEADER_CONTROL,
    HEADER_DATA,
    HEADER_RELAY,
    ConnectedGuestState,
    GuestP2PProxy,
    HostP2PBridge,
    decode_control_packet,
    encode_control_packet,
    encode_relay_packet,
    format_data_packet,
    is_control_packet,
    parse_data_packet,
)
from src.client.session_orchestrator import RoleType, SessionOrchestrator


def _get_free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ============================================================================
# T33.1: Замер RTT и EMA-сглаживание в прямом P2P
# ============================================================================


def test_t33_1_direct_p2p_rtt_and_ema_smoothing():
    """T33.1: Проверка замера RTT по эхо-таймстемпу, EMA-сглаживания и get_guests_metrics в Direct P2P."""
    host_bridge = HostP2PBridge(
        session_token="test_token_033",
        stun_host="127.0.0.1",
        stun_port=14197,
        factorio_port=34197,
    )
    guest_proxy = GuestP2PProxy(
        guest_name="PikachuPlayer",
        guest_id="guest001",
        stun_host="127.0.0.1",
        stun_port=14197,
    )

    guest_addr = ("127.0.0.1", 55551)
    host_addr = ("127.0.0.1", 55552)
    guest_proxy._host_active_endpoint = host_addr
    guest_proxy._is_direct_p2p = True

    # 1. Гость отправляет первый P2P_KEEPALIVE с ts = 1000.0, пока rtt_ms = None
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        # Имитируем отправку keepalive Гостем
        pkt_bytes = encode_control_packet({
            "cmd": "P2P_KEEPALIVE",
            "sender_role": "guest",
            "guest_id": guest_proxy.guest_id,
            "guest_name": guest_proxy.guest_name,
            "ts": t0,
            "ping_M": guest_proxy.rtt_ms,
        })

    # 2. Хост принимает P2P_KEEPALIVE напрямую от guest_addr
    ctrl = decode_control_packet(pkt_bytes)
    assert ctrl is not None
    host_bridge._s_host = MagicMock()

    host_bridge._handle_p2p_keepalive(ctrl, addr=guest_addr, via_relay=False)

    # Проверяем состояние на Хосте
    guest_state = host_bridge._guests["guest001"]
    assert guest_state.is_direct_p2p is True
    assert guest_state.is_relay_active is False
    assert guest_state.active_endpoint == guest_addr
    assert guest_state.guest_name == "PikachuPlayer"

    # Проверяем, что Хост отправил P2P_KEEPALIVE_ACK с echo_ts = 1000.0
    host_bridge._s_host.sendto.assert_called_once()
    ack_sent, target_addr = host_bridge._s_host.sendto.call_args[0]
    assert target_addr == guest_addr
    ack_ctrl = decode_control_packet(ack_sent)
    assert ack_ctrl is not None
    assert ack_ctrl["cmd"] == "P2P_KEEPALIVE_ACK"
    assert ack_ctrl["echo_ts"] == 1000.0

    # 3. Гость получает P2P_KEEPALIVE_ACK через 30 мс (время 1000.030)
    with patch("time.time", return_value=1000.030):
        guest_proxy._handle_keepalive_ack(ack_ctrl)

    # Первичное значение без сглаживания
    assert guest_proxy.rtt_ms == 30.0

    # 4. Гость отправляет второй P2P_KEEPALIVE, уже передавая ping_M = 30.0
    t1 = 1003.0
    pkt2_bytes = encode_control_packet({
        "cmd": "P2P_KEEPALIVE",
        "sender_role": "guest",
        "guest_id": guest_proxy.guest_id,
        "guest_name": guest_proxy.guest_name,
        "ts": t1,
        "ping_M": guest_proxy.rtt_ms,
    })
    ctrl2 = decode_control_packet(pkt2_bytes)
    host_bridge._s_host.reset_mock()
    host_bridge._handle_p2p_keepalive(ctrl2, addr=guest_addr, via_relay=False)

    # Хост обновил rtt_ms гостя
    assert guest_state.rtt_ms == 30.0

    # 5. Гость получает второй ответ с задержкой 40 мс (время 1003.040)
    # EMA: 0.7 * 30.0 + 0.3 * 40.0 = 21.0 + 12.0 = 33.0 мс
    ack2_sent = host_bridge._s_host.sendto.call_args[0][0]
    ack2_ctrl = decode_control_packet(ack2_sent)
    with patch("time.time", return_value=1003.040):
        guest_proxy._handle_keepalive_ack(ack2_ctrl)

    assert guest_proxy.rtt_ms == 33.0

    # 6. Проверяем get_guests_metrics на Хосте
    metrics = host_bridge.get_guests_metrics()
    assert len(metrics) == 1
    assert metrics[0] == {
        "name": "PikachuPlayer",
        "guest_id": "guest001",
        "ping_ms": 30.0,
        "direct_p2p": True,
        "mode": "P2P",
    }


# ============================================================================
# T33.2: Замер RTT и режим Relay
# ============================================================================


def test_t33_2_relay_mode_rtt_and_packet_encapsulation():
    """T33.2: Проверка замера RTT при передаче через Relay-сервер VPS и изоляции трафика."""
    stun_addr = ("127.0.0.1", 14197)
    host_bridge = HostP2PBridge(
        session_token="test_token_033",
        stun_host=stun_addr[0],
        stun_port=stun_addr[1],
    )
    guest_proxy = GuestP2PProxy(
        guest_name="RelayGuest",
        guest_id="guest_r1",
        stun_host=stun_addr[0],
        stun_port=stun_addr[1],
    )

    guest_proxy._is_relay_active = True
    guest_proxy._is_direct_p2p = False
    guest_proxy._s_guest = MagicMock()
    host_bridge._s_host = MagicMock()

    # 1. Гость формирует keepalive в режиме Relay
    t0 = 2000.0
    with patch("time.time", return_value=t0):
        guest_proxy._send_keepalive(now=t0)

    # Проверяем, что пакет был отправлен на stun_addr и упакован в HEADER_RELAY
    guest_proxy._s_guest.sendto.assert_called_once()
    relay_pkt, dest = guest_proxy._s_guest.sendto.call_args[0]
    assert dest == stun_addr
    assert relay_pkt.startswith(HEADER_RELAY)

    # 2. Распаковываем на стороне Хоста
    parsed = parse_data_packet(relay_pkt)
    assert parsed is not None
    guest_id, raw_payload, is_relay = parsed
    assert is_relay is True
    assert guest_id == "guest_r1"
    assert is_control_packet(raw_payload) is True

    # Имитируем чтение на внешнем сокете Хоста
    host_bridge._s_host.recvfrom = MagicMock(return_value=(relay_pkt, stun_addr))
    host_bridge._handle_external_socket_read()

    # Проверяем состояние Гостя на Хосте: режим Relay!
    guest_state = host_bridge._guests["guest_r1"]
    assert guest_state.is_direct_p2p is False
    assert guest_state.is_relay_active is True

    # Проверяем, что Хост отправил ответ через Relay на STUN
    host_bridge._s_host.sendto.assert_called_once()
    host_ack_relay, ack_dest = host_bridge._s_host.sendto.call_args[0]
    assert ack_dest == stun_addr
    assert host_ack_relay.startswith(HEADER_RELAY)

    ack_parsed = parse_data_packet(host_ack_relay)
    assert ack_parsed is not None
    ack_guest_id, ack_inner, ack_is_relay = ack_parsed
    assert ack_guest_id == "guest_r1"
    assert ack_is_relay is True
    assert is_control_packet(ack_inner) is True

    # 3. Гость получает ответ от Relay
    guest_proxy._l_proxy = MagicMock()
    guest_proxy._s_guest.recvfrom = MagicMock(return_value=(host_ack_relay, stun_addr))

    with patch("time.time", return_value=t0 + 0.055):  # 55 мс
        guest_proxy._handle_external_socket_read()

    # Пинг Гостя вычислен: 55 мс
    assert guest_proxy.rtt_ms == 55.0
    # И при этом управляющий пакет НЕ попал в локальный клиент Factorio!
    guest_proxy._l_proxy.sendto.assert_not_called()

    # Проверяем метрики Хоста: режим Relay
    metrics = host_bridge.get_guests_metrics()
    assert len(metrics) == 1
    assert metrics[0]["mode"] == "Relay"
    assert metrics[0]["direct_p2p"] is False


# ============================================================================
# T33.3: Защита от рассинхрона часов (Clock Skew Immunity)
# ============================================================================


def test_t33_3_clock_skew_immunity():
    """T33.3: Проверка, что расхождение часов Хоста и Гостя (+3600с / -3600с) не влияет на RTT."""
    guest_proxy = GuestP2PProxy(guest_name="SkewTester", guest_id="skew001")
    host_bridge = HostP2PBridge(session_token="token_skew", stun_host="127.0.0.1")

    # Пусть часы Гостя показывают 10_000.0
    guest_start_time = 10_000.0
    keepalive_pkt = encode_control_packet({
        "cmd": "P2P_KEEPALIVE",
        "guest_id": guest_proxy.guest_id,
        "ts": guest_start_time,
        "ping_M": None,
    })

    # Часы Хоста спешат на 1 час (3600 сек) вперед: 13_600.0
    host_time = 13_600.0
    host_bridge._s_host = MagicMock()
    with patch("time.time", return_value=host_time):
        host_bridge._handle_p2p_keepalive(
            decode_control_packet(keepalive_pkt),
            addr=("127.0.0.1", 44444),
            via_relay=False,
        )

    ack_bytes = host_bridge._s_host.sendto.call_args[0][0]
    ack_ctrl = decode_control_packet(ack_bytes)
    assert ack_ctrl is not None
    # Эхо содержит оригинальный таймстемп Гостя
    assert ack_ctrl["echo_ts"] == 10_000.0

    # Гость принимает ответ через 15.4 мс по своим часам (10_000.0154)
    with patch("time.time", return_value=10_000.0154):
        guest_proxy._handle_keepalive_ack(ack_ctrl)

    # RTT равен ровно 15.4 мс, спешащие на 1 час часы Хоста не оказали никакого влияния!
    assert guest_proxy.rtt_ms == 15.4


# ============================================================================
# T33.4: Фильтрация аномальных и отрицательных значений RTT
# ============================================================================


def test_t33_4_anomalous_rtt_filtering():
    """T33.4: Отклонение отрицательных значений (сдвиг NTP) и слишком больших (> 9999 мс)."""
    guest_proxy = GuestP2PProxy(guest_name="AnomalyTester", guest_id="anom001")
    guest_proxy._rtt_ms = 25.0

    # 1. Отрицательный RTT (служба времени Windows сдвинула часы назад)
    with patch("time.time", return_value=1000.0):
        guest_proxy._handle_keepalive_ack({"cmd": "P2P_KEEPALIVE_ACK", "echo_ts": 1050.0})
    # rtt_ms остался неизменным 25.0!
    assert guest_proxy.rtt_ms == 25.0

    # 2. Слишком малый RTT (< 0.05 мс)
    with patch("time.time", return_value=1000.0):
        guest_proxy._handle_keepalive_ack({"cmd": "P2P_KEEPALIVE_ACK", "echo_ts": 999.99999})
    assert guest_proxy.rtt_ms == 25.0

    # 3. Слишком большой RTT (> 9999.0 мс, гибернация или зависание)
    with patch("time.time", return_value=10000.0):
        guest_proxy._handle_keepalive_ack({"cmd": "P2P_KEEPALIVE_ACK", "echo_ts": 0.0})
    assert guest_proxy.rtt_ms == 25.0

    # 4. Валидный RTT (35.0 мс)
    # EMA: 0.7 * 25.0 + 0.3 * 35.0 = 17.5 + 10.5 = 28.0 мс
    with patch("time.time", return_value=1000.035):
        guest_proxy._handle_keepalive_ack({"cmd": "P2P_KEEPALIVE_ACK", "echo_ts": 1000.0})
    assert guest_proxy.rtt_ms == 28.0


# ============================================================================
# T33.5: Фильтрация устаревших гостей и обработка None
# ============================================================================


def test_t33_5_stale_guest_filtering_and_none_rtt():
    """T33.5: Фильтрация гостей старше stale_timeout_sec и корректный возврат None."""
    host_bridge = HostP2PBridge(session_token="token_stale", stun_host="127.0.0.1")

    now = 5000.0
    with patch("time.time", return_value=now):
        # Гость 1: активный (last_seen = now - 2.0), пинг 18.2
        host_bridge._guests["g1"] = ConnectedGuestState(
            guest_id="g1",
            guest_name="ActivePlayer",
            last_seen=now - 2.0,
            is_direct_p2p=True,
            rtt_ms=18.2,
        )
        # Гость 2: только что подключился, rtt_ms = None
        host_bridge._guests["g2"] = ConnectedGuestState(
            guest_id="g2",
            guest_name="NewPlayer",
            last_seen=now - 0.5,
            is_direct_p2p=False,
            is_relay_active=True,
            rtt_ms=None,
        )
        # Гость 3: устаревший (last_seen = now - 20.0), должен быть отфильтрован при таймауте 15.0
        host_bridge._guests["g3"] = ConnectedGuestState(
            guest_id="g3",
            guest_name="OldZombie",
            last_seen=now - 20.0,
            is_direct_p2p=True,
            rtt_ms=45.0,
        )

        metrics = host_bridge.get_guests_metrics(stale_timeout_sec=15.0)

    # Должны остаться только ActivePlayer и NewPlayer, отсортированные по имени
    assert len(metrics) == 2
    assert metrics[0]["name"] == "ActivePlayer"
    assert metrics[0]["ping_ms"] == 18.2
    assert metrics[0]["mode"] == "P2P"

    assert metrics[1]["name"] == "NewPlayer"
    assert metrics[1]["ping_ms"] is None
    assert metrics[1]["mode"] == "Relay"


# ============================================================================
# T33.6: Изоляция трафика Factorio
# ============================================================================


def test_t33_6_factorio_traffic_isolation():
    """T33.6: Управляющие пакеты внутри DATA и RELAY перехватываются и не попадают в сокет игры."""
    host_bridge = HostP2PBridge(session_token="token_iso", stun_host="127.0.0.1", factorio_port=34197)
    host_bridge._s_host = MagicMock()

    # Создаем фиктивный сокет Factorio для гостя
    dummy_factorio_sock = MagicMock()
    guest_state = ConnectedGuestState(
        guest_id="iso_g1",
        guest_name="IsoGuest",
        local_factorio_sock=dummy_factorio_sock,
        is_direct_p2p=True,
    )
    host_bridge._guests["iso_g1"] = guest_state
    host_bridge._sock_to_guest_id[dummy_factorio_sock] = "iso_g1"

    # 1. Передача обычных игровых данных Factorio
    game_payload = b"\x01\x02\x03\x04FactorioTickData"
    data_pkt = format_data_packet("iso_g1", game_payload, is_relay=False)
    host_bridge._s_host.recvfrom = MagicMock(return_value=(data_pkt, ("1.2.3.4", 12345)))
    host_bridge._handle_external_socket_read()

    # Игровые данные должны быть переданы в сокет Factorio
    dummy_factorio_sock.sendto.assert_called_once_with(game_payload, ("127.0.0.1", 34197))
    dummy_factorio_sock.sendto.reset_mock()

    # 2. Инкапсулированный управляющий пакет P2P_KEEPALIVE внутри RELAY_DATA
    control_payload = encode_control_packet({"cmd": "P2P_KEEPALIVE", "guest_id": "iso_g1", "ts": 123.4})
    relay_ctrl_pkt = format_data_packet("iso_g1", control_payload, is_relay=True)
    host_bridge._s_host.recvfrom = MagicMock(return_value=(relay_ctrl_pkt, ("127.0.0.1", 14197)))
    host_bridge._handle_external_socket_read()

    # В сокет Factorio НИЧЕГО не должно было отправиться!
    dummy_factorio_sock.sendto.assert_not_called()


# ============================================================================
# T33.7: Проброс guests_metrics в HeartbeatWorker и HubClient
# ============================================================================


def test_t33_7_heartbeat_worker_metrics_forwarding_and_exception_safety():
    """T33.7: Передача guests_metrics в HubClient и отказоустойчивость при исключениях."""
    mock_hub = MagicMock(spec=HubClient)
    mock_hub.send_heartbeat.return_value = {"success": True, "alive": True}

    # 1. Нормальный сценарий: передача списка метрик
    sample_metrics = [
        {"name": "Alex", "guest_id": "g_alex", "ping_ms": 14.5, "direct_p2p": True, "mode": "P2P"}
    ]
    worker = HeartbeatWorker(
        hub_client=mock_hub,
        session_token="tok_hb",
        is_confirmed_getter=lambda: True,
        udp_port=34197,
        guests_metrics_getter=lambda: sample_metrics,
    )

    worker.send_once()
    mock_hub.send_heartbeat.assert_called_once_with(
        "tok_hb",
        udp_port=34197,
        guests_metrics=sample_metrics,
    )

    # 2. Отказоустойчивость: исключение в getter не должно ронять Heartbeat!
    mock_hub.send_heartbeat.reset_mock()
    failing_worker = HeartbeatWorker(
        hub_client=mock_hub,
        session_token="tok_hb",
        is_confirmed_getter=lambda: True,
        udp_port=34197,
        guests_metrics_getter=MagicMock(side_effect=RuntimeError("Metric calculation crash")),
    )

    res = failing_worker.send_once()
    assert res is not None
    # Heartbeat успешно отправлен без падения и без поврежденных метрик
    mock_hub.send_heartbeat.assert_called_once_with(
        "tok_hb",
        udp_port=34197,
    )


def test_t33_7_hub_client_send_heartbeat_payload():
    """T33.7b: Проверка корректного формирования JSON-тела в HubClient.send_heartbeat."""
    captured_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.url.path == "/heartbeat":
            return httpx.Response(200, json={"success": True, "status": {"session": {"state": "hosting_active"}}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler), base_url="http://test-server:8088")
    hub = HubClient(http_client=client)

    # Запрос с guests_metrics
    metrics = [{"name": "Bob", "ping_ms": 28.0, "mode": "Relay"}]
    hub.send_heartbeat("tok_123", udp_port=34197, guests_metrics=metrics)

    assert len(captured_requests) == 1
    req = captured_requests[0]
    payload = json.loads(req.content.decode("utf-8"))
    assert payload["session_token"] == "tok_123"
    assert payload["udp_port"] == 34197
    assert payload["guests_metrics"] == metrics

    # Запрос без guests_metrics: ключ не должен присутствовать или равен None
    captured_requests.clear()
    hub.send_heartbeat("tok_123", udp_port=34197)
    assert len(captured_requests) == 1
    payload2 = json.loads(captured_requests[0].content.decode("utf-8"))
    assert "guests_metrics" not in payload2


# ============================================================================
# T33.8: Интеграционная связка HostWorkersManager и SessionOrchestrator
# ============================================================================


def test_t33_8_host_workers_manager_wiring(tmp_path):
    """T33.8: Проверка передачи guests_metrics_getter из HostWorkersManager в HeartbeatWorker."""
    log_file = tmp_path / "factorio-current.log"
    log_file.write_text("Factorio initial log")
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    mock_hub = MagicMock(spec=HubClient)
    dummy_getter = MagicMock(return_value=[{"name": "Alice", "ping_ms": 12.0}])

    manager = HostWorkersManager(
        log_path=log_file,
        saves_dir=saves_dir,
        hub_client=mock_hub,
        session_token="session_test",
        guests_metrics_getter=dummy_getter,
    )

    assert manager.heartbeat.guests_metrics_getter is dummy_getter
