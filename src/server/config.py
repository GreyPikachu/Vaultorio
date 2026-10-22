"""
Server configuration module for Factorio P2P Cloud Sync & Coordinator.
Provides centralized, validated configuration with environment variable support.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigValidationError(ValueError):
    """Raised when configuration parameters fail validation rules."""


@dataclass(frozen=True)
class ServerConfig:
    """Immutable server configuration container with strict validation."""

    storage_dir: Path
    current_save_filename: str = "cloud_world.zip"
    temp_save_prefix: str = "upload_tmp_"
    max_backups: int = 3
    max_upload_size_bytes: int = 200 * 1024 * 1024  # 200 MB
    http_host: str = "0.0.0.0"
    http_port: int = 8088
    udp_stun_port: int = 14197
    lease_timeout_seconds: int = 90
    require_device_auth: bool = False

    @property
    def backups_dir(self) -> Path:
        """Directory for rotating backup archives."""
        return self.storage_dir / "backups"

    @property
    def tmp_dir(self) -> Path:
        """Directory for intermediate staging and uploads."""
        return self.storage_dir / "tmp"

    @property
    def logs_dir(self) -> Path:
        """Directory for server execution logs."""
        return self.storage_dir / "logs"

    @property
    def current_save_path(self) -> Path:
        """Absolute path to the active cloud world save file."""
        return self.storage_dir / self.current_save_filename

    @property
    def metadata_file_path(self) -> Path:
        """Path to metadata file containing hash, version, and upload stats."""
        return self.storage_dir / "save_metadata.json"

    @property
    def auth_state_path(self) -> Path:
        """Path to persistent device tokens and invite tickets JSON file."""
        return self.storage_dir / "auth_state.json"

    def validate(self) -> None:
        """
        Validates all configuration attributes against system limits and safety policies.

        Raises:
            ConfigValidationError: If any configuration value is invalid or unsafe.
        """
        # Validate storage_dir
        if not isinstance(self.storage_dir, Path):
            raise ConfigValidationError(
                f"storage_dir must be a pathlib.Path, got {type(self.storage_dir).__name__}"
            )

        # Validate save filename
        if not self.current_save_filename or not self.current_save_filename.endswith(".zip"):
            raise ConfigValidationError(
                f"current_save_filename must be a non-empty .zip file name, got '{self.current_save_filename}'"
            )

        # Validate max backups
        if self.max_backups < 1 or self.max_backups > 50:
            raise ConfigValidationError(
                f"max_backups must be between 1 and 50, got {self.max_backups}"
            )

        # Validate max upload size
        if self.max_upload_size_bytes < 1024 or self.max_upload_size_bytes > 2 * 1024 * 1024 * 1024:
            raise ConfigValidationError(
                f"max_upload_size_bytes must be between 1 KB and 2 GB, got {self.max_upload_size_bytes}"
            )

        # Validate HTTP Port
        if not (1 <= self.http_port <= 65535):
            raise ConfigValidationError(
                f"http_port must be in range 1-65535, got {self.http_port}"
            )

        # Validate UDP STUN Port
        if not (1 <= self.udp_stun_port <= 65535):
            raise ConfigValidationError(
                f"udp_stun_port must be in range 1-65535, got {self.udp_stun_port}"
            )

        # Critical Safety Gate: Ensure UDP STUN Port does not collide with Hysteria port hopping (20000-50000)
        if 20000 <= self.udp_stun_port <= 50000:
            raise ConfigValidationError(
                f"CRITICAL PORT CONFLICT: udp_stun_port {self.udp_stun_port} falls into "
                "the 20000-50000 range intercepted by Hysteria 2 iptables rules on the VPS!"
            )

        # Validate timeout
        if self.lease_timeout_seconds < 10 or self.lease_timeout_seconds > 3600:
            raise ConfigValidationError(
                f"lease_timeout_seconds must be between 10 and 3600 seconds, got {self.lease_timeout_seconds}"
            )


def load_server_config() -> ServerConfig:
    """
    Factory function to load and validate ServerConfig from environment variables or defaults.

    Returns:
        ServerConfig: Fully validated server configuration instance.
    """
    env_storage_dir = os.getenv("FACTORIO_STORAGE_DIR")
    if env_storage_dir:
        storage_path = Path(env_storage_dir).resolve()
    else:
        # Default policy: on Linux VPS use /opt/factorio-hub/data if /opt exists and is writable,
        # otherwise default to local ./data/server_storage
        vps_default = Path("/opt/factorio-hub/data")
        try:
            if vps_default.parent.exists() and os.access(vps_default.parent, os.W_OK):
                storage_path = vps_default
            else:
                storage_path = Path("./data/server_storage").resolve()
        except Exception:
            storage_path = Path("./data/server_storage").resolve()

    http_host = os.getenv("FACTORIO_HTTP_HOST", "0.0.0.0")

    try:
        http_port = int(os.getenv("FACTORIO_HTTP_PORT", "8088"))
    except ValueError as exc:
        raise ConfigValidationError(f"Invalid FACTORIO_HTTP_PORT: {exc}") from exc

    try:
        udp_stun_port = int(os.getenv("FACTORIO_UDP_STUN_PORT", "14197"))
    except ValueError as exc:
        raise ConfigValidationError(f"Invalid FACTORIO_UDP_STUN_PORT: {exc}") from exc

    try:
        max_backups = int(os.getenv("FACTORIO_MAX_BACKUPS", "3"))
    except ValueError as exc:
        raise ConfigValidationError(f"Invalid FACTORIO_MAX_BACKUPS: {exc}") from exc

    try:
        max_upload_size = int(
            os.getenv("FACTORIO_MAX_UPLOAD_SIZE_BYTES", str(200 * 1024 * 1024))
        )
    except ValueError as exc:
        raise ConfigValidationError(f"Invalid FACTORIO_MAX_UPLOAD_SIZE_BYTES: {exc}") from exc

    try:
        lease_timeout = int(os.getenv("FACTORIO_LEASE_TIMEOUT_SECONDS", "90"))
    except ValueError as exc:
        raise ConfigValidationError(f"Invalid FACTORIO_LEASE_TIMEOUT_SECONDS: {exc}") from exc

    require_device_auth_env = os.getenv("FACTORIO_REQUIRE_DEVICE_AUTH", "false").lower()
    require_device_auth = require_device_auth_env in ("true", "1", "yes")

    config = ServerConfig(
        storage_dir=storage_path,
        current_save_filename=os.getenv("FACTORIO_CURRENT_SAVE_FILENAME", "cloud_world.zip"),
        temp_save_prefix=os.getenv("FACTORIO_TEMP_SAVE_PREFIX", "upload_tmp_"),
        max_backups=max_backups,
        max_upload_size_bytes=max_upload_size,
        http_host=http_host,
        http_port=http_port,
        udp_stun_port=udp_stun_port,
        lease_timeout_seconds=lease_timeout,
        require_device_auth=require_device_auth,
    )

    config.validate()
    return config
