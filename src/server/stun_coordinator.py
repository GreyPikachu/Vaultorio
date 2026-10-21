"""
Asynchronous UDP STUN Coordinator and P2P Relay for Factorio («Сейф + Координатор»).
Listens on port 14197/udp (safely avoiding Hysteria 2 port hopping range 20000:50000/udp).
Provides external IP:port reflection, synchronized peer matching for UDP Hole Punching,
and low-overhead fallback relaying for symmetric NAT / CGNAT peers.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from src.server.config import ServerConfig
from src.server.logger import get_logger
from src.server.session_manager import SessionLockManager

logger = get_logger("stun_coordinator")

# Protocol constants
MAGIC_PREFIX: bytes = b"FP2P\x01"
TYPE_CONTROL: bytes = b"J"  # JSON control signaling
TYPE_RELAY: bytes = b"R"    # Fallback binary UDP relay

HEADER_CONTROL: bytes = MAGIC_PREFIX + TYPE_CONTROL  # 6 bytes
HEADER_RELAY: bytes = MAGIC_PREFIX + TYPE_RELAY      # 6 bytes

MAX_DATAGRAM_SIZE: int = 65507
MAX_CONTROL_PAYLOAD: int = 4096
PEER_EXPIRATION_SECONDS: float = 60.0
MAX_GUESTS: int = 16


@dataclass
class HostPeer:
    """Active host endpoint details."""

    public_ip: str
    public_port: int
    local_ip: Optional[str] = None
    local_port: Optional[int] = None
    last_seen: float = field(default_factory=time.time)


@dataclass
class GuestPeer:
    """Active guest endpoint details."""

    guest_id: str
    guest_name: str
    public_ip: str
    public_port: int
    local_ip: Optional[str] = None
    local_port: Optional[int] = None
    last_seen: float = field(default_factory=time.time)


class StunCoordinatorProtocol(asyncio.DatagramProtocol):
    """
    Asyncio DatagramProtocol implementing STUN reflection,
    hole punching signaling, and fallback UDP relay.
    """

    def __init__(self, config: ServerConfig, session_manager: SessionLockManager) -> None:
        """
        Initializes the STUN coordinator protocol.

        Args:
            config: Server configuration settings.
            session_manager: Active SessionLockManager for verifying host tokens.
        """
        self.config = config
        self.session_manager = session_manager
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.host_peer: Optional[HostPeer] = None
        self.guests: Dict[str, GuestPeer] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Called when UDP socket is opened."""
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("socket")
        sockname = sock.getsockname() if sock else (self.config.http_host, self.config.udp_stun_port)
        logger.info("UDP STUN Coordinator listening on %s:%s", sockname[0], sockname[1])

    def connection_lost(self, exc: Optional[Exception]) -> None:
        """Called when UDP socket is closed."""
        if exc:
            logger.warning("UDP STUN Coordinator transport connection lost with error: %s", exc)
        else:
            logger.info("UDP STUN Coordinator transport closed cleanly.")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        """
        Entry point for every incoming UDP datagram.
        Guaranteed to never raise unhandled exceptions or crash the event loop.
        """
        try:
            # 1. Basic sanity checks
            if len(data) < 6:
                return

            if not data.startswith(MAGIC_PREFIX):
                # Ignore random noise / scanner probes without logging overhead
                return

            msg_type = data[5:6]

            # 2. Control Signaling (JSON)
            if msg_type == TYPE_CONTROL:
                self._handle_control_packet(data[6:], addr)

            # 3. Fallback UDP Relay (Binary)
            elif msg_type == TYPE_RELAY:
                self._handle_relay_packet(data, addr)

        except Exception as exc:
            logger.error("Unexpected error handling datagram from %s: %s", addr, exc, exc_info=True)

    def _handle_control_packet(self, payload_bytes: bytes, addr: Tuple[str, int]) -> None:
        """Parses and dispatches JSON control signaling packets."""
        if len(payload_bytes) > MAX_CONTROL_PAYLOAD:
            logger.warning("Dropped oversized control packet (%d bytes) from %s", len(payload_bytes), addr)
            return

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception as json_err:
            logger.warning("Malformed JSON in control packet from %s: %s", addr, json_err)
            return

        if not isinstance(payload, dict):
            return

        cmd = payload.get("cmd")

        if cmd == "HOST_HELLO":
            self._handle_host_hello(payload, addr)
        elif cmd == "GUEST_HELLO":
            self._handle_guest_hello(payload, addr)
        else:
            logger.debug("Unknown control command '%s' from %s", cmd, addr)

    def _handle_host_hello(self, payload: Dict[str, Any], addr: Tuple[str, int]) -> None:
        """
        Handles HOST_HELLO: registers/refreshes host UDP endpoint and echoes public IP:port.
        """
        token = payload.get("session_token")
        if not token or not self.session_manager.validate_host_token(token):
            logger.warning("HOST_HELLO rejected from %s: invalid session_token", addr)
            self._send_control({"cmd": "ERROR", "reason": "invalid_token"}, addr)
            return

        now = time.time()
        local_ip = payload.get("local_ip")
        local_port = payload.get("local_port")

        self.host_peer = HostPeer(
            public_ip=addr[0],
            public_port=addr[1],
            local_ip=local_ip,
            local_port=local_port,
            last_seen=now,
        )

        # Update host port in session manager
        with self.session_manager._lock:
            self.session_manager._session.host_udp_port = addr[1]
            self.session_manager._session.host_public_ip = addr[0]

        logger.info(
            "Registered active Host STUN endpoint: %s:%d (local: %s:%s)",
            addr[0],
            addr[1],
            local_ip,
            local_port,
        )

        # Echo back observed public coordinates to the Host
        self._send_control(
            {
                "cmd": "HOST_ACK",
                "public_ip": addr[0],
                "public_port": addr[1],
            },
            addr,
        )

    def _handle_guest_hello(self, payload: Dict[str, Any], addr: Tuple[str, int]) -> None:
        """
        Handles GUEST_HELLO: checks active host, registers guest,
        and broadcasts PEER_MATCH to both Host and Guest for simultaneous UDP Hole Punching.
        """
        # 1. Ensure server currently has an active host
        session_status = self.session_manager.get_public_status()
        state = session_status.get("state")

        if state not in ("hosting_active", "claiming") or not self.host_peer:
            logger.info("GUEST_HELLO from %s rejected: no active host (state: %s)", addr, state)
            self._send_control({"cmd": "ERROR", "reason": "no_active_host"}, addr)
            return

        # 2. Prune expired guest peers
        self._prune_inactive_peers()

        raw_guest_id = str(payload.get("guest_id", ""))
        guest_id = raw_guest_id.strip()[:16]
        if not guest_id:
            # Fallback to deterministic pseudo-id from addr
            guest_id = f"g_{addr[0]}_{addr[1]}"[:16]

        guest_name = str(payload.get("guest_name", "Guest")).strip()[:32]
        local_ip = payload.get("local_ip")
        local_port = payload.get("local_port")

        # 3. Capacity limit check
        if len(self.guests) >= MAX_GUESTS and guest_id not in self.guests:
            logger.warning("GUEST_HELLO rejected from %s: max guest limit (%d) reached", addr, MAX_GUESTS)
            self._send_control({"cmd": "ERROR", "reason": "max_peers_exceeded"}, addr)
            return

        # 4. Register guest peer
        self.guests[guest_id] = GuestPeer(
            guest_id=guest_id,
            guest_name=guest_name,
            public_ip=addr[0],
            public_port=addr[1],
            local_ip=local_ip,
            local_port=local_port,
            last_seen=time.time(),
        )

        # Register in session manager
        try:
            self.session_manager.register_guest(
                guest_name=guest_name,
                guest_public_ip=addr[0],
                guest_udp_port=addr[1],
            )
        except Exception as reg_err:
            logger.warning("Failed to register guest in session manager: %s", reg_err)

        logger.info(
            "Guest '%s' (id: %s) checked in from %s:%d. Coordinating PEER_MATCH with Host %s:%d.",
            guest_name,
            guest_id,
            addr[0],
            addr[1],
            self.host_peer.public_ip,
            self.host_peer.public_port,
        )

        # 5. Send PEER_MATCH to Guest (containing Host coordinates)
        guest_match_payload = {
            "cmd": "PEER_MATCH",
            "role": "guest",
            "session_id": session_status.get("session_id"),
            "host_name": session_status.get("host_name"),
            "host_public": [self.host_peer.public_ip, self.host_peer.public_port],
            "host_local": [self.host_peer.local_ip, self.host_peer.local_port],
            "guest_id": guest_id,
            "guest_public": [addr[0], addr[1]],
            "guest_local": [local_ip, local_port],
        }
        self._send_control(guest_match_payload, addr)

        # 6. Send PEER_MATCH to Host (containing Guest coordinates for simultaneous punch)
        host_match_payload = {
            "cmd": "PEER_MATCH",
            "role": "host",
            "session_id": session_status.get("session_id"),
            "guest_id": guest_id,
            "guest_name": guest_name,
            "guest_public": [addr[0], addr[1]],
            "guest_local": [local_ip, local_port],
        }
        self._send_control(host_match_payload, (self.host_peer.public_ip, self.host_peer.public_port))

    def _handle_relay_packet(self, data: bytes, addr: Tuple[str, int]) -> None:
        """
        Handles Fallback UDP Relay:
        Structure: b"FP2P\x01R" (6 bytes) + guest_id (8 bytes ASCII) + raw_factorio_payload
        Total header size: 14 bytes.
        """
        if len(data) < 14:
            return

        if not self.transport:
            return

        guest_id_raw = data[6:14].decode("utf-8", errors="replace").strip()

        # Case A: Packet from Host -> Forward to Guest
        if (
            self.host_peer
            and addr[0] == self.host_peer.public_ip
            and addr[1] == self.host_peer.public_port
        ):
            target_guest = self.guests.get(guest_id_raw)
            if target_guest:
                try:
                    self.transport.sendto(data, (target_guest.public_ip, target_guest.public_port))
                except Exception as send_err:
                    logger.debug("Relay host->guest send error: %s", send_err)
            return

        # Case B: Packet from Guest -> Forward to Host
        # Update guest last_seen and public address
        if guest_id_raw in self.guests:
            g = self.guests[guest_id_raw]
            g.last_seen = time.time()
            g.public_ip = addr[0]
            g.public_port = addr[1]

        if self.host_peer:
            try:
                self.transport.sendto(data, (self.host_peer.public_ip, self.host_peer.public_port))
            except Exception as send_err:
                logger.debug("Relay guest->host send error: %s", send_err)

    def _send_control(self, cmd_dict: Dict[str, Any], addr: Tuple[str, int]) -> None:
        """Encodes and transmits a JSON control datagram to target address."""
        if not self.transport:
            return

        try:
            body = json.dumps(cmd_dict, ensure_ascii=False).encode("utf-8")
            packet = HEADER_CONTROL + body
            self.transport.sendto(packet, addr)
        except Exception as exc:
            logger.warning("Failed to send control packet to %s: %s", addr, exc)

    def _prune_inactive_peers(self) -> None:
        """Removes guest peers that haven't sent keepalive packets in 60 seconds."""
        now = time.time()
        expired = [
            gid
            for gid, guest in self.guests.items()
            if now - guest.last_seen > PEER_EXPIRATION_SECONDS
        ]
        for gid in expired:
            del self.guests[gid]

        if expired:
            logger.debug("Pruned %d expired guest peers from STUN table", len(expired))

    def close(self) -> None:
        """Closes the underlying UDP transport."""
        if self.transport:
            self.transport.close()
            self.transport = None


async def start_stun_coordinator(
    config: ServerConfig,
    session_manager: SessionLockManager,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Tuple[asyncio.DatagramTransport, StunCoordinatorProtocol]:
    """
    Factory helper to initialize and bind the UDP STUN coordinator socket.

    Args:
        config: Validated ServerConfig instance.
        session_manager: SessionLockManager instance.
        loop: Optional running event loop.

    Returns:
        Tuple of (DatagramTransport, StunCoordinatorProtocol).
    """
    active_loop = loop or asyncio.get_running_loop()

    transport, protocol = await active_loop.create_datagram_endpoint(
        lambda: StunCoordinatorProtocol(config, session_manager),
        local_addr=(config.http_host, config.udp_stun_port),
    )

    return transport, protocol  # type: ignore[return-value]
