"""
Pydantic data schemas and validation models for Factorio Safe HTTP REST API.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ClaimHostRequest(BaseModel):
    """Payload for reserving the host slot."""

    player_name: str = Field(..., min_length=2, max_length=32, description="Player nickname")
    local_ip: Optional[str] = Field(None, description="Local LAN IP address of client")


class ConfirmHostingRequest(BaseModel):
    """Payload for confirming active multiplayer hosting once Factorio lobby starts."""

    session_token: str = Field(..., min_length=16, description="Host session token")
    udp_port: Optional[int] = Field(None, ge=1, le=65535, description="External UDP port from NAT")


class BeginFinalUploadRequest(BaseModel):
    """Payload for notifying server of game shutdown before uploading final save."""

    session_token: str = Field(..., min_length=16, description="Host session token")


class HeartbeatRequest(BaseModel):
    """Payload for periodic host lease renewal."""

    session_token: str = Field(..., min_length=16, description="Host session token")
    udp_port: Optional[int] = Field(None, ge=1, le=65535, description="Updated external UDP port")


class ReleaseHostRequest(BaseModel):
    """Payload for voluntary host lease release."""

    session_token: str = Field(..., min_length=16, description="Host session token")
    reason: str = Field("normal_exit", description="Reason for lease release")


class GuestRegisterRequest(BaseModel):
    """Payload for registering a guest connecting to the host."""

    guest_name: str = Field(..., min_length=2, max_length=32, description="Guest player nickname")
    udp_port: Optional[int] = Field(None, ge=1, le=65535, description="Guest local/external UDP port")


class GuestDeregisterRequest(BaseModel):
    """Payload for unregistering a disconnected guest."""

    guest_name: str = Field(..., min_length=2, max_length=32, description="Guest player nickname")


class ModManifestRequest(BaseModel):
    """Payload for uploading or updating mod list manifest."""

    mod_list: Any = Field(..., description="Active mods data (dict or list)")
    session_token: Optional[str] = Field(None, description="Host session token if verifying ownership")


class HealthResponse(BaseModel):
    """Health check response schema."""

    status: str = "ok"
    service: str = "factorio-safe"
    timestamp: str


class PublicStatusResponse(BaseModel):
    """Combined public status containing session state and current save metadata."""

    session: Dict[str, Any]
    save: Dict[str, Any]
    mod_list: Optional[Any] = None


class ClaimResponse(BaseModel):
    """Outcome of claim_host request."""

    success: bool
    session_token: Optional[str] = None
    reconnected: Optional[bool] = None
    reason: Optional[str] = None
    message: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    status: Optional[Dict[str, Any]] = None


class StandardActionResponse(BaseModel):
    """Generic outcome for session lifecycle actions."""

    success: bool
    reason: Optional[str] = None
    message: Optional[str] = None
    status: Optional[Dict[str, Any]] = None
    active_guests: Optional[List[Dict[str, Any]]] = None


class UploadSaveResponse(BaseModel):
    """Response returned upon successful atomic save commit."""

    success: bool
    save: Dict[str, Any]
    status: Dict[str, Any]


class RedeemInviteRequest(BaseModel):
    """Payload to redeem an invite ticket and acquire a permanent device token."""

    ticket: str
    player_name: str


class RedeemInviteResponse(BaseModel):
    """Response returned upon successful invite redemption."""

    success: bool
    device_token: str
    device_id: str
    player_name: str

