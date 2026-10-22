"""
FastAPI HTTP REST API for Factorio P2P Cloud Sync & Coordinator («Сейф»).
Provides session lease orchestration, Split-Brain protection, atomic save streaming,
and mod manifest synchronization on port 8088/tcp.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from src.server.auth_manager import (
    AuthError,
    AuthManager,
    InvalidInviteTicketError,
    InviteTicketAlreadyUsedError,
    InviteTicketExpiredError,
    RateLimitExceededError,
)
from src.server.config import ServerConfig, load_server_config
from src.server.logger import get_logger
from src.server.schemas import (
    BeginFinalUploadRequest,
    ClaimHostRequest,
    ClaimResponse,
    ConfirmHostingRequest,
    GuestDeregisterRequest,
    GuestRegisterRequest,
    HealthResponse,
    HeartbeatRequest,
    ModManifestRequest,
    PublicStatusResponse,
    RedeemInviteRequest,
    RedeemInviteResponse,
    ReleaseHostRequest,
    StandardActionResponse,
    UploadSaveResponse,
)
from src.server.session_manager import SessionLockManager
from src.server.storage import InvalidSaveArchiveError, SaveStorageManager, StorageError

logger = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manages background UDP STUN Coordinator lifecycle alongside HTTP server."""
    stun_transport = None
    stun_protocol = None

    if getattr(app.state, "enable_stun", False):
        try:
            from src.server.stun_coordinator import start_stun_coordinator
            stun_transport, stun_protocol = await start_stun_coordinator(
                config=app.state.config,
                session_manager=app.state.session_manager,
                auth_manager=getattr(app.state, "auth_manager", None),
            )
            app.state.stun_transport = stun_transport
            app.state.stun_protocol = stun_protocol
            logger.info(
                "UDP STUN Coordinator started on %s:%d",
                app.state.config.http_host,
                app.state.config.udp_stun_port,
            )
        except Exception as exc:
            logger.warning("Could not start UDP STUN Coordinator in lifespan: %s", exc)

    yield

    if stun_transport:
        try:
            stun_transport.close()
            logger.info("UDP STUN Coordinator transport closed")
        except Exception as exc:
            logger.warning("Error closing UDP STUN transport: %s", exc)


