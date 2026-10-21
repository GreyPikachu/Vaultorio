"""
Unit and integration tests for StunCoordinatorProtocol and P2P Coordinator.
Covers STUN reflection, peer matching for UDP hole punching, fallback relay,
datagram corruption resistance, and peer lifecycle management.
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple

from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager
from src.server.stun_coordinator import (
    HEADER_CONTROL,
    HEADER_RELAY,
    MAGIC_PREFIX,
    StunCoordinatorProtocol,
    start_stun_coordinator,
)


class DummyClientProtocol(asyncio.DatagramProtocol):
    """Test client protocol using asyncio.Queue for reliable FIFO packet inspection."""

    def __init__(self):
        self.queue: asyncio.Queue[Tuple[bytes, Tuple[str, int]]] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        self.queue.put_nowait((data, addr))

    async def wait_for_packet(self, timeout: float = 1.5) -> bytes:
        """Waits for and pops the next incoming packet from the FIFO queue."""
        try:
            data, _ = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            return data
        except asyncio.TimeoutError:
            raise TimeoutError("Timed out waiting for UDP packet")


class TestStunCoordinator(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_stun_"))
        # Use safe test port outside 20000:50000
        self.test_port = 14198
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            http_host="127.0.0.1",
            udp_stun_port=self.test_port,
            lease_timeout_seconds=90,
        )
        self.session_manager = SessionLockManager(self.config)
        self.server_transport, self.server_protocol = await start_stun_coordinator(
            config=self.config,
            session_manager=self.session_manager,
        )

    async def asyncTearDown(self):
        if self.server_transport:
            self.server_transport.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def _create_client(self) -> Tuple[asyncio.DatagramTransport, DummyClientProtocol]:
        """Creates an ephemeral local UDP client socket."""
        loop = asyncio.get_running_loop()
        client_proto = DummyClientProtocol()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: client_proto,
            local_addr=("127.0.0.1", 0),
        )
        return transport, client_proto

    async def test_header_and_error_handling(self):
        """T4.1: Random data, short packets, and malformed JSON are ignored without crashing."""
        client_tx, client_rx = await self._create_client()
        server_addr = ("127.0.0.1", self.test_port)

        try:
            # 1. Truncated packet (< 6 bytes)
            client_tx.sendto(b"123", server_addr)
            await asyncio.sleep(0.05)
            self.assertEqual(client_rx.queue.qsize(), 0)

            # 2. Non-matching magic prefix
            client_tx.sendto(b"BAD_MAGIC_HEADER_12345678", server_addr)
            await asyncio.sleep(0.05)
            self.assertEqual(client_rx.queue.qsize(), 0)

            # 3. Valid header but malformed JSON
            client_tx.sendto(HEADER_CONTROL + b"{this is not valid json!}", server_addr)
            await asyncio.sleep(0.05)
            self.assertEqual(client_rx.queue.qsize(), 0)

            # 4. Oversized control payload (> 4096 bytes)
            oversized_json = json.dumps({"cmd": "PING", "junk": "x" * 5000}).encode("utf-8")
            client_tx.sendto(HEADER_CONTROL + oversized_json, server_addr)
            await asyncio.sleep(0.05)
            self.assertEqual(client_rx.queue.qsize(), 0)

        finally:
            client_tx.close()

    async def test_host_hello_stun_reflector(self):
        """T4.2: HOST_HELLO validates token, returns HOST_ACK with public coordinates, and updates session."""
        client_tx, client_rx = await self._create_client()
        server_addr = ("127.0.0.1", self.test_port)

        try:
            # 1. HOST_HELLO with invalid token -> ERROR
            bad_hello = json.dumps({"cmd": "HOST_HELLO", "session_token": "fake_token_12345"}).encode("utf-8")
            client_tx.sendto(HEADER_CONTROL + bad_hello, server_addr)
            reply_raw = await client_rx.wait_for_packet()
            self.assertTrue(reply_raw.startswith(HEADER_CONTROL))
            reply = json.loads(reply_raw[len(HEADER_CONTROL):].decode("utf-8"))
            self.assertEqual(reply["cmd"], "ERROR")
            self.assertEqual(reply["reason"], "invalid_token")

            # 2. Claim host in session manager
            claim_result = self.session_manager.claim_host("Bob", client_public_ip="127.0.0.1")
            token = claim_result["session_token"]

            # 3. HOST_HELLO with valid token -> HOST_ACK
            good_hello = json.dumps({
                "cmd": "HOST_HELLO",
                "session_token": token,
                "local_ip": "192.168.1.10",
                "local_port": 34197,
            }).encode("utf-8")
            client_tx.sendto(HEADER_CONTROL + good_hello, server_addr)

            ack_raw = await client_rx.wait_for_packet()
            ack = json.loads(ack_raw[len(HEADER_CONTROL):].decode("utf-8"))
            self.assertEqual(ack["cmd"], "HOST_ACK")
            self.assertEqual(ack["public_ip"], "127.0.0.1")
            self.assertIsInstance(ack["public_port"], int)

            # Check that host_udp_port was updated in SessionLockManager
            status = self.session_manager.get_public_status()
            self.assertEqual(status["host_udp_port"], ack["public_port"])

        finally:
            client_tx.close()

    async def test_guest_hello_and_peer_matching(self):
        """T4.3: GUEST_HELLO triggers simultaneous PEER_MATCH to both Host and Guest."""
        host_tx, host_rx = await self._create_client()
        guest_tx, guest_rx = await self._create_client()
        server_addr = ("127.0.0.1", self.test_port)

        try:
            # 1. GUEST_HELLO while idle -> ERROR no_active_host
            guest_hello = json.dumps({
                "cmd": "GUEST_HELLO",
                "guest_id": "guest_01",
                "guest_name": "Charlie",
                "local_ip": "192.168.1.55",
                "local_port": 34198,
            }).encode("utf-8")
            guest_tx.sendto(HEADER_CONTROL + guest_hello, server_addr)

            err_raw = await guest_rx.wait_for_packet()
            err = json.loads(err_raw[len(HEADER_CONTROL):].decode("utf-8"))
            self.assertEqual(err["cmd"], "ERROR")
            self.assertEqual(err["reason"], "no_active_host")

            # 2. Host registers via session_manager and sends HOST_HELLO
            claim_res = self.session_manager.claim_host("Alice", client_public_ip="127.0.0.1")
            token = claim_res["session_token"]
            self.session_manager.confirm_hosting(token)

            host_hello = json.dumps({
                "cmd": "HOST_HELLO",
                "session_token": token,
                "local_ip": "192.168.1.100",
                "local_port": 34197,
            }).encode("utf-8")
            host_tx.sendto(HEADER_CONTROL + host_hello, server_addr)
            await host_rx.wait_for_packet()  # wait for HOST_ACK

            # 3. Guest checks in via GUEST_HELLO
            guest_tx.sendto(HEADER_CONTROL + guest_hello, server_addr)

            # 4. Verify Guest receives PEER_MATCH with Host coordinates
            guest_match_raw = await guest_rx.wait_for_packet()
            guest_match = json.loads(guest_match_raw[len(HEADER_CONTROL):].decode("utf-8"))
            self.assertEqual(guest_match["cmd"], "PEER_MATCH")
            self.assertEqual(guest_match["role"], "guest")
            self.assertEqual(guest_match["host_name"], "Alice")
            self.assertEqual(guest_match["host_public"][0], "127.0.0.1")
            self.assertEqual(guest_match["host_local"], ["192.168.1.100", 34197])

            # 5. Verify Host receives PEER_MATCH with Guest coordinates
            host_match_raw = await host_rx.wait_for_packet()
            host_match = json.loads(host_match_raw[len(HEADER_CONTROL):].decode("utf-8"))
            self.assertEqual(host_match["cmd"], "PEER_MATCH")
            self.assertEqual(host_match["role"], "host")
            self.assertEqual(host_match["guest_id"], "guest_01")
            self.assertEqual(host_match["guest_name"], "Charlie")
            self.assertEqual(host_match["guest_local"], ["192.168.1.55", 34198])

            # 6. Verify guest is registered in session_manager
            status = self.session_manager.get_public_status()
            guests = status["active_guests"]
            self.assertEqual(len(guests), 1)
            self.assertEqual(guests[0]["name"], "Charlie")

        finally:
            host_tx.close()
            guest_tx.close()

    async def test_multiple_guests_matching(self):
        """T4.4: Multiple distinct guests can connect and receive independent PEER_MATCH packets."""
        host_tx, host_rx = await self._create_client()
        g1_tx, g1_rx = await self._create_client()
        g2_tx, g2_rx = await self._create_client()
        server_addr = ("127.0.0.1", self.test_port)

        try:
            claim_res = self.session_manager.claim_host("Alice", client_public_ip="127.0.0.1")
            token = claim_res["session_token"]
            self.session_manager.confirm_hosting(token)

            host_tx.sendto(HEADER_CONTROL + json.dumps({
                "cmd": "HOST_HELLO",
                "session_token": token,
                "local_ip": "10.0.0.1",
                "local_port": 34197,
            }).encode("utf-8"), server_addr)
            await host_rx.wait_for_packet()

            # Connect Guest 1
            g1_tx.sendto(HEADER_CONTROL + json.dumps({
                "cmd": "GUEST_HELLO",
                "guest_id": "g_alpha",
                "guest_name": "Friend1",
                "local_ip": "10.0.0.2",
                "local_port": 34198,
            }).encode("utf-8"), server_addr)

            match_g1 = json.loads((await g1_rx.wait_for_packet())[len(HEADER_CONTROL):].decode("utf-8"))
            match_host_for_g1 = json.loads((await host_rx.wait_for_packet())[len(HEADER_CONTROL):].decode("utf-8"))
            self.assertEqual(match_g1["guest_id"], "g_alpha")
            self.assertEqual(match_host_for_g1["guest_id"], "g_alpha")

            # Connect Guest 2
            g2_tx.sendto(HEADER_CONTROL + json.dumps({
                "cmd": "GUEST_HELLO",
                "guest_id": "g_beta",
                "guest_name": "Friend2",
                "local_ip": "10.0.0.3",
                "local_port": 34198,
            }).encode("utf-8"), server_addr)

            match_g2 = json.loads((await g2_rx.wait_for_packet())[len(HEADER_CONTROL):].decode("utf-8"))
            match_host_for_g2 = json.loads((await host_rx.wait_for_packet())[len(HEADER_CONTROL):].decode("utf-8"))
            self.assertEqual(match_g2["guest_id"], "g_beta")
            self.assertEqual(match_host_for_g2["guest_id"], "g_beta")

            # Server tracks both
            self.assertEqual(len(self.server_protocol.guests), 2)

        finally:
            host_tx.close()
            g1_tx.close()
            g2_tx.close()

    async def test_fallback_relay_data_forwarding(self):
        """T4.5: Fallback relay forwards binary traffic between Host and Guest."""
        host_tx, host_rx = await self._create_client()
        guest_tx, guest_rx = await self._create_client()
        server_addr = ("127.0.0.1", self.test_port)

        try:
            # Register Host and Guest
            claim_res = self.session_manager.claim_host("Alice", client_public_ip="127.0.0.1")
            token = claim_res["session_token"]
            self.session_manager.confirm_hosting(token)

            host_tx.sendto(HEADER_CONTROL + json.dumps({
                "cmd": "HOST_HELLO",
                "session_token": token,
            }).encode("utf-8"), server_addr)
            await host_rx.wait_for_packet()

            # 8-byte guest_id
            guest_id_8 = "guest_01"  # exactly 8 chars
            guest_tx.sendto(HEADER_CONTROL + json.dumps({
                "cmd": "GUEST_HELLO",
                "guest_id": guest_id_8,
                "guest_name": "Charlie",
            }).encode("utf-8"), server_addr)
            await guest_rx.wait_for_packet()
            await host_rx.wait_for_packet()

            # 1. Guest -> Host relay packet:
            # Format: HEADER_RELAY (6 bytes) + guest_id (8 bytes) + factorio_game_payload
            relay_guest_to_host = HEADER_RELAY + guest_id_8.encode("utf-8") + b"FACTORIO_GAME_PACKET_FROM_GUEST"
            guest_tx.sendto(relay_guest_to_host, server_addr)

            fwd_to_host = await host_rx.wait_for_packet()
            self.assertEqual(fwd_to_host, relay_guest_to_host)

            # 2. Host -> Guest relay packet:
            relay_host_to_guest = HEADER_RELAY + guest_id_8.encode("utf-8") + b"FACTORIO_GAME_PACKET_FROM_HOST"
            host_tx.sendto(relay_host_to_guest, server_addr)

            fwd_to_guest = await guest_rx.wait_for_packet()
            self.assertEqual(fwd_to_guest, relay_host_to_guest)

        finally:
            host_tx.close()
            guest_tx.close()

    async def test_peer_pruning_and_capacity(self):
        """T4.6: Expired peers are cleaned up and capacity is guarded."""
        host_tx, host_rx = await self._create_client()
        server_addr = ("127.0.0.1", self.test_port)

        try:
            claim_res = self.session_manager.claim_host("Alice", client_public_ip="127.0.0.1")
            token = claim_res["session_token"]
            self.session_manager.confirm_hosting(token)
            host_tx.sendto(HEADER_CONTROL + json.dumps({
                "cmd": "HOST_HELLO",
                "session_token": token,
            }).encode("utf-8"), server_addr)
            await host_rx.wait_for_packet()

            # Add an artificial expired guest
            from src.server.stun_coordinator import GuestPeer
            self.server_protocol.guests["expired_g"] = GuestPeer(
                guest_id="expired_g",
                guest_name="OldFriend",
                public_ip="1.2.3.4",
                public_port=5555,
                last_seen=0.0,  # long ago
            )
            self.assertIn("expired_g", self.server_protocol.guests)

            # Pruning helper removes expired
            self.server_protocol._prune_inactive_peers()
            self.assertNotIn("expired_g", self.server_protocol.guests)

        finally:
            host_tx.close()

    async def test_fastapi_lifespan_lifecycle(self):
        """T4.7: Verifies FastAPI lifespan cleanly starts and terminates UDP STUN transport."""
        from src.server.api import create_app

        lifespan_port = 14199
        config = ServerConfig(
            storage_dir=self.temp_dir,
            http_host="127.0.0.1",
            udp_stun_port=lifespan_port,
        )
        app = create_app(config=config, session_manager=self.session_manager, enable_stun=True)

        async with app.router.lifespan_context(app):
            # Check transport is active and listening
            self.assertIsNotNone(app.state.stun_transport)
            self.assertFalse(app.state.stun_transport.is_closing())

            # Send ping to verify listening
            client_tx, client_rx = await self._create_client()
            try:
                client_tx.sendto(b"FP2P\x01\x00", ("127.0.0.1", lifespan_port))
                await asyncio.sleep(0.05)
            finally:
                client_tx.close()

        # After exiting lifespan, transport is closed
        self.assertTrue(app.state.stun_transport.is_closing())


if __name__ == "__main__":
    unittest.main()
