"""
Unit and integration tests for SaveStorageManager.
Covers atomic save commits, backup rotation, CRC-32 testing, and edge case handling.
"""

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.server.config import ServerConfig
from src.server.storage import (
    InvalidSaveArchiveError,
    SaveNotFoundError,
    SaveStorageManager,
    StorageError,
)


class TestSaveStorageManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_factorio_storage_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            max_backups=3,
            max_upload_size_bytes=5 * 1024 * 1024,  # 5 MB
            udp_stun_port=14197,
        )
        self.manager = SaveStorageManager(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_valid_factorio_zip(self, file_path: Path, marker: str = "world_v1") -> Path:
        """Helper to create a syntactically valid Factorio save archive."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(file_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("my_save/level.dat", f"Binary Level Data: {marker}".encode("utf-8") * 5)
            zf.writestr("my_save/info.json", json.dumps({"name": marker, "version": "2.0"}))
            zf.writestr("my_save/control.lua", "-- Lua script logic\n")
        return file_path

    def _create_corrupted_crc_zip(self, file_path: Path) -> Path:
        """Helper to create a ZIP file with intentionally corrupted payload to fail CRC."""
        self._create_valid_factorio_zip(file_path, marker="corrupt_target")
        with open(file_path, "r+b") as f:
            # Corrupt byte in the middle of compressed payload
            f.seek(60)
            f.write(b"\xff\xff\xff\xff")
        return file_path

    def test_storage_initialization_and_temp_cleanup(self):
        """Ensure directories are created and orphaned temp files are purged on init."""
        self.assertTrue(self.config.backups_dir.exists())
        self.assertTrue(self.config.tmp_dir.exists())
        self.assertTrue(self.config.logs_dir.exists())

        # Inject an orphaned temp file
        orphaned = self.config.tmp_dir / f"{self.config.temp_save_prefix}orphaned.zip"
        orphaned.write_bytes(b"temp data" * 20)
        self.assertTrue(orphaned.exists())

        # Re-initialize and verify cleanup
        self.manager.initialize_storage()
        self.assertFalse(orphaned.exists())

    def test_validate_zip_non_existent(self):
        self.assertFalse(self.manager.validate_zip_archive(self.temp_dir / "non_existent.zip"))

    def test_validate_zip_too_small(self):
        tiny_file = self.temp_dir / "tiny.zip"
        tiny_file.write_bytes(b"PK\x03\x04short")
        self.assertFalse(self.manager.validate_zip_archive(tiny_file))

    def test_validate_zip_oversized(self):
        oversized_config = ServerConfig(
            storage_dir=self.temp_dir,
            max_upload_size_bytes=200,  # small threshold for testing
        )
        small_manager = SaveStorageManager(oversized_config)
        test_file = self.temp_dir / "oversized.zip"
        self._create_valid_factorio_zip(test_file)
        self.assertFalse(small_manager.validate_zip_archive(test_file))

    def test_validate_zip_not_zip(self):
        junk_file = self.temp_dir / "junk.zip"
        junk_file.write_bytes(b"This is just random non-zip text data that is long enough." * 5)
        self.assertFalse(self.manager.validate_zip_archive(junk_file))

    def test_validate_zip_corrupt_crc(self):
        corrupt_file = self.temp_dir / "corrupted_crc.zip"
        self._create_corrupted_crc_zip(corrupt_file)
        self.assertFalse(self.manager.validate_zip_archive(corrupt_file))

    def test_validate_zip_missing_factorio_markers(self):
        non_factorio_file = self.temp_dir / "random_archive.zip"
        with zipfile.ZipFile(non_factorio_file, mode="w") as zf:
            zf.writestr("notes.txt", "Just a readme text" * 20)
        self.assertFalse(self.manager.validate_zip_archive(non_factorio_file))

    def test_validate_zip_valid_archive(self):
        valid_file = self.temp_dir / "valid_world.zip"
        self._create_valid_factorio_zip(valid_file)
        self.assertTrue(self.manager.validate_zip_archive(valid_file))

    def test_compute_sha256(self):
        target_file = self.temp_dir / "hash_target.bin"
        target_file.write_bytes(b"Hello Factorio Cloud Sync!")
        sha = self.manager.compute_sha256(target_file)
        # Precomputed SHA-256 for b"Hello Factorio Cloud Sync!"
        expected = "1b3bc0bee3183847925e6934775e0145b7f6e058460c54e4927677ebfa80ff4c"
        self.assertEqual(sha, expected)

    def test_atomic_save_rejects_invalid_archive(self):
        invalid_temp = self.config.tmp_dir / f"{self.config.temp_save_prefix}bad.zip"
        invalid_temp.write_bytes(b"garbage content")

        with self.assertRaises(InvalidSaveArchiveError):
            self.manager.save_new_world_atomic(invalid_temp, uploaded_by="Alex")

        # Verify invalid temp file was deleted
        self.assertFalse(invalid_temp.exists())
        # Verify no save was created
        self.assertFalse(self.config.current_save_path.exists())

    def test_atomic_save_initial_commit(self):
        temp_save = self.config.tmp_dir / f"{self.config.temp_save_prefix}1.zip"
        self._create_valid_factorio_zip(temp_save, marker="gen_1")

        meta = self.manager.save_new_world_atomic(temp_save, uploaded_by="Alex")

        self.assertTrue(meta["exists"])
        self.assertEqual(meta["status"], "updated")
        self.assertEqual(meta["uploaded_by"], "Alex")
        self.assertEqual(meta["version_counter"], 1)
        self.assertTrue(self.config.current_save_path.exists())
        self.assertFalse(temp_save.exists())

        # Verify save_metadata.json matches
        current_meta = self.manager.get_current_save_metadata()
        self.assertEqual(current_meta["version_counter"], 1)
        self.assertEqual(current_meta["sha256"], meta["sha256"])

    def test_atomic_save_unchanged_deduplication(self):
        temp_save1 = self.config.tmp_dir / f"{self.config.temp_save_prefix}1.zip"
        self._create_valid_factorio_zip(temp_save1, marker="same_world")
        meta1 = self.manager.save_new_world_atomic(temp_save1, uploaded_by="Alex")

        # Second upload with identical bytes
        temp_save2 = self.config.tmp_dir / f"{self.config.temp_save_prefix}2.zip"
        self._create_valid_factorio_zip(temp_save2, marker="same_world")
        meta2 = self.manager.save_new_world_atomic(temp_save2, uploaded_by="Dima")

        self.assertEqual(meta2["status"], "unchanged")
        self.assertEqual(meta2["version_counter"], 1)
        # Temp file must be cleanly deleted
        self.assertFalse(temp_save2.exists())
        # No backups should be created for duplicate
        self.assertFalse((self.config.backups_dir / "backup_1.zip").exists())

    def test_atomic_save_circular_backup_rotation(self):
        """Test full rotation cycle across 4 versions with max_backups=3."""
        # Version 1
        t1 = self.config.tmp_dir / f"{self.config.temp_save_prefix}1.zip"
        self._create_valid_factorio_zip(t1, marker="v1")
        m1 = self.manager.save_new_world_atomic(t1, uploaded_by="Host1")
        self.assertEqual(m1["version_counter"], 1)
        sha_v1 = m1["sha256"]

        # Version 2
        t2 = self.config.tmp_dir / f"{self.config.temp_save_prefix}2.zip"
        self._create_valid_factorio_zip(t2, marker="v2")
        m2 = self.manager.save_new_world_atomic(t2, uploaded_by="Host2")
        self.assertEqual(m2["version_counter"], 2)
        sha_v2 = m2["sha256"]

        # Check backup_1 has v1
        b1 = self.config.backups_dir / "backup_1.zip"
        self.assertTrue(b1.exists())
        self.assertEqual(self.manager.compute_sha256(b1), sha_v1)

        # Version 3
        t3 = self.config.tmp_dir / f"{self.config.temp_save_prefix}3.zip"
        self._create_valid_factorio_zip(t3, marker="v3")
        m3 = self.manager.save_new_world_atomic(t3, uploaded_by="Host3")
        self.assertEqual(m3["version_counter"], 3)
        sha_v3 = m3["sha256"]

        # Check backup_1 has v2, backup_2 has v1
        b2 = self.config.backups_dir / "backup_2.zip"
        self.assertEqual(self.manager.compute_sha256(b1), sha_v2)
        self.assertEqual(self.manager.compute_sha256(b2), sha_v1)

        # Version 4
        t4 = self.config.tmp_dir / f"{self.config.temp_save_prefix}4.zip"
        self._create_valid_factorio_zip(t4, marker="v4")
        m4 = self.manager.save_new_world_atomic(t4, uploaded_by="Host4")
        self.assertEqual(m4["version_counter"], 4)
        sha_v4 = m4["sha256"]

        # Check backup_1 has v3, backup_2 has v2, backup_3 has v1
        b3 = self.config.backups_dir / "backup_3.zip"
        self.assertEqual(self.manager.compute_sha256(b1), sha_v3)
        self.assertEqual(self.manager.compute_sha256(b2), sha_v2)
        self.assertEqual(self.manager.compute_sha256(b3), sha_v1)

        # Version 5: v1 should be dropped because max_backups=3
        t5 = self.config.tmp_dir / f"{self.config.temp_save_prefix}5.zip"
        self._create_valid_factorio_zip(t5, marker="v5")
        m5 = self.manager.save_new_world_atomic(t5, uploaded_by="Host5")
        self.assertEqual(m5["version_counter"], 5)

        self.assertEqual(self.manager.compute_sha256(b1), sha_v4)
        self.assertEqual(self.manager.compute_sha256(b2), sha_v3)
        self.assertEqual(self.manager.compute_sha256(b3), sha_v2)
        # Ensure no backup_4 was created
        self.assertFalse((self.config.backups_dir / "backup_4.zip").exists())

    def test_restore_from_backup(self):
        # Create v1 and v2
        t1 = self.config.tmp_dir / f"{self.config.temp_save_prefix}1.zip"
        self._create_valid_factorio_zip(t1, marker="world_v1")
        m1 = self.manager.save_new_world_atomic(t1, uploaded_by="Host1")
        sha_v1 = m1["sha256"]

        t2 = self.config.tmp_dir / f"{self.config.temp_save_prefix}2.zip"
        self._create_valid_factorio_zip(t2, marker="world_v2")
        m2 = self.manager.save_new_world_atomic(t2, uploaded_by="Host2")
        sha_v2 = m2["sha256"]

        # Current is v2, backup_1 is v1. Now restore from backup 1.
        restored_meta = self.manager.restore_from_backup(1)
        self.assertEqual(restored_meta["status"], "restored")
        self.assertEqual(restored_meta["sha256"], sha_v1)
        self.assertEqual(restored_meta["version_counter"], 3)

        # Now current world must match v1
        self.assertEqual(self.manager.compute_sha256(self.config.current_save_path), sha_v1)
        # And backup_1 now holds the archived v2
        b1 = self.config.backups_dir / "backup_1.zip"
        self.assertEqual(self.manager.compute_sha256(b1), sha_v2)

    def test_restore_invalid_index(self):
        with self.assertRaises(ValueError):
            self.manager.restore_from_backup(0)
        with self.assertRaises(ValueError):
            self.manager.restore_from_backup(4)

    def test_restore_non_existent_backup(self):
        with self.assertRaises(SaveNotFoundError):
            self.manager.restore_from_backup(2)


if __name__ == "__main__":
    unittest.main()
