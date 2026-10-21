"""
Distributed session state and host lock manager for Factorio P2P Cloud Sync & Coordinator.
Implements a 4-phase state machine (idle -> claiming -> hosting_active -> uploading_final -> idle),
preventing Split-Brain, Fake Host blocking, and race conditions during host handover.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.server.config import ServerConfig
from src.server.logger import get_logger

logger = get_logger("session_manager")


class SessionState(str, Enum):
    """4-phase state machine states."""

    IDLE = "idle"
    CLAIMING = "claiming"
    HOSTING_ACTIVE = "hosting_active"
    UPLOADING_FINAL = "uploading_final"


@dataclass
class SessionData:
    """Internal representation of current server multiplayer session."""

    state: str = SessionState.IDLE.value
    session_id: Optional[str] = None
    session_token: Optional[str] = None
    host_name: Optional[str] = None
    host_local_ip: Optional[str] = None
    host_public_ip: Optional[str] = None
    host_udp_port: Optional[int] = None
    started_at: Optional[float] = None
    state_changed_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    active_guests: List[Dict[str, Any]] = field(default_factory=list)

    def reset_to_idle(self) -> None:
        """Resets all session fields back to idle state."""
        self.state = SessionState.IDLE.value
        self.session_id = None
        self.session_token = None
        self.host_name = None
        self.host_local_ip = None
        self.host_public_ip = None
        self.host_udp_port = None
        self.started_at = None
        self.state_changed_at = time.time()
        self.last_seen = time.time()
        self.active_guests.clear()


class SessionLockManager:
    """
    Thread-safe session state and distributed lock manager.
    Enforces time-to-live policies:
    - claiming TTL: 180 seconds (Fake Host protection)
    - hosting_active TTL: 90 seconds (Network disconnect / crash protection)
    - uploading_final TTL: 45 seconds (Handover upload race protection)
    """

    CLAIM_TTL_SECONDS: float = 180.0
    FINAL_UPLOAD_TTL_SECONDS: float = 45.0

    def __init__(self, config: ServerConfig) -> None:
        """
        Initializes the session lock manager.

        Args:
            config: Validated ServerConfig instance.
        """
        if not isinstance(config, ServerConfig):
            raise TypeError(f"config must be a ServerConfig instance, got {type(config).__name__}")

        self.config = config
        self.status_file_path: Path = self.config.storage_dir / "status.json"
        self._lock = threading.RLock()
        self._session = SessionData()

        # Load persisted status or initialize fresh
        self._load_status_from_disk()

    @property
    def lease_timeout_seconds(self) -> float:
        """Heartbeat expiration timeout from configuration."""
        return float(self.config.lease_timeout_seconds)

    def _load_status_from_disk(self) -> None:
        """Loads session state from status.json if present, then checks for expiration."""
        with self._lock:
            if not self.status_file_path.exists():
                logger.info("No status.json found at %s. Starting with fresh IDLE state.", self.status_file_path)
                self._persist_status_locked()
                return

            try:
                with open(self.status_file_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)

                if isinstance(raw_data, dict):
                    self._session.state = raw_data.get("state", SessionState.IDLE.value)
                    self._session.session_id = raw_data.get("session_id")
                    self._session.session_token = raw_data.get("session_token")
                    self._session.host_name = raw_data.get("host_name")
                    self._session.host_local_ip = raw_data.get("host_local_ip")
                    self._session.host_public_ip = raw_data.get("host_public_ip")
                    self._session.host_udp_port = raw_data.get("host_udp_port")
                    self._session.started_at = raw_data.get("started_at")
                    self._session.state_changed_at = raw_data.get("state_changed_at", time.time())
                    self._session.last_seen = raw_data.get("last_seen", time.time())
                    self._session.active_guests = raw_data.get("active_guests", [])

                    logger.info("Loaded persisted session state: %s (host: %s)", self._session.state, self._session.host_name)
                    # Run expiration check in case server was restarted after a timeout
                    self._check_and_expire_lease_locked()
            except Exception as exc:
                logger.warning("Failed to load status.json (%s). Reverting to clean IDLE state.", exc)
                self._session.reset_to_idle()
                self._persist_status_locked()

    def _persist_status_locked(self) -> None:
        """Atomically persists the current session state to disk. Caller must hold self._lock."""
        try:
            self.config.storage_dir.mkdir(parents=True, exist_ok=True)
            temp_path = self.status_file_path.with_suffix(".tmp")
            data = asdict(self._session)

            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            os.replace(str(temp_path), str(self.status_file_path))
            logger.debug("Persisted session state %s to %s", self._session.state, self.status_file_path)
        except Exception as exc:
            logger.error("Failed to persist session state to %s: %s", self.status_file_path, exc)

    def _check_and_expire_lease_locked(self) -> bool:
        """
        Enforces state expiration policies. Caller must hold self._lock.

        Returns:
            bool: True if an expiration transition occurred.
        """
        now = time.time()
        state = self._session.state

        # 1. Fake Host protection: claiming state expiration (180 sec)
        if state == SessionState.CLAIMING.value:
            elapsed = now - self._session.state_changed_at
            if elapsed > self.CLAIM_TTL_SECONDS:
                logger.warning(
                    "Fake Host protection triggered: player '%s' claimed host slot %d seconds ago "
                    "without opening network game in Factorio. Reverting reservation to IDLE.",
                    self._session.host_name,
                    int(elapsed),
                )
                self._session.reset_to_idle()
                self._persist_status_locked()
                return True

        # 2. Host disconnect / crash protection: hosting_active lease timeout (90 sec)
        elif state == SessionState.HOSTING_ACTIVE.value:
            elapsed = now - self._session.last_seen
            if elapsed > self.lease_timeout_seconds:
                logger.warning(
                    "Host heartbeat expired: host '%s' was last seen %d seconds ago "
                    "(timeout threshold %d sec). Resetting session to IDLE.",
                    self._session.host_name,
                    int(elapsed),
                    int(self.lease_timeout_seconds),
                )
                self._session.reset_to_idle()
                self._persist_status_locked()
                return True

        # 3. Handover upload race protection: uploading_final timeout (45 sec)
        elif state == SessionState.UPLOADING_FINAL.value:
            elapsed = now - self._session.state_changed_at
            if elapsed > self.FINAL_UPLOAD_TTL_SECONDS:
                logger.warning(
                    "Final upload timeout: host '%s' initiated final save upload %d seconds ago "
                    "without completion. Resetting session to IDLE.",
                    self._session.host_name,
                    int(elapsed),
                )
                self._session.reset_to_idle()
                self._persist_status_locked()
                return True

        return False

    def get_public_status(self) -> Dict[str, Any]:
        """
        Retrieves the public session status view without exposing sensitive session_token.

        Returns:
            Dict[str, Any]: Sanitized dictionary containing current multiplayer state.
        """
        with self._lock:
            self._check_and_expire_lease_locked()
            now = time.time()

            lease_remaining = 0.0
            if self._session.state == SessionState.CLAIMING.value:
                lease_remaining = max(0.0, self.CLAIM_TTL_SECONDS - (now - self._session.state_changed_at))
            elif self._session.state == SessionState.HOSTING_ACTIVE.value:
                lease_remaining = max(0.0, self.lease_timeout_seconds - (now - self._session.last_seen))
            elif self._session.state == SessionState.UPLOADING_FINAL.value:
                lease_remaining = max(0.0, self.FINAL_UPLOAD_TTL_SECONDS - (now - self._session.state_changed_at))

            return {
                "state": self._session.state,
                "session_id": self._session.session_id,
                "host_name": self._session.host_name,
                "host_public_ip": self._session.host_public_ip,
                "host_local_ip": self._session.host_local_ip,
                "host_udp_port": self._session.host_udp_port,
                "lease_remaining_seconds": round(lease_remaining, 1),
                "active_guests": list(self._session.active_guests),
                "started_at": self._session.started_at,
                "last_seen": self._session.last_seen,
            }

    def claim_host(
        self,
        player_name: str,
        client_public_ip: str,
        client_local_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Attempts to claim the host lease for a player launching Factorio.

        Args:
            player_name: Unique player nickname.
            client_public_ip: Remote public IP address of the client router.
            client_local_ip: Optional local LAN IP address of the client PC.

        Returns:
            Dict[str, Any]: Operation outcome including session_token on success.
        """
        # Validate player_name
        if not isinstance(player_name, str):
            return {
                "success": False,
                "reason": "invalid_player_name",
                "message": "player_name must be a string",
                "status": self.get_public_status(),
            }

        cleaned_name = player_name.strip()
        if len(cleaned_name) < 2 or len(cleaned_name) > 32:
            return {
                "success": False,
                "reason": "invalid_player_name",
                "message": "player_name length must be between 2 and 32 characters",
                "status": self.get_public_status(),
            }

        if cleaned_name.lower() == "player":
            return {
                "success": False,
                "reason": "invalid_player_name",
                "message": "Default nickname 'Player' is forbidden to prevent inventory collision",
                "status": self.get_public_status(),
            }

        with self._lock:
            self._check_and_expire_lease_locked()
            now = time.time()

            # Case A: Host is currently uploading final save
            if self._session.state == SessionState.UPLOADING_FINAL.value:
                logger.info(
                    "Player '%s' attempted to claim host during uploading_final phase. "
                    "Instructing client to wait for final save commit.",
                    cleaned_name,
                )
                return {
                    "success": False,
                    "reason": "uploading_final",
                    "message": "Previous host is currently uploading the final save. Please wait a few seconds.",
                    "retry_after_seconds": 3,
                    "status": self.get_public_status(),
                }

            # Case B: Server is actively occupied by a host
            if self._session.state in (SessionState.CLAIMING.value, SessionState.HOSTING_ACTIVE.value):
                # Check for idempotent reconnect from the same host player/IP
                if (
                    self._session.host_name == cleaned_name
                    and self._session.host_public_ip == client_public_ip
                    and self._session.session_token is not None
                ):
                    logger.info("Host '%s' re-connected from same IP (%s). Refreshing lease.", cleaned_name, client_public_ip)
                    self._session.last_seen = now
                    if client_local_ip:
                        self._session.host_local_ip = client_local_ip
                    self._persist_status_locked()
                    return {
                        "success": True,
                        "reconnected": True,
                        "session_token": self._session.session_token,
                        "status": self.get_public_status(),
                    }

                logger.info(
                    "Rejected claim from '%s' (%s): server is already hosted by '%s' (%s, state: %s).",
                    cleaned_name,
                    client_public_ip,
                    self._session.host_name,
                    self._session.host_public_ip,
                    self._session.state,
                )
                return {
                    "success": False,
                    "reason": "already_hosted",
                    "message": f"Server is already hosted by {self._session.host_name}",
                    "status": self.get_public_status(),
                }

            # Case C: Server is IDLE -> Claim host slot
            token = secrets.token_urlsafe(32)
            session_id = str(uuid.uuid4())

            self._session.state = SessionState.CLAIMING.value
            self._session.session_id = session_id
            self._session.session_token = token
            self._session.host_name = cleaned_name
            self._session.host_public_ip = client_public_ip
            self._session.host_local_ip = client_local_ip
            self._session.host_udp_port = None
            self._session.started_at = now
            self._session.state_changed_at = now
            self._session.last_seen = now
            self._session.active_guests = []

            self._persist_status_locked()
            logger.info("Host slot reserved for '%s' (IP: %s, session_id: %s)", cleaned_name, client_public_ip, session_id)

            return {
                "success": True,
                "reconnected": False,
                "session_token": token,
                "status": self.get_public_status(),
            }

    def confirm_hosting(self, session_token: str, host_udp_port: Optional[int] = None) -> Dict[str, Any]:
        """
        Transitions state from 'claiming' to 'hosting_active' once the wrapper
        confirms that Factorio has opened UDP port 34197 and is ready for peers.

        Args:
            session_token: Secret token issued during claim_host.
            host_udp_port: External public UDP port assigned by NAT router.

        Returns:
            Dict[str, Any]: Operation outcome.
        """
        with self._lock:
            self._check_and_expire_lease_locked()

            if not self._validate_token_locked(session_token):
                logger.warning("confirm_hosting rejected: invalid session_token")
                return {"success": False, "reason": "invalid_token", "status": self.get_public_status()}

            if self._session.state not in (SessionState.CLAIMING.value, SessionState.HOSTING_ACTIVE.value):
                logger.warning("confirm_hosting rejected: unexpected state '%s'", self._session.state)
                return {
                    "success": False,
                    "reason": "invalid_state",
                    "current_state": self._session.state,
                    "status": self.get_public_status(),
                }

            now = time.time()
            self._session.state = SessionState.HOSTING_ACTIVE.value
            self._session.state_changed_at = now
            self._session.last_seen = now

            if host_udp_port is not None and (1 <= host_udp_port <= 65535):
                self._session.host_udp_port = host_udp_port

            self._persist_status_locked()
            logger.info("Host '%s' confirmed active multiplayer hosting on UDP port %s", self._session.host_name, self._session.host_udp_port)

            return {"success": True, "status": self.get_public_status()}

    def heartbeat(self, session_token: str, host_udp_port: Optional[int] = None) -> Dict[str, Any]:
        """
        Refreshes the lease timestamp for the active host.

        Args:
            session_token: Secret token issued during claim_host.
            host_udp_port: Optional updated external UDP port.

        Returns:
            Dict[str, Any]: Operation outcome and active guest list.
        """
        with self._lock:
            self._check_and_expire_lease_locked()

            if not self._validate_token_locked(session_token):
                logger.warning("heartbeat rejected: invalid session_token")
                return {"success": False, "reason": "invalid_token", "status": self.get_public_status()}

            if self._session.state not in (SessionState.HOSTING_ACTIVE.value, SessionState.CLAIMING.value):
                logger.warning("heartbeat rejected: session is in state '%s'", self._session.state)
                return {"success": False, "reason": "not_hosting", "status": self.get_public_status()}

            self._session.last_seen = time.time()
            if host_udp_port is not None and (1 <= host_udp_port <= 65535):
                self._session.host_udp_port = host_udp_port

            self._persist_status_locked()
            logger.debug("Received heartbeat from host '%s'", self._session.host_name)

            return {
                "success": True,
                "active_guests": list(self._session.active_guests),
                "status": self.get_public_status(),
            }

    def begin_final_upload(self, session_token: str) -> Dict[str, Any]:
        """
        Transitions state to 'uploading_final' when the host closes the game
        and prepares to upload the final world save to the VPS.

        Args:
            session_token: Secret token issued during claim_host.

        Returns:
            Dict[str, Any]: Operation outcome.
        """
        with self._lock:
            self._check_and_expire_lease_locked()

            if not self._validate_token_locked(session_token):
                logger.warning("begin_final_upload rejected: invalid session_token")
                return {"success": False, "reason": "invalid_token", "status": self.get_public_status()}

            now = time.time()
            self._session.state = SessionState.UPLOADING_FINAL.value
            self._session.state_changed_at = now
            self._session.last_seen = now
            self._persist_status_locked()

            logger.info("Host '%s' initiated final world upload (state: uploading_final)", self._session.host_name)
            return {"success": True, "status": self.get_public_status()}

    def release_host(self, session_token: str, reason: str = "normal_exit") -> Dict[str, Any]:
        """
        Releases the host lock and resets session state to 'idle'.

        Args:
            session_token: Secret token issued during claim_host.
            reason: Optional explanation string for logging.

        Returns:
            Dict[str, Any]: Operation outcome.
        """
        with self._lock:
            self._check_and_expire_lease_locked()

            if not self._validate_token_locked(session_token):
                logger.warning("release_host rejected: invalid session_token")
                return {"success": False, "reason": "invalid_token", "status": self.get_public_status()}

            logger.info("Host '%s' released server lock (reason: %s). Transitioning to IDLE.", self._session.host_name, reason)
            self._session.reset_to_idle()
            self._persist_status_locked()

            return {"success": True, "reason": reason, "status": self.get_public_status()}

    def validate_host_token(self, session_token: Optional[str]) -> bool:
        """
        Strictly verifies whether a given token matches the currently active host.

        Args:
            session_token: Candidate token to verify.

        Returns:
            bool: True if authorized as active host.
        """
        with self._lock:
            self._check_and_expire_lease_locked()
            return self._validate_token_locked(session_token)

    def _validate_token_locked(self, candidate_token: Optional[str]) -> bool:
        """Internal token verification while holding lock."""
        if not candidate_token or not isinstance(candidate_token, str):
            return False

        if self._session.state == SessionState.IDLE.value:
            return False

        if not self._session.session_token:
            return False

        return secrets.compare_digest(self._session.session_token, candidate_token)

    def register_guest(
        self,
        guest_name: str,
        guest_public_ip: str,
        guest_udp_port: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Registers or updates an active guest connecting to the host.

        Args:
            guest_name: Unique nickname of the guest.
            guest_public_ip: Remote public IP address of the guest router.
            guest_udp_port: External UDP port of the guest proxy.

        Returns:
            Dict[str, Any]: Operation outcome.
        """
        if not guest_name or not isinstance(guest_name, str):
            return {"success": False, "reason": "invalid_guest_name"}

        with self._lock:
            self._check_and_expire_lease_locked()

            # Update existing guest or append new
            updated = False
            for guest in self._session.active_guests:
                if guest.get("name") == guest_name:
                    guest["public_ip"] = guest_public_ip
                    guest["udp_port"] = guest_udp_port
                    guest["last_seen"] = time.time()
                    updated = True
                    break

            if not updated:
                self._session.active_guests.append({
                    "name": guest_name,
                    "public_ip": guest_public_ip,
                    "udp_port": guest_udp_port,
                    "last_seen": time.time(),
                })

            self._persist_status_locked()
            logger.debug("Registered active guest '%s' (%s:%s)", guest_name, guest_public_ip, guest_udp_port)
            return {"success": True, "active_guests": list(self._session.active_guests)}

    def deregister_guest(self, guest_name: str) -> Dict[str, Any]:
        """
        Removes a guest from the active guest roster upon disconnect.

        Args:
            guest_name: Nickname of the guest to remove.

        Returns:
            Dict[str, Any]: Operation outcome.
        """
        with self._lock:
            self._check_and_expire_lease_locked()
            self._session.active_guests = [
                g for g in self._session.active_guests if g.get("name") != guest_name
            ]
            self._persist_status_locked()
            logger.debug("Deregistered guest '%s'", guest_name)
            return {"success": True, "active_guests": list(self._session.active_guests)}
