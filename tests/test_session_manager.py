"""
Unit and integration tests for SessionLockManager.
Validates the 4-phase state machine, host lease claiming, timeouts,
token authorization, and guest tracking.
"""

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager, SessionState


class TestSessionLockManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_factorio_session_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            lease_timeout_seconds=90,
        )
        self.manager = SessionLockManager(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initial_state_is_idle(self):
        status = self.manager.get_public_status()
        self.assertEqual(status["state"], SessionState.IDLE.value)
        self.assertIsNone(status["host_name"])
        self.assertIsNone(status["host_public_ip"])
        self.assertIsNone(status["host_udp_port"])
        self.assertEqual(status["active_guests"], [])

    def test_claim_host_success_from_idle(self):
        res = self.manager.claim_host(
            player_name="Alex",
            client_public_ip="198.51.100.1",
            client_local_ip="192.168.1.50",
        )
        self.assertTrue(res["success"])
        self.assertFalse(res["reconnected"])
        self.assertTrue(isinstance(res["session_token"], str))
        self.assertGreater(len(res["session_token"]), 20)

        status = self.manager.get_public_status()
        self.assertEqual(status["state"], SessionState.CLAIMING.value)
        self.assertEqual(status["host_name"], "Alex")
        self.assertEqual(status["host_public_ip"], "198.51.100.1")
        self.assertEqual(status["host_local_ip"], "192.168.1.50")
        self.assertGreater(status["lease_remaining_seconds"], 170.0)

    def test_claim_host_invalid_nicknames(self):
        invalid_names = [
            "",
            "   ",
            "A",  # too short (< 2)
            "A" * 33,  # too long (> 32)
            "Player",  # default forbidden
            "player",
            "PLAYER",
        ]
        for name in invalid_names:
            with self.subTest(name=name):
                res = self.manager.claim_host(name, "198.51.100.1")
                self.assertFalse(res["success"])
                self.assertIn("player_name", res.get("reason", "") + res.get("message", ""))

    def test_claim_host_already_hosted_rejection(self):
        # Host 1 claims slot
        res1 = self.manager.claim_host("Alex", "198.51.100.1")
        self.assertTrue(res1["success"])

        # Host 2 tries to claim while Host 1 is claiming
        res2 = self.manager.claim_host("Dima", "203.0.113.5")
        self.assertFalse(res2["success"])
        self.assertEqual(res2["reason"], "already_hosted")
        self.assertIn("Alex", res2["message"])

    def test_claim_host_idempotent_reconnect_same_host(self):
        res1 = self.manager.claim_host("Alex", "198.51.100.1", "192.168.1.50")
        token1 = res1["session_token"]

        # Same host reconnects from same IP
        res2 = self.manager.claim_host("Alex", "198.51.100.1", "192.168.1.50")
        self.assertTrue(res2["success"])
        self.assertTrue(res2["reconnected"])
        self.assertEqual(res2["session_token"], token1)

    def test_confirm_hosting_lifecycle(self):
        claim_res = self.manager.claim_host("Alex", "198.51.100.1")
        token = claim_res["session_token"]

        # Confirm hosting with UDP port
        confirm_res = self.manager.confirm_hosting(token, host_udp_port=34197)
        self.assertTrue(confirm_res["success"])

        status = self.manager.get_public_status()
        self.assertEqual(status["state"], SessionState.HOSTING_ACTIVE.value)
        self.assertEqual(status["host_udp_port"], 34197)
        self.assertGreater(status["lease_remaining_seconds"], 80.0)

    def test_confirm_hosting_invalid_token(self):
        self.manager.claim_host("Alex", "198.51.100.1")
        confirm_res = self.manager.confirm_hosting("invalid_token_123", host_udp_port=34197)
        self.assertFalse(confirm_res["success"])
        self.assertEqual(confirm_res["reason"], "invalid_token")

    def test_heartbeat_updates_lease(self):
        claim_res = self.manager.claim_host("Alex", "198.51.100.1")
        token = claim_res["session_token"]
        self.manager.confirm_hosting(token, host_udp_port=34197)

        initial_seen = self.manager._session.last_seen

        with patch("time.time", return_value=initial_seen + 20):
            hb_res = self.manager.heartbeat(token)
            self.assertTrue(hb_res["success"])
            self.assertEqual(self.manager._session.last_seen, initial_seen + 20)

    def test_heartbeat_invalid_token(self):
        claim_res = self.manager.claim_host("Alex", "198.51.100.1")
        token = claim_res["session_token"]
        self.manager.confirm_hosting(token)

        hb_res = self.manager.heartbeat("wrong_token")
        self.assertFalse(hb_res["success"])
        self.assertEqual(hb_res["reason"], "invalid_token")

    def test_begin_final_upload_race_protection(self):
        claim_res = self.manager.claim_host("Alex", "198.51.100.1")
        token = claim_res["session_token"]
        self.manager.confirm_hosting(token)

        # Host initiates final save upload
        upload_res = self.manager.begin_final_upload(token)
        self.assertTrue(upload_res["success"])

        status = self.manager.get_public_status()
        self.assertEqual(status["state"], SessionState.UPLOADING_FINAL.value)

        # Second player tries to claim while final save is uploading
        dima_res = self.manager.claim_host("Dima", "203.0.113.5")
        self.assertFalse(dima_res["success"])
        self.assertEqual(dima_res["reason"], "uploading_final")
        self.assertEqual(dima_res["retry_after_seconds"], 3)

    def test_release_host_normal_exit(self):
        claim_res = self.manager.claim_host("Alex", "198.51.100.1")
        token = claim_res["session_token"]
        self.manager.confirm_hosting(token)

        release_res = self.manager.release_host(token, reason="host_quit_cleanly")
        self.assertTrue(release_res["success"])

        status = self.manager.get_public_status()
        self.assertEqual(status["state"], SessionState.IDLE.value)
        self.assertIsNone(status["host_name"])

        # Next player can now immediately claim
        dima_res = self.manager.claim_host("Dima", "203.0.113.5")
        self.assertTrue(dima_res["success"])

    def test_validate_host_token(self):
        claim_res = self.manager.claim_host("Alex", "198.51.100.1")
        token = claim_res["session_token"]

        self.assertTrue(self.manager.validate_host_token(token))
        self.assertFalse(self.manager.validate_host_token("random_stranger_token"))
        self.assertFalse(self.manager.validate_host_token(None))
        self.assertFalse(self.manager.validate_host_token(""))

        # After release, token is no longer valid
        self.manager.release_host(token)
        self.assertFalse(self.manager.validate_host_token(token))

    def test_fake_host_timeout_expiration(self):
        """If claiming state exceeds 180 seconds without confirm_hosting, revert to idle."""
        base_time = 1000.0
        with patch("time.time", return_value=base_time):
            self.manager.claim_host("Alex", "198.51.100.1")
            self.assertEqual(self.manager._session.state, SessionState.CLAIMING.value)

        # 181 seconds later
        with patch("time.time", return_value=base_time + 181.0):
            status = self.manager.get_public_status()
            self.assertEqual(status["state"], SessionState.IDLE.value)
            self.assertIsNone(status["host_name"])

            # Another player can claim slot
            res = self.manager.claim_host("Dima", "203.0.113.5")
            self.assertTrue(res["success"])

    def test_hosting_active_timeout_expiration(self):
        """If host disappears (>90 sec without heartbeat), revert to idle."""
        base_time = 1000.0
        with patch("time.time", return_value=base_time):
            claim_res = self.manager.claim_host("Alex", "198.51.100.1")
            token = claim_res["session_token"]
            self.manager.confirm_hosting(token)
            self.assertEqual(self.manager._session.state, SessionState.HOSTING_ACTIVE.value)

        # 95 seconds later without heartbeat
        with patch("time.time", return_value=base_time + 95.0):
            status = self.manager.get_public_status()
            self.assertEqual(status["state"], SessionState.IDLE.value)
            self.assertIsNone(status["host_name"])

    def test_uploading_final_timeout_expiration(self):
        """If final upload has no activity longer than FINAL_UPLOAD_TTL_SECONDS, revert to idle."""
        base_time = 1000.0
        with patch("time.time", return_value=base_time):
            claim_res = self.manager.claim_host("Alex", "198.51.100.1")
            token = claim_res["session_token"]
            self.manager.confirm_hosting(token)
            self.manager.begin_final_upload(token)
            self.assertEqual(self.manager._session.state, SessionState.UPLOADING_FINAL.value)

        # 46 seconds later: with 300s TTL, session is STILL uploading_final (large files don't abort)
        with patch("time.time", return_value=base_time + 46.0):
            status = self.manager.get_public_status()
            self.assertEqual(status["state"], SessionState.UPLOADING_FINAL.value)

        # Exceeding full TTL without activity: reverts to idle
        with patch("time.time", return_value=base_time + self.manager.FINAL_UPLOAD_TTL_SECONDS + 1.0):
            status = self.manager.get_public_status()
            self.assertEqual(status["state"], SessionState.IDLE.value)

    def test_uploading_final_sliding_activity_timeout(self):
        """Activity updates (touch_activity) extend the lease and prevent expiration."""
        base_time = 1000.0
        with patch("time.time", return_value=base_time):
            claim_res = self.manager.claim_host("Alex", "198.51.100.1")
            token = claim_res["session_token"]
            self.manager.confirm_hosting(token)
            self.manager.begin_final_upload(token)

        # Activity touches every 200 seconds up to 500 seconds total
        for t in (1200.0, 1400.0, 1500.0):
            with patch("time.time", return_value=t):
                touched = self.manager.touch_activity(token)
                self.assertTrue(touched)
                status = self.manager.get_public_status()
                self.assertEqual(status["state"], SessionState.UPLOADING_FINAL.value)

        # After 1500 + TTL + 1s of total silence, it finally expires
        with patch("time.time", return_value=1500.0 + self.manager.FINAL_UPLOAD_TTL_SECONDS + 1.0):
            status = self.manager.get_public_status()
            self.assertEqual(status["state"], SessionState.IDLE.value)

    def test_guest_registration_and_deregistration(self):
        claim_res = self.manager.claim_host("Alex", "198.51.100.1")
        token = claim_res["session_token"]
        self.manager.confirm_hosting(token)

        # Register guests
        self.manager.register_guest("Dima", "203.0.113.5", guest_udp_port=34198)
        self.manager.register_guest("Ivan", "198.51.100.77", guest_udp_port=34199)

        status = self.manager.get_public_status()
        self.assertEqual(len(status["active_guests"]), 2)
        guest_names = [g["name"] for g in status["active_guests"]]
        self.assertIn("Dima", guest_names)
        self.assertIn("Ivan", guest_names)

        # Deregister Ivan
        self.manager.deregister_guest("Ivan")
        status2 = self.manager.get_public_status()
        self.assertEqual(len(status2["active_guests"]), 1)
        self.assertEqual(status2["active_guests"][0]["name"], "Dima")

    def test_persistence_across_manager_restarts(self):
        claim_res = self.manager.claim_host("Alex", "198.51.100.1", "192.168.1.10")
        token = claim_res["session_token"]
        self.manager.confirm_hosting(token, host_udp_port=34197)

        # Create a new manager instance pointing to the same storage_dir
        new_manager = SessionLockManager(self.config)
        new_status = new_manager.get_public_status()

        self.assertEqual(new_status["state"], SessionState.HOSTING_ACTIVE.value)
        self.assertEqual(new_status["host_name"], "Alex")
        self.assertEqual(new_status["host_public_ip"], "198.51.100.1")
        self.assertEqual(new_status["host_udp_port"], 34197)
        self.assertTrue(new_manager.validate_host_token(token))


if __name__ == "__main__":
    unittest.main()
