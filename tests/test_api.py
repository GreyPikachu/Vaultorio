"""
Integration and functional tests for Factorio Safe HTTP REST API.
Verifies session state transitions, Split-Brain protection, atomic save upload/download,
and mod manifest synchronization via FastAPI TestClient.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.server.api import create_app
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager
from src.server.storage import SaveStorageManager


class TestServerAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_factorio_api_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            max_backups=3,
            max_upload_size_bytes=5 * 1024 * 1024,  # 5 MB
            http_port=8088,
            udp_stun_port=14197,
            lease_timeout_seconds=90,
        )
        self.storage = SaveStorageManager(self.config)
        self.session_manager = SessionLockManager(self.config)
        self.app = create_app(
            config=self.config,
            storage=self.storage,
            session_manager=self.session_manager,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_valid_factorio_zip(self, file_path: Path, marker: str = "world_v1") -> Path:
        """Creates a syntactically valid Factorio save archive for testing."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(file_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("test_world/level.dat", f"Binary Level Data: {marker}".encode("utf-8") * 5)
            zf.writestr("test_world/info.json", json.dumps({"name": marker, "version": "2.0"}))
            zf.writestr("test_world/control.lua", "-- Lua script logic\n")
        return file_path

    def _compute_sha256(self, file_path: Path) -> str:
        """Calculates SHA-256 for test verification."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(64 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()

    def test_health_check(self):
        """T3.1: GET /health returns 200 OK and valid daemon info."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "factorio-safe")
        self.assertIn("timestamp", data)

    def test_initial_status_idle(self):
        """T3.1: GET /status initially returns idle state and save_exists=False."""
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["state"], "idle")
        self.assertIsNone(data["host_name"])
        self.assertFalse(data["save_exists"])
        self.assertIn("session", data)
        self.assertIn("save", data)

    def test_claim_lifecycle_and_validation(self):
        """T3.2: POST /claim validates player name and reserves host slot."""
        # 1. Invalid nickname: too short (rejected by Pydantic 422 or business logic 400)
        res_short = self.client.post("/claim", json={"player_name": "A"})
        self.assertIn(res_short.status_code, (400, 422))

        # 2. Invalid nickname: default 'Player' is rejected
        res_player = self.client.post("/claim", json={"player_name": "Player"})
        self.assertEqual(res_player.status_code, 400)
        self.assertEqual(res_player.json()["reason"], "invalid_player_name")

        # 3. Successful claim by Bob
        res_bob = self.client.post("/claim", json={"player_name": "Bob", "local_ip": "192.168.1.50"})
        self.assertEqual(res_bob.status_code, 200)
        bob_data = res_bob.json()
        self.assertTrue(bob_data["success"])
        self.assertIsNotNone(bob_data["session_token"])
        bob_token = bob_data["session_token"]

        # Check status updated
        status = self.client.get("/status").json()
        self.assertEqual(status["state"], "claiming")
        self.assertEqual(status["host_name"], "Bob")

        # 4. Competing claim by Alice is rejected
        res_alice = self.client.post("/claim", json={"player_name": "Alice"})
        self.assertEqual(res_alice.status_code, 200)
        alice_data = res_alice.json()
        self.assertFalse(alice_data["success"])
        self.assertEqual(alice_data["reason"], "already_hosted")

        # 5. Idempotent reconnect from Bob returns existing token
        res_bob_reconnect = self.client.post("/claim", json={"player_name": "Bob"})
        self.assertEqual(res_bob_reconnect.status_code, 200)
        reconnect_data = res_bob_reconnect.json()
        self.assertTrue(reconnect_data["success"])
        self.assertTrue(reconnect_data.get("reconnected"))
        self.assertEqual(reconnect_data["session_token"], bob_token)

    def test_confirm_hosting(self):
        """T3.2: POST /confirm-hosting transitions session from claiming to hosting_active."""
        # Claim slot
        res_claim = self.client.post("/claim", json={"player_name": "Bob"})
        token = res_claim.json()["session_token"]

        # 1. Confirm with invalid token returns 403 Forbidden
        res_bad = self.client.post("/confirm-hosting", json={"session_token": "fake_token_1234567890"})
        self.assertEqual(res_bad.status_code, 403)

        # 2. Confirm with valid token
        res_ok = self.client.post("/confirm-hosting", json={"session_token": token, "udp_port": 34197})
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.json()["success"])

        status = self.client.get("/status").json()
        self.assertEqual(status["state"], "hosting_active")
        self.assertEqual(status["host_udp_port"], 34197)

    def test_heartbeat_and_lease_renewal(self):
        """T3.3: POST /heartbeat extends lease for active host."""
        res_claim = self.client.post("/claim", json={"player_name": "Bob"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token, "udp_port": 34197})

        # 1. Heartbeat with bad token -> 403
        res_bad = self.client.post("/heartbeat", json={"session_token": "invalid_token_12345"})
        self.assertEqual(res_bad.status_code, 403)

        # 2. Heartbeat with valid token -> 200
        res_ok = self.client.post("/heartbeat", json={"session_token": token, "udp_port": 34199})
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.json()["success"])
        self.assertIn("active_guests", res_ok.json())

    def test_begin_final_upload_race_protection(self):
        """T3.3: POST /begin-final-upload sets uploading_final and gives retry_after_seconds=3 to competitors."""
        res_claim = self.client.post("/claim", json={"player_name": "Bob"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token})

        # 1. Bad token -> 403
        res_bad = self.client.post("/begin-final-upload", json={"session_token": "invalid_token_12345"})
        self.assertEqual(res_bad.status_code, 403)

        # 2. Valid token -> transitions to uploading_final
        res_ok = self.client.post("/begin-final-upload", json={"session_token": token})
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.json()["success"])

        status = self.client.get("/status").json()
        self.assertEqual(status["state"], "uploading_final")

        # 3. Another player attempting to claim gets uploading_final wait instruction
        res_claim_charlie = self.client.post("/claim", json={"player_name": "Charlie"})
        self.assertEqual(res_claim_charlie.status_code, 200)
        charlie_data = res_claim_charlie.json()
        self.assertFalse(charlie_data["success"])
        self.assertEqual(charlie_data["reason"], "uploading_final")
        self.assertEqual(charlie_data["retry_after_seconds"], 3)

    def test_split_brain_shield_unauthorized_upload(self):
        """T3.4: POST /upload-save strictly blocks unauthorized uploads with 403 Forbidden."""
        dummy_zip = self.temp_dir / "test_dummy.zip"
        self._create_valid_factorio_zip(dummy_zip, marker="unauthorized_test")

        # 1. Upload attempt with no token while idle -> 403
        with open(dummy_zip, "rb") as f:
            res_idle = self.client.post(
                "/upload-save",
                files={"file": ("cloud_world.zip", f, "application/zip")},
            )
        self.assertEqual(res_idle.status_code, 403)
        self.assertEqual(res_idle.json()["error"], "not_active_host")

        # Ensure cloud save file was NOT created on disk
        self.assertFalse(self.config.current_save_path.exists())

        # 2. Bob claims host, but an impostor tries with a forged token -> 403
        self.client.post("/claim", json={"player_name": "Bob"})
        with open(dummy_zip, "rb") as f:
            res_fake_token = self.client.post(
                "/upload-save",
                data={"session_token": "forged_secret_token_12345678"},
                files={"file": ("cloud_world.zip", f, "application/zip")},
            )
        self.assertEqual(res_fake_token.status_code, 403)
        self.assertEqual(res_fake_token.json()["error"], "not_active_host")
        self.assertFalse(self.config.current_save_path.exists())

    def test_upload_save_validation_failures(self):
        """T3.4: POST /upload-save rejects corrupt archives and mismatched SHA-256."""
        res_claim = self.client.post("/claim", json={"player_name": "Bob"})
        token = res_claim.json()["session_token"]

        # 1. Corrupt ZIP payload
        bad_zip = self.temp_dir / "bad.zip"
        bad_zip.write_bytes(b"NOT_A_ZIP_ARCHIVE_DATA" * 10)

        with open(bad_zip, "rb") as f:
            res_corrupt = self.client.post(
                "/upload-save",
                data={"session_token": token, "release_on_success": "false"},
                files={"file": ("cloud_world.zip", f, "application/zip")},
            )
        self.assertEqual(res_corrupt.status_code, 400)
        self.assertEqual(res_corrupt.json()["error"], "invalid_archive")

        # 2. Valid ZIP but mismatched expected SHA-256
        valid_zip = self.temp_dir / "valid.zip"
        self._create_valid_factorio_zip(valid_zip, marker="sha_mismatch")

        with open(valid_zip, "rb") as f:
            res_sha_fail = self.client.post(
                "/upload-save",
                data={
                    "session_token": token,
                    "expected_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
                    "release_on_success": "false",
                },
                files={"file": ("cloud_world.zip", f, "application/zip")},
            )
        self.assertEqual(res_sha_fail.status_code, 400)
        self.assertEqual(res_sha_fail.json()["error"], "sha256_mismatch")

    def test_upload_save_success_and_atomic_commit(self):
        """T3.4: Active host successfully uploads valid save, commits atomically and records mods."""
        res_claim = self.client.post("/claim", json={"player_name": "Bob"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token})
        self.client.post("/begin-final-upload", json={"session_token": token})

        valid_zip = self.temp_dir / "save_to_upload.zip"
        self._create_valid_factorio_zip(valid_zip, marker="factorio_base_v1")
        expected_sha = self._compute_sha256(valid_zip)

        mods_payload = [
            {"name": "base", "enabled": True},
            {"name": "space-age", "enabled": True},
        ]

        with open(valid_zip, "rb") as f:
            res_upload = self.client.post(
                "/upload-save",
                data={
                    "session_token": token,
                    "expected_sha256": expected_sha,
                    "mod_list_json": json.dumps(mods_payload),
                    "release_on_success": "true",
                },
                files={"file": ("cloud_world.zip", f, "application/zip")},
            )

        self.assertEqual(res_upload.status_code, 200)
        upload_data = res_upload.json()
        self.assertTrue(upload_data["success"])
        self.assertEqual(upload_data["save"]["sha256"], expected_sha)
        self.assertEqual(upload_data["save"]["uploaded_by"], "Bob")
        self.assertEqual(upload_data["save"]["version_counter"], 1)

        # Check session is now reset to idle due to release_on_success=True
        status = self.client.get("/status").json()
        self.assertEqual(status["state"], "idle")
        self.assertTrue(status["save_exists"])
        self.assertEqual(status["sha256"], expected_sha)

        # Check mod manifest
        mod_res = self.client.get("/mod-manifest")
        self.assertEqual(mod_res.status_code, 200)
        self.assertTrue(mod_res.json()["exists"])
        self.assertEqual(mod_res.json()["mod_list"], mods_payload)

    def test_download_save_and_caching(self):
        """T3.5: GET /download-save delivers binary archive and handles 304 Not Modified caching."""
        # 1. Download when no save exists -> 404
        res_404 = self.client.get("/download-save")
        self.assertEqual(res_404.status_code, 404)

        # 2. Upload a valid save first
        res_claim = self.client.post("/claim", json={"player_name": "Bob"})
        token = res_claim.json()["session_token"]
        valid_zip = self.temp_dir / "download_test_world.zip"
        self._create_valid_factorio_zip(valid_zip, marker="download_v1")
        actual_sha = self._compute_sha256(valid_zip)

        with open(valid_zip, "rb") as f:
            self.client.post(
                "/upload-save",
                data={"session_token": token, "release_on_success": "true"},
                files={"file": ("cloud_world.zip", f, "application/zip")},
            )

        # 3. Download full save without hash -> 200 OK
        res_download = self.client.get("/download-save")
        self.assertEqual(res_download.status_code, 200)
        self.assertEqual(res_download.headers.get("x-save-sha256"), actual_sha)
        self.assertIn("ETag", res_download.headers)
        self.assertEqual(len(res_download.content), valid_zip.stat().st_size)

        # 4. Download with matching client_sha256 query param -> 304 Not Modified
        res_cached_param = self.client.get(f"/download-save?client_sha256={actual_sha}")
        self.assertEqual(res_cached_param.status_code, 304)
        self.assertEqual(len(res_cached_param.content), 0)

        # 5. Download with matching If-None-Match header -> 304 Not Modified
        res_cached_header = self.client.get(
            "/download-save",
            headers={"If-None-Match": f'"{actual_sha}"'},
        )
        self.assertEqual(res_cached_header.status_code, 304)
        self.assertEqual(len(res_cached_header.content), 0)

        # 6. Download with non-matching hash -> 200 OK
        res_mismatch = self.client.get("/download-save?client_sha256=outdated_hash_123")
        self.assertEqual(res_mismatch.status_code, 200)
        self.assertEqual(len(res_mismatch.content), valid_zip.stat().st_size)

    def test_guest_registration_endpoints(self):
        """T3.6: POST /guests/register and /guests/deregister maintain guest roster."""
        res_claim = self.client.post("/claim", json={"player_name": "Bob"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token})

        # Register Guest1
        res_reg = self.client.post("/guests/register", json={"guest_name": "Guest1", "udp_port": 34198})
        self.assertEqual(res_reg.status_code, 200)
        self.assertTrue(res_reg.json()["success"])

        status = self.client.get("/status").json()
        guests = status["active_guests"]
        self.assertEqual(len(guests), 1)
        self.assertEqual(guests[0]["name"], "Guest1")

        # Deregister Guest1
        res_dereg = self.client.post("/guests/deregister", json={"guest_name": "Guest1"})
        self.assertEqual(res_dereg.status_code, 200)

        status_after = self.client.get("/status").json()
        self.assertEqual(len(status_after["active_guests"]), 0)

    def test_release_host_endpoint(self):
        """T3.7: POST /release frees the host slot back to idle."""
        res_claim = self.client.post("/claim", json={"player_name": "Bob"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token})

        # 1. Invalid token -> 403
        res_bad = self.client.post("/release", json={"session_token": "fake_token_12345"})
        self.assertEqual(res_bad.status_code, 403)

        # 2. Valid token -> 200, state resets to idle
        res_ok = self.client.post("/release", json={"session_token": token, "reason": "user_quit"})
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.json()["success"])

        status = self.client.get("/status").json()
        self.assertEqual(status["state"], "idle")
        self.assertIsNone(status["host_name"])

    def test_mod_manifest_explicit_endpoints(self):
        """Tests standalone GET and POST /mod-manifest endpoints."""
        # 1. Initially no mod manifest
        res_init = self.client.get("/mod-manifest")
        self.assertEqual(res_init.status_code, 200)
        self.assertFalse(res_init.json()["exists"])

        # 2. Post mod manifest
        test_mods = {"mods": [{"name": "base", "enabled": True}]}
        res_post = self.client.post("/mod-manifest", json={"mod_list": test_mods})
        self.assertEqual(res_post.status_code, 200)
        self.assertTrue(res_post.json()["success"])

        # 3. Retrieve updated mod manifest
        res_get = self.client.get("/mod-manifest")
        self.assertEqual(res_get.status_code, 200)
        self.assertTrue(res_get.json()["exists"])
        self.assertEqual(res_get.json()["mod_list"], test_mods)

    def test_upload_save_stream_activity_touch(self):
        """T9.1: Tests that /upload-save calls touch_activity via middleware and chunk loop."""
        # 1. Claim host and confirm hosting
        res_claim = self.client.post("/claim", json={"player_name": "Alice"})
        token = res_claim.json()["session_token"]
        self.client.post("/confirm-hosting", json={"session_token": token})

        # 2. Begin final upload
        self.session_manager.begin_final_upload(token)

        save_path = self.temp_dir / "stream_touch_save.zip"
        self._create_valid_factorio_zip(save_path, marker="stream_touch_test")
        sha256 = self._compute_sha256(save_path)

        with patch.object(self.session_manager, "touch_activity", wraps=self.session_manager.touch_activity) as mock_touch:
            with open(save_path, "rb") as f:
                res = self.client.post(
                    "/upload-save",
                    data={
                        "session_token": token,
                        "release_on_success": "true",
                    },
                    files={"file": ("stream_touch_save.zip", f, "application/zip")},
                    headers={
                        "X-Expected-SHA256": sha256,
                        "X-Session-Token": token,
                    },
                )

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        # touch_activity must be called at least once (by middleware and/or chunk reader)
        self.assertGreaterEqual(mock_touch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