def get_client_ip(request: Request) -> str:
    """
    Extracts the client's public IP address, respecting X-Forwarded-For if reverse-proxied.

    Args:
        request: FastAPI HTTP request object.

    Returns:
        str: Resolved remote client IP.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    if request.client and request.client.host:
        return request.client.host

    return "127.0.0.1"


def create_app(
    config: Optional[ServerConfig] = None,
    storage: Optional[SaveStorageManager] = None,
    session_manager: Optional[SessionLockManager] = None,
    auth_manager: Optional[AuthManager] = None,
    enable_stun: bool = False,
) -> FastAPI:
    """
    Application factory for the Factorio Safe HTTP API.

    Args:
        config: Optional pre-configured ServerConfig.
        storage: Optional SaveStorageManager instance.
        session_manager: Optional SessionLockManager instance.
        auth_manager: Optional AuthManager instance for device token verification.
        enable_stun: If True, automatically launches UDP STUN Coordinator in lifespan.

    Returns:
        FastAPI: Fully configured ASGI application.
    """
    app_config = config or load_server_config()
    app_storage = storage or SaveStorageManager(app_config)
    app_session = session_manager or SessionLockManager(app_config)
    app_auth = auth_manager or AuthManager(app_config)

    app = FastAPI(
        title="Factorio P2P Safe & Coordinator API",
        version="1.0.0",
        description="Coordinates cloud saves, locks host lease to prevent Split-Brain, and synchronizes mods.",
        lifespan=lifespan,
    )

    # Attach instances to app state for lifecycle access
    app.state.config = app_config
    app.state.storage = app_storage
    app.state.session_manager = app_session
    app.state.auth_manager = app_auth
    app.state.enable_stun = enable_stun

    async def verify_device_token(
        request: Request,
        x_device_token: Optional[str] = Header(None, alias="X-Device-Token"),
    ) -> None:
        """Verifies client device token against AuthManager trusted devices table."""
        client_ip = get_client_ip(request)
        if not app_auth.validate_device_token(x_device_token, client_ip=client_ip):
            logger.warning(
                "Access denied (403 Forbidden): invalid or missing X-Device-Token from %s for %s",
                client_ip,
                request.url.path,
            )
            raise HTTPException(
                status_code=403,
                detail="Forbidden: valid X-Device-Token is required to access Factorio Hub",
            )

    @app.get("/health", response_model=HealthResponse)
    async def health_check() -> Dict[str, Any]:
        """Health check endpoint to verify daemon vitality."""
        return {
            "status": "ok",
            "service": "factorio-safe",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.post("/auth/redeem-invite", response_model=RedeemInviteResponse)
    async def redeem_invite(req: RedeemInviteRequest, request: Request) -> RedeemInviteResponse:
        """
        Public onboarding endpoint: redeems a one-time invite ticket and issues a permanent device_token.
        Rate limited per client IP to prevent brute-force attacks.
        """
        client_ip = get_client_ip(request)
        try:
            result = app_auth.redeem_invite(
                ticket=req.ticket,
                player_name=req.player_name,
                client_ip=client_ip,
            )
            return RedeemInviteResponse(**result)
        except RateLimitExceededError as rle:
            logger.warning("Redeem invite rate limit hit for IP %s: %s", client_ip, rle)
            raise HTTPException(status_code=429, detail=str(rle))
        except (InvalidInviteTicketError, InviteTicketExpiredError, InviteTicketAlreadyUsedError) as auth_err:
            logger.warning("Redeem invite rejected for IP %s: %s", client_ip, auth_err)
            raise HTTPException(status_code=403, detail=str(auth_err))
        except ValueError as val_err:
            raise HTTPException(status_code=422, detail=str(val_err))
        except Exception as exc:
            logger.error("Unexpected error during redeem_invite: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @app.get("/status", dependencies=[Depends(verify_device_token)])
    async def get_status() -> Dict[str, Any]:
        """
        Returns full public status combining session lock information,
        save file metadata, and active mod manifest.
        """
        try:
            session_status = app_session.get_public_status()
            save_meta = app_storage.get_current_save_metadata()
            mod_manifest = app_storage.get_mod_manifest()

            return {
                "session": session_status,
                "save": save_meta,
                "mod_list": mod_manifest.get("mod_list"),
                # Flattened convenience aliases for clients
                "state": session_status.get("state"),
                "host_name": session_status.get("host_name"),
                "host_public_ip": session_status.get("host_public_ip"),
                "host_udp_port": session_status.get("host_udp_port"),
                "lease_remaining_seconds": session_status.get("lease_remaining_seconds"),
                "active_guests": session_status.get("active_guests", []),
                "save_exists": save_meta.get("exists", False),
                "sha256": save_meta.get("sha256"),
                "version_counter": save_meta.get("version_counter", 0),
            }
        except Exception as exc:
            logger.error("Failed to assemble public status: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": "status_error", "message": str(exc)},
            )

    @app.post("/claim", dependencies=[Depends(verify_device_token)])
    async def claim_host(req: ClaimHostRequest, request: Request) -> JSONResponse:
        """
        Attempts to claim host lease for a player launching Factorio.
        Returns session_token on success, or wait parameters if uploading_final.
        """
        client_ip = get_client_ip(request)
        logger.info("Claim attempt from '%s' at IP %s (local: %s)", req.player_name, client_ip, req.local_ip)

        try:
            result = app_session.claim_host(
                player_name=req.player_name,
                client_public_ip=client_ip,
                client_local_ip=req.local_ip,
            )

            if not result.get("success"):
                reason = result.get("reason")
                if reason == "invalid_player_name":
                    return JSONResponse(status_code=400, content=result)
                # already_hosted or uploading_final return 200 with success=False to allow client to inspect retry_after
                return JSONResponse(status_code=200, content=result)

            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.error("Error during claim_host for '%s': %s", req.player_name, exc, exc_info=True)
            return JSONResponse(status_code=500, content={"error": "claim_failed", "message": str(exc)})

    @app.post("/confirm-hosting", dependencies=[Depends(verify_device_token)])
    async def confirm_hosting(req: ConfirmHostingRequest) -> JSONResponse:
        """
        Transitions host from 'claiming' to 'hosting_active' once the lobby is listening.
        """
        try:
            result = app_session.confirm_hosting(
                session_token=req.session_token,
                host_udp_port=req.udp_port,
            )

            if not result.get("success"):
                reason = result.get("reason")
                if reason == "invalid_token":
                    return JSONResponse(status_code=403, content=result)
                if reason == "invalid_state":
                    return JSONResponse(status_code=409, content=result)
                return JSONResponse(status_code=400, content=result)

            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.error("Error during confirm_hosting: %s", exc, exc_info=True)
            return JSONResponse(status_code=500, content={"error": "confirm_failed", "message": str(exc)})

    @app.post("/heartbeat", dependencies=[Depends(verify_device_token)])
    async def heartbeat(req: HeartbeatRequest) -> JSONResponse:
        """
        Renews the host lease timestamp to prevent network disconnect expiration.
        """
        try:
            result = app_session.heartbeat(
                session_token=req.session_token,
                host_udp_port=req.udp_port,
            )

            if not result.get("success"):
                reason = result.get("reason")
                if reason == "invalid_token":
                    return JSONResponse(status_code=403, content=result)
                if reason == "not_hosting":
                    return JSONResponse(status_code=409, content=result)
                return JSONResponse(status_code=400, content=result)

            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.error("Error during heartbeat: %s", exc, exc_info=True)
            return JSONResponse(status_code=500, content={"error": "heartbeat_failed", "message": str(exc)})

    @app.post("/begin-final-upload", dependencies=[Depends(verify_device_token)])
    async def begin_final_upload(req: BeginFinalUploadRequest) -> JSONResponse:
        """
        Signals server that host is closing the game and starting final world save upload.
        Transitions state to 'uploading_final'.
        """
        try:
            result = app_session.begin_final_upload(session_token=req.session_token)

            if not result.get("success"):
                if result.get("reason") == "invalid_token":
                    return JSONResponse(status_code=403, content=result)
                return JSONResponse(status_code=400, content=result)

            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.error("Error during begin_final_upload: %s", exc, exc_info=True)
            return JSONResponse(status_code=500, content={"error": "upload_start_failed", "message": str(exc)})

    @app.post("/release", dependencies=[Depends(verify_device_token)])
    async def release_host(req: ReleaseHostRequest) -> JSONResponse:
        """
        Releases host lease and resets session state to 'idle'.
        """
        try:
            result = app_session.release_host(session_token=req.session_token, reason=req.reason)

            if not result.get("success"):
                if result.get("reason") == "invalid_token":
                    return JSONResponse(status_code=403, content=result)
                return JSONResponse(status_code=400, content=result)

            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.error("Error during release_host: %s", exc, exc_info=True)
            return JSONResponse(status_code=500, content={"error": "release_failed", "message": str(exc)})

    @app.post("/guests/register", dependencies=[Depends(verify_device_token)])
    async def register_guest(req: GuestRegisterRequest, request: Request) -> JSONResponse:
        """
        Registers a guest player currently connected to the host.
        """
        client_ip = get_client_ip(request)
        try:
            result = app_session.register_guest(
                guest_name=req.guest_name,
                guest_public_ip=client_ip,
                guest_udp_port=req.udp_port,
            )
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.error("Error during register_guest: %s", exc, exc_info=True)
            return JSONResponse(status_code=500, content={"error": "guest_register_failed", "message": str(exc)})

    @app.post("/guests/deregister", dependencies=[Depends(verify_device_token)])
    async def deregister_guest(req: GuestDeregisterRequest) -> JSONResponse:
        """
        Deregisters a disconnected guest.
        """
        try:
            result = app_session.deregister_guest(guest_name=req.guest_name)
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.error("Error during deregister_guest: %s", exc, exc_info=True)
            return JSONResponse(status_code=500, content={"error": "guest_deregister_failed", "message": str(exc)})

    @app.get("/download-save", dependencies=[Depends(verify_device_token)])
    async def download_save(
        request: Request,
        client_sha256: Optional[str] = None,
        if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
    ) -> Response:
        """
        Streams the active cloud world save file (cloud_world.zip).
        Supports ETag and client_sha256 caching (returns 304 Not Modified if unchanged).
        """
        meta = app_storage.get_current_save_metadata()
        current_save_path = app_config.current_save_path

        if not meta.get("exists") or not current_save_path.exists():
            raise HTTPException(status_code=404, detail="No active cloud save exists on the server")

        server_sha = meta.get("sha256", "")
        etag_val = f'"{server_sha}"'

        # Check client cache matches
        if client_sha256 and client_sha256.strip().lower() == server_sha.lower():
            logger.debug("download-save: 304 Not Modified (matched client_sha256: %s)", client_sha256)
            return Response(status_code=304, headers={"ETag": etag_val, "X-Save-SHA256": server_sha})

        if if_none_match and if_none_match.strip('"').lower() == server_sha.lower():
            logger.debug("download-save: 304 Not Modified (matched If-None-Match: %s)", if_none_match)
            return Response(status_code=304, headers={"ETag": etag_val, "X-Save-SHA256": server_sha})

        # Return full binary archive
        return FileResponse(
            path=str(current_save_path),
            media_type="application/zip",
            filename=app_config.current_save_filename,
            headers={
                "ETag": etag_val,
                "X-Save-SHA256": server_sha,
                "X-Save-Version": str(meta.get("version_counter", 1)),
            },
        )

    @app.post("/upload-save", dependencies=[Depends(verify_device_token)])
    async def upload_save(
        file: UploadFile = File(...),
        session_token: Optional[str] = Form(None),
        x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
        expected_sha256: Optional[str] = Form(None),
        x_expected_sha256: Optional[str] = Header(None, alias="X-Expected-SHA256"),
        mod_list_json: Optional[str] = Form(None),
        release_on_success: bool = Form(True),
    ) -> JSONResponse:
        """
        Uploads and atomically commits a new Factorio cloud world save.

        CRITICAL SPLIT-BRAIN SHIELD:
        Strictly requires valid session_token of the currently active host.
        Rejects unauthorized uploads immediately with 403 Forbidden.
        """
        effective_token = session_token or x_session_token
        effective_sha = expected_sha256 or x_expected_sha256

        # 1. Host Lock Verification
        if not effective_token or not app_session.validate_host_token(effective_token):
            logger.warning(
                "SECURITY: Unauthorized upload attempt rejected (token: %s).",
                effective_token[:8] + "..." if effective_token else "NONE",
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "not_active_host",
                    "message": "Загрузка сейвов разрешена только текущему Хосту",
                    "status": app_session.get_public_status(),
                },
            )

        # 2. Stream upload to temporary staging file in 64 KB chunks
        temp_filename = f"{app_config.temp_save_prefix}{secrets.token_hex(8)}.zip"
        temp_path = app_config.tmp_dir / temp_filename
        hasher = hashlib.sha256()
        total_bytes = 0

        try:
            with open(temp_path, "wb") as f_out:
                while True:
                    chunk = await file.read(64 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > app_config.max_upload_size_bytes:
                        temp_path.unlink(missing_ok=True)
                        logger.warning("Upload rejected: size %d exceeds limit %d", total_bytes, app_config.max_upload_size_bytes)
                        return JSONResponse(
                            status_code=413,
                            content={
                                "error": "file_too_large",
                                "message": f"File size exceeds limit of {app_config.max_upload_size_bytes} bytes",
                            },
                        )
                    hasher.update(chunk)
                    f_out.write(chunk)
        except Exception as io_err:
            temp_path.unlink(missing_ok=True)
            logger.error("I/O error during file upload to %s: %s", temp_path, io_err)
            return JSONResponse(status_code=500, content={"error": "io_error", "message": str(io_err)})

        # 3. Verify SHA-256 Checksum if expected_sha256 provided
        computed_sha = hasher.hexdigest()
        if effective_sha and effective_sha.strip().lower() != computed_sha.lower():
            temp_path.unlink(missing_ok=True)
            logger.warning("Upload SHA-256 mismatch: expected %s, got %s", effective_sha, computed_sha)
            return JSONResponse(
                status_code=400,
                content={
                    "error": "sha256_mismatch",
                    "message": f"Expected SHA-256 {effective_sha}, computed {computed_sha}",
                },
            )

        # 4. Parse optional mod manifest JSON
        parsed_mods = None
        if mod_list_json:
            try:
                parsed_mods = json.loads(mod_list_json)
            except Exception as parse_err:
                temp_path.unlink(missing_ok=True)
                logger.warning("Failed to parse mod_list_json: %s", parse_err)
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_mod_list_json", "message": str(parse_err)},
                )

        # 5. Extract host nickname
        with app_session._lock:
            host_name = app_session._session.host_name or "host"

        # 6. Commit save atomically
        try:
            save_result = app_storage.save_new_world_atomic(
                temp_file_path=temp_path,
                uploaded_by=host_name,
                mod_list=parsed_mods,
            )
        except InvalidSaveArchiveError as bad_archive:
            return JSONResponse(status_code=400, content={"error": "invalid_archive", "message": str(bad_archive)})
        except StorageError as stor_err:
            return JSONResponse(status_code=500, content={"error": "storage_error", "message": str(stor_err)})
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            logger.error("Unexpected error committing save: %s", exc, exc_info=True)
            return JSONResponse(status_code=500, content={"error": "commit_failed", "message": str(exc)})

        # 7. Release host slot if requested
        if release_on_success:
            app_session.release_host(effective_token, reason="final_save_uploaded")

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "save": save_result,
                "status": app_session.get_public_status(),
            },
        )

    @app.get("/mod-manifest", dependencies=[Depends(verify_device_token)])
    async def get_mod_manifest() -> Dict[str, Any]:
        """
        Retrieves the active mod manifest (mod-list.json).
        """
        manifest = app_storage.get_mod_manifest()
        return manifest

    @app.post("/mod-manifest", dependencies=[Depends(verify_device_token)])
    async def post_mod_manifest(req: ModManifestRequest) -> JSONResponse:
        """
        Explicitly stores or updates mod-list.json manifest.
        Verifies session_token if provided.
        """
        if req.session_token and not app_session.validate_host_token(req.session_token):
            return JSONResponse(
                status_code=403,
                content={"error": "not_active_host", "message": "Only active host may update mod manifest"},
            )

        try:
            app_storage.save_mod_manifest(req.mod_list)
            return JSONResponse(status_code=200, content={"success": True, "message": "Mod manifest saved"})
        except Exception as exc:
            logger.error("Failed to save mod manifest: %s", exc)
            return JSONResponse(status_code=500, content={"error": "save_manifest_failed", "message": str(exc)})

    return app


# Default singleton app for ASGI servers (e.g. uvicorn src.server.api:app)
app = create_app()
