"""
Atomic save file storage manager for Factorio P2P Cloud Sync & Coordinator.
Handles atomic saves, ZIP integrity validation, CRC checks, SHA-256 calculation,
and circular backup rotation (backup_1.zip, backup_2.zip, backup_3.zip).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.server.config import ServerConfig
from src.server.logger import get_logger

logger = get_logger("storage")


class StorageError(Exception):
    """Base exception for storage errors."""


class InvalidSaveArchiveError(StorageError):
    """Raised when a save file is not a valid, non-corrupt Factorio ZIP archive."""


class SaveNotFoundError(StorageError):
    """Raised when an operation expects a save file that does not exist."""


class SaveStorageManager:
    """
    Manages atomic storage, integrity validation, and backup rotation
    for the shared Factorio world save.
    """

    # Essential files indicative of a Factorio save directory structure inside the ZIP
    FACTORIO_MARKER_FILES = (
        "level.dat",
        "level-init.dat",
        "control.lua",
        "info.json",
        "map-settings.json",
        "script-output",
    )

    def __init__(self, config: ServerConfig) -> None:
        """
        Initializes the storage manager with a validated ServerConfig.

        Args:
            config: Server configuration settings.
        """
        if not isinstance(config, ServerConfig):
            raise TypeError(f"config must be a ServerConfig instance, got {type(config).__name__}")

        self.config = config
        self._lock = threading.RLock()
        self.initialize_storage()

    def initialize_storage(self) -> None:
        """
        Verifies and initializes storage directory hierarchy:
        - storage_dir
        - backups_dir
        - tmp_dir
        - logs_dir
        Cleans up orphaned temporary upload files left over from crashes.
        """
        try:
            self.config.storage_dir.mkdir(parents=True, exist_ok=True)
            self.config.backups_dir.mkdir(parents=True, exist_ok=True)
            self.config.tmp_dir.mkdir(parents=True, exist_ok=True)
            self.config.logs_dir.mkdir(parents=True, exist_ok=True)

            logger.info("Storage directories initialized at %s", self.config.storage_dir)

            # Cleanup orphaned temporary files in tmp_dir
            cleaned_count = 0
            for item in self.config.tmp_dir.iterdir():
                if item.is_file() and item.name.startswith(self.config.temp_save_prefix):
                    try:
                        item.unlink()
                        cleaned_count += 1
                    except OSError as cleanup_err:
                        logger.warning("Failed to remove orphaned temp file %s: %s", item, cleanup_err)

            if cleaned_count > 0:
                logger.info("Cleaned up %d orphaned temporary files from %s", cleaned_count, self.config.tmp_dir)

        except Exception as exc:
            logger.error("Failed to initialize storage directories: %s", exc, exc_info=True)
            raise StorageError(f"Storage initialization failed: {exc}") from exc

    def validate_zip_archive(self, file_path: Path) -> bool:
        """
        Performs comprehensive integrity validation on a Factorio save ZIP archive:
        1. Checks file existence and size bounds (100 bytes to MAX_UPLOAD_SIZE_BYTES).
        2. Validates ZIP signature (zipfile.is_zipfile).
        3. Performs full CRC-32 testing of all archived members (testzip()).
        4. Validates that the archive contains standard Factorio world files.

        Args:
            file_path: Path to the archive file to check.

        Returns:
            bool: True if the file is a healthy, valid Factorio save archive.
        """
        if not isinstance(file_path, Path):
            file_path = Path(file_path)

        if not file_path.exists():
            logger.warning("Validation failed: file %s does not exist", file_path)
            return False

        if not file_path.is_file():
            logger.warning("Validation failed: %s is not a regular file", file_path)
            return False

        try:
            file_size = file_path.stat().st_size
        except OSError as stat_err:
            logger.warning("Validation failed: could not stat %s: %s", file_path, stat_err)
            return False

        # Guard against zero-length or truncated files
        if file_size < 100:
            logger.warning(
                "Validation failed: file size %d bytes is below minimum threshold (100 bytes)",
                file_size,
            )
            return False

        # Guard against exceeding disk limits
        if file_size > self.config.max_upload_size_bytes:
            logger.warning(
                "Validation failed: file size %d bytes exceeds maximum allowed limit (%d bytes)",
                file_size,
                self.config.max_upload_size_bytes,
            )
            return False

        # Verify ZIP header signature
        if not zipfile.is_zipfile(file_path):
            logger.warning("Validation failed: %s does not have a valid ZIP header", file_path)
            return False

        try:
            with zipfile.ZipFile(file_path, mode="r") as zf:
                # Test CRC-32 checksums of all members
                corrupted_member = zf.testzip()
                if corrupted_member is not None:
                    logger.warning(
                        "Validation failed: archive %s has corrupted member '%s' (CRC mismatch)",
                        file_path,
                        corrupted_member,
                    )
                    return False

                namelist = zf.namelist()
                if not namelist:
                    logger.warning("Validation failed: archive %s is empty", file_path)
                    return False

                # Check for characteristic Factorio files inside the archive
                has_factorio_marker = any(
                    any(name.endswith(marker) for marker in self.FACTORIO_MARKER_FILES)
                    for name in namelist
                )
                if not has_factorio_marker:
                    logger.warning(
                        "Validation failed: archive %s does not contain recognizable Factorio save structures (%s)",
                        file_path,
                        self.FACTORIO_MARKER_FILES,
                    )
                    return False

        except zipfile.BadZipFile as bad_zip:
            logger.warning("Validation failed: bad ZIP format in %s: %s", file_path, bad_zip)
            return False
        except Exception as exc:
            logger.warning("Validation failed: unexpected error checking %s: %s", file_path, exc)
            return False

        logger.debug("Validation succeeded for archive %s (size: %d bytes)", file_path, file_size)
        return True

    def compute_sha256(self, file_path: Path) -> str:
        """
        Computes SHA-256 hash using 64 KB chunk streaming to keep memory footprint under 15-25 MB.

        Args:
            file_path: Path to the target file.

        Returns:
            str: Hexadecimal SHA-256 digest.
        """
        if not isinstance(file_path, Path):
            file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Cannot compute hash: file {file_path} not found")

        hasher = hashlib.sha256()
        chunk_size = 64 * 1024  # 64 KB chunks

        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as exc:
            logger.error("Failed to compute SHA-256 for %s: %s", file_path, exc)
            raise StorageError(f"SHA-256 computation failed: {exc}") from exc

    def save_new_world_atomic(
        self,
        temp_file_path: Path,
        uploaded_by: str,
        mod_list: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Atomically replaces the active cloud world save file with a new version.
        Guaranteed thread-safe and crash-resistant:
        1. Validates the integrity of the uploaded archive.
        2. If unchanged (identical SHA-256), discards temp file and avoids redundant rotation.
        3. If changed, performs circular backup rotation:
           backup_2 -> backup_3, backup_1 -> backup_2, current -> backup_1.
        4. Atomically replaces current_save_path via os.replace.
        5. Updates and persists save_metadata.json (including mod_list if provided).

        Args:
            temp_file_path: Path to the newly uploaded temporary archive.
            uploaded_by: Unique player nickname / identifier.
            mod_list: Optional list or dictionary of active mods.

        Returns:
            Dict[str, Any]: Save metadata details.

        Raises:
            InvalidSaveArchiveError: If uploaded file is corrupt or not a valid Factorio save.
            StorageError: On I/O or filesystem failures.
        """
        if not isinstance(temp_file_path, Path):
            temp_file_path = Path(temp_file_path)

        if not uploaded_by or not isinstance(uploaded_by, str):
            uploaded_by = "anonymous"

        with self._lock:
            # 1. Validate temporary archive integrity
            if not self.validate_zip_archive(temp_file_path):
                try:
                    if temp_file_path.exists():
                        temp_file_path.unlink()
                except OSError as cleanup_err:
                    logger.warning("Failed to delete invalid temp archive %s: %s", temp_file_path, cleanup_err)
                msg = f"Uploaded file {temp_file_path} failed integrity checks and was rejected."
                logger.error(msg)
                raise InvalidSaveArchiveError(msg)

            new_sha256 = self.compute_sha256(temp_file_path)
            new_size = temp_file_path.stat().st_size
            current_path = self.config.current_save_path

            # 2. Check if current save exists and is identical
            if current_path.exists() and current_path.is_file():
                try:
                    current_sha256 = self.compute_sha256(current_path)
                    if current_sha256 == new_sha256:
                        logger.info(
                            "Uploaded save is byte-for-byte identical to active world (SHA-256: %s). "
                            "Discarding temp file without backup rotation.",
                            new_sha256,
                        )
                        temp_file_path.unlink(missing_ok=True)
                        metadata = self._get_current_save_metadata_locked()
                        metadata["status"] = "unchanged"
                        return metadata
                except Exception as check_err:
                    logger.warning(
                        "Error checking current save SHA-256 (%s). Proceeding with backup rotation.",
                        check_err,
                    )

                # 3. Rotate backups
                self._rotate_backups_locked()

            # 4. Atomically replace active save file
            try:
                # Ensure destination directory exists
                current_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(temp_file_path), str(current_path))
                logger.info(
                    "Atomically committed new save: %s (size: %d bytes, SHA-256: %s)",
                    current_path.name,
                    new_size,
                    new_sha256,
                )
            except Exception as replace_err:
                logger.error("Atomic replace failed from %s to %s: %s", temp_file_path, current_path, replace_err)
                raise StorageError(f"Failed to commit save file: {replace_err}") from replace_err

            # 5. Determine version counter and update metadata
            prev_metadata = self._read_metadata_file_locked()
            prev_version = prev_metadata.get("version_counter", 0) if prev_metadata.get("exists") else 0
            new_version = prev_version + 1

            new_metadata: Dict[str, Any] = {
                "exists": True,
                "status": "updated",
                "filename": self.config.current_save_filename,
                "sha256": new_sha256,
                "size_bytes": new_size,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "uploaded_by": uploaded_by,
                "version_counter": new_version,
            }

            if mod_list is not None:
                new_metadata["mod_list"] = mod_list
                # Also persist mod-list.json for standalone access
                try:
                    self._save_mod_manifest_locked(mod_list)
                except Exception as mod_err:
                    logger.warning("Could not persist separate mod-list.json: %s", mod_err)

            self._write_metadata_file_locked(new_metadata)
            return new_metadata

    def get_mod_manifest(self) -> Dict[str, Any]:
        """
        Retrieves the active mod manifest either from save metadata or mod-list.json.

        Returns:
            Dict[str, Any]: {"exists": bool, "mod_list": Optional[Any]}
        """
        with self._lock:
            meta = self._get_current_save_metadata_locked()
            if meta.get("exists") and "mod_list" in meta and meta["mod_list"] is not None:
                return {"exists": True, "mod_list": meta["mod_list"]}

            mod_file = self.config.storage_dir / "mod-list.json"
            if mod_file.exists():
                try:
                    with open(mod_file, "r", encoding="utf-8") as f:
                        return {"exists": True, "mod_list": json.load(f)}
                except Exception as exc:
                    logger.warning("Failed to read mod-list.json: %s", exc)

            return {"exists": False, "mod_list": None}

    def save_mod_manifest(self, mod_list: Any) -> None:
        """
        Atomically saves standalone mod-list.json manifest to the storage directory.

        Args:
            mod_list: List or dictionary representing mod configuration.
        """
        with self._lock:
            self._save_mod_manifest_locked(mod_list)

    def _save_mod_manifest_locked(self, mod_list: Any) -> None:
        """Internal helper to atomically write mod-list.json while holding lock."""
        mod_file = self.config.storage_dir / "mod-list.json"
        tmp_file = mod_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(mod_list, f, indent=2, ensure_ascii=False)
            os.replace(str(tmp_file), str(mod_file))
            logger.debug("Saved mod manifest to %s", mod_file)
        except Exception as exc:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            logger.error("Failed to write mod manifest to %s: %s", mod_file, exc)
            raise StorageError(f"Failed to persist mod manifest: {exc}") from exc

    def _rotate_backups_locked(self) -> None:
        """
        Shifts existing backups circularly:
        backup_(N-1) -> backup_N, ..., backup_1 -> backup_2, current_save -> backup_1.
        Caller must hold self._lock.
        """
        current_path = self.config.current_save_path
        if not current_path.exists():
            return

        backups_dir = self.config.backups_dir
        max_b = self.config.max_backups

        try:
            # Shift older backups backwards
            for i in range(max_b - 1, 0, -1):
                src = backups_dir / f"backup_{i}.zip"
                dst = backups_dir / f"backup_{i + 1}.zip"
                if src.exists():
                    try:
                        shutil.copy2(src, dst)
                        logger.debug("Rotated backup: %s -> %s", src.name, dst.name)
                    except Exception as rot_err:
                        logger.warning("Error rotating %s to %s: %s", src, dst, rot_err)

            # Copy current save to backup_1.zip
            target_b1 = backups_dir / "backup_1.zip"
            shutil.copy2(current_path, target_b1)
            logger.info("Current save successfully archived to %s", target_b1.name)

        except Exception as exc:
            logger.error("Failed during backup rotation: %s", exc)
            # We don't abort save write if rotation fails, but log error prominently

    def get_current_save_metadata(self) -> Dict[str, Any]:
        """
        Retrieves metadata of the current active save.

        Returns:
            Dict[str, Any]: Metadata dictionary, or {"exists": False} if no world exists.
        """
        with self._lock:
            return self._get_current_save_metadata_locked()

    def _get_current_save_metadata_locked(self) -> Dict[str, Any]:
        """
        Retrieves metadata of the current active save while holding self._lock.
        """
        current_path = self.config.current_save_path
        if not current_path.exists() or not current_path.is_file():
            return {"exists": False}

        metadata = self._read_metadata_file_locked()
        if metadata.get("exists"):
            return metadata

        # Reconstruct metadata if file exists on disk but metadata file was missing
        try:
            sha256 = self.compute_sha256(current_path)
            size_bytes = current_path.stat().st_size
            mtime = datetime.fromtimestamp(current_path.stat().st_mtime, tz=timezone.utc).isoformat()
            reconstructed: Dict[str, Any] = {
                "exists": True,
                "filename": self.config.current_save_filename,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "updated_at": mtime,
                "uploaded_by": "unknown",
                "version_counter": 1,
            }
            self._write_metadata_file_locked(reconstructed)
            return reconstructed
        except Exception as exc:
            logger.error("Could not reconstruct save metadata: %s", exc)
            return {"exists": True, "filename": self.config.current_save_filename, "error": str(exc)}

    def restore_from_backup(self, backup_index: int = 1) -> Dict[str, Any]:
        """
        Restores active cloud world from one of the circular backups.

        Args:
            backup_index: Backup slot number (1 <= backup_index <= max_backups).

        Returns:
            Dict[str, Any]: New metadata for the restored world.

        Raises:
            SaveNotFoundError: If selected backup file does not exist.
            InvalidSaveArchiveError: If the backup archive is corrupt.
            StorageError: On I/O failure.
        """
        if not (1 <= backup_index <= self.config.max_backups):
            raise ValueError(
                f"backup_index must be between 1 and {self.config.max_backups}, got {backup_index}"
            )

        with self._lock:
            backup_file = self.config.backups_dir / f"backup_{backup_index}.zip"
            if not backup_file.exists():
                raise SaveNotFoundError(f"Backup file {backup_file} does not exist")

            if not self.validate_zip_archive(backup_file):
                raise InvalidSaveArchiveError(f"Backup file {backup_file} is corrupt")

            current_path = self.config.current_save_path

            # Stage the target backup first to protect it from being overwritten during rotation
            staging_file = self.config.tmp_dir / f"restore_staging_{backup_index}.zip"
            try:
                shutil.copy2(backup_file, staging_file)

                # Archive existing current save to backup_1 before overwriting
                if current_path.exists():
                    self._rotate_backups_locked()

                # Atomically commit restored world
                os.replace(str(staging_file), str(current_path))
                logger.info("Restored cloud world from %s", backup_file.name)
            except Exception as copy_err:
                logger.error("Failed to restore from %s: %s", backup_file, copy_err)
                raise StorageError(f"Restore failed: {copy_err}") from copy_err
            finally:
                if staging_file.exists():
                    staging_file.unlink(missing_ok=True)

            prev_metadata = self._read_metadata_file_locked()
            prev_version = prev_metadata.get("version_counter", 0) if prev_metadata.get("exists") else 0

            new_sha256 = self.compute_sha256(current_path)
            new_size = current_path.stat().st_size

            restored_metadata: Dict[str, Any] = {
                "exists": True,
                "status": "restored",
                "filename": self.config.current_save_filename,
                "sha256": new_sha256,
                "size_bytes": new_size,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "uploaded_by": f"system_restore_from_backup_{backup_index}",
                "version_counter": prev_version + 1,
            }

            self._write_metadata_file_locked(restored_metadata)
            return restored_metadata

    def _read_metadata_file_locked(self) -> Dict[str, Any]:
        """Reads metadata JSON file. Must be called while holding self._lock."""
        meta_path = self.config.metadata_file_path
        if not meta_path.exists():
            return {"exists": False}

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            logger.warning("Could not read metadata JSON at %s: %s", meta_path, exc)

        return {"exists": False}

    def _write_metadata_file_locked(self, data: Dict[str, Any]) -> None:
        """Writes metadata JSON file atomically. Must be called while holding self._lock."""
        meta_path = self.config.metadata_file_path
        tmp_meta_path = meta_path.with_suffix(".tmp")
        try:
            with open(tmp_meta_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(str(tmp_meta_path), str(meta_path))
            logger.debug("Updated metadata file at %s", meta_path)
        except Exception as exc:
            logger.error("Failed to write metadata file %s: %s", meta_path, exc)
            if tmp_meta_path.exists():
                tmp_meta_path.unlink(missing_ok=True)
            raise StorageError(f"Failed to persist save metadata: {exc}") from exc
