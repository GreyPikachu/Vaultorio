"""
Asynchronous Telegram Bot Service for Factorio P2P Cloud Sync & Coordinator.
Provides lightweight Telegram Bot API integration via httpx.AsyncClient (~3-4 MB RAM),
a pinned live status dashboard, interactive inline settings menu, and long-polling updates.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.server.auth_manager import AuthManager
from src.server.config import ServerConfig
from src.server.logger import get_logger
from src.server.session_manager import SessionLockManager
from src.server.storage import SaveStorageManager

logger = get_logger("telegram_bot")


# Schedule modes and labels
AI_SCHEDULE_MODES: List[Tuple[str, str]] = [
    ("hourly_and_events", "Каждый час + события"),
    ("important_only", "Только важные"),
    ("only_on_exit", "Только финал"),
]

# Persona styles and labels
AI_PERSONA_STYLES: List[Tuple[str, str]] = [
    ("reporter", "Репортер Наувиса"),
    ("ship_ai", "Бортовой ИИ"),
    ("friendly", "Дружеский обзор"),
]

# Supported timezone offsets (hours from UTC) for cycling in /settings
DASHBOARD_TIMEZONES: List[Tuple[int, str]] = [
    (0, "UTC (0)"),
    (2, "UTC+2 (КЛД)"),
    (3, "UTC+3 (МСК)"),
    (4, "UTC+4 (СМР)"),
    (5, "UTC+5 (ЕКБ)"),
    (6, "UTC+6 (ОМСК)"),
    (7, "UTC+7 (НСК)"),
    (8, "UTC+8 (ИРК)"),
    (9, "UTC+9 (ЯКТ)"),
    (10, "UTC+10 (ВЛД)"),
]


def format_tz_label(offset_hours: int) -> str:
    """Returns human-readable UTC offset label, e.g. 'UTC', 'UTC+3', or 'UTC-4'."""
    if offset_hours == 0:
        return "UTC"
    sign = "+" if offset_hours > 0 else ""
    return f"UTC{sign}{offset_hours}"


@dataclass
class TelegramBotSettings:
    """
    Persistent configuration for the Telegram Bot and AI chronicle.
    Saved to storage_dir / 'telegram_state.json' with environment variable fallbacks.
    """

    enabled: bool = True
    bot_token: str = ""
    chat_id: Optional[int | str] = None
    pinned_message_id: Optional[int] = None
    dashboard_title: str = "Factorio Server - Live Status"
    live_dashboard_enabled: bool = True
    show_ping_in_dashboard: bool = True
    show_updated_time: bool = True
    dashboard_tz_offset: int = 3

    # Notification forwarding toggles
    notify_host_start: bool = True
    notify_guest_join: bool = True
    notify_guest_leave: bool = True
    notify_rolling_save: bool = False
    notify_session_end: bool = True
    silent_notifications: bool = False

    # AI Chronicle settings (Task 032)
    ai_chronicle_enabled: bool = True
    ai_schedule_mode: str = "hourly_and_events"
    ai_persona_style: str = "reporter"
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-2.0-flash-exp:free"

    # Access control
    admin_user_ids: List[int] = field(default_factory=list)
    known_users: Dict[str, int] = field(default_factory=dict)
    admin_usernames: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializes settings to a dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TelegramBotSettings":
        """Instantiates settings from a dictionary, validating and clamping fields."""
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        if "dashboard_tz_offset" in filtered:
            try:
                offset_val = int(filtered["dashboard_tz_offset"])
                filtered["dashboard_tz_offset"] = max(-12, min(14, offset_val))
            except (ValueError, TypeError):
                filtered["dashboard_tz_offset"] = 3
        if "show_updated_time" in filtered:
            filtered["show_updated_time"] = bool(filtered["show_updated_time"])
        return cls(**filtered)

    def save(self, file_path: Path) -> None:
        """Atomically saves settings to JSON file."""
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = file_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            os.replace(str(tmp_path), str(file_path))
            logger.debug("Saved Telegram bot settings to %s", file_path)
        except Exception as exc:
            logger.error("Failed to save Telegram bot settings to %s: %s", file_path, exc)

    @classmethod
    def load(cls, file_path: Path) -> "TelegramBotSettings":
        """
        Loads settings from file with fallback to environment variables:
        - FACTORIO_TG_BOT_TOKEN
        - FACTORIO_TG_CHAT_ID
        - FACTORIO_OPENROUTER_API_KEY
        """
        instance = cls()
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                instance = cls.from_dict(data)
                logger.info("Loaded Telegram bot settings from %s", file_path)
            except Exception as exc:
                logger.warning("Error reading %s: %s. Using default settings.", file_path, exc)

        # Apply environment overrides if not present in saved state
        if not instance.bot_token:
            env_token = os.getenv("FACTORIO_TG_BOT_TOKEN", "").strip()
            if env_token:
                instance.bot_token = env_token
                logger.info("Using bot_token from FACTORIO_TG_BOT_TOKEN environment variable.")

        if instance.chat_id is None:
            env_chat_id = os.getenv("FACTORIO_TG_CHAT_ID", "").strip()
            if env_chat_id:
                try:
                    instance.chat_id = int(env_chat_id)
                except ValueError:
                    instance.chat_id = env_chat_id
                logger.info("Using chat_id from FACTORIO_TG_CHAT_ID environment variable.")

        if not instance.openrouter_api_key:
            env_or_key = os.getenv("FACTORIO_OPENROUTER_API_KEY", "").strip()
            if env_or_key:
                instance.openrouter_api_key = env_or_key
                logger.info("Using openrouter_api_key from FACTORIO_OPENROUTER_API_KEY environment variable.")

        env_tz = os.getenv("FACTORIO_TG_TZ_OFFSET", "").strip()
        if env_tz:
            try:
                val = int(env_tz)
                instance.dashboard_tz_offset = max(-12, min(14, val))
                logger.info("Using dashboard_tz_offset from FACTORIO_TG_TZ_OFFSET environment variable.")
            except ValueError:
                pass

        return instance


class TelegramBotService:
    """
    Asynchronous Telegram Bot Service using httpx.AsyncClient for low memory usage.
    Handles messaging, dashboard generation, settings menu interaction, and long-polling.
    """

    TELEGRAM_API_BASE = "https://api.telegram.org"
    CAPTION_MAX_LENGTH = 1024

    def __init__(
        self,
        config: ServerConfig,
        session_manager: Optional[SessionLockManager] = None,
        storage_manager: Optional[SaveStorageManager] = None,
        auth_manager: Optional[AuthManager] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """
        Initializes the Telegram bot service.

        Args:
            config: Server configuration containing storage paths.
            session_manager: Active multiplayer session manager.
            storage_manager: World save storage manager.
            auth_manager: Friend ticket and device auth manager.
            client: Optional httpx.AsyncClient instance for testing / reuse.
        """
        self.config = config
        self.session_manager = session_manager
        self.storage_manager = storage_manager
        self.auth_manager = auth_manager

        self.state_file = config.telegram_state_path
        self.settings = TelegramBotSettings.load(self.state_file)

        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
        self._owns_client = client is None

        self._last_dashboard_hash: Optional[str] = None
        self._last_dashboard_structural_hash: Optional[str] = None
        self._last_dashboard_edit_ts: float = 0.0
        self._polling_task: Optional[asyncio.Task] = None
        self._dashboard_timer_task: Optional[asyncio.Task] = None
        self._is_polling = False
        self._update_offset = 0
        self._wizard_state: Dict[int, Dict[str, Any]] = {}

    @property
    def is_configured(self) -> bool:
        """Returns True if bot is enabled and possesses a bot token."""
        return bool(self.settings.enabled and self.settings.bot_token)

    @property
    def bot_url(self) -> str:
        """Base URL for Telegram Bot API calls."""
        return f"{self.TELEGRAM_API_BASE}/bot{self.settings.bot_token}"

    def save_settings(self) -> None:
        """Persists current settings to disk."""
        self.settings.save(self.state_file)

    def format_time_with_tz(
        self,
        dt: Optional[datetime] = None,
        fmt: str = "%H:%M:%S",
    ) -> str:
        """
        Formats a datetime in the configured dashboard_tz_offset timezone.
        Appends human-readable UTC offset label (e.g. '17:30:00 UTC+3').
        """
        offset = max(-12, min(14, int(self.settings.dashboard_tz_offset)))
        tz = timezone(timedelta(hours=offset))
        target_dt = dt.astimezone(tz) if dt is not None else datetime.now(tz)
        return f"{target_dt.strftime(fmt)} {format_tz_label(offset)}"

    async def close(self) -> None:
        """Stops background tasks and releases HTTP client resources."""
        await self.stop_polling()
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.debug("Error closing HTTP client: %s", exc)

    # --------------------------------------------------------------------------
    # Telegram Bot API Methods
    # --------------------------------------------------------------------------

    async def send_message(
        self,
        text: str,
        chat_id: Optional[int | str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_notification: Optional[bool] = None,
        parse_mode: str = "HTML",
    ) -> Optional[Dict[str, Any]]:
        """
        Sends an HTML-formatted message to the specified or configured chat.

        Args:
            text: Message body.
            chat_id: Target chat ID. Falls back to self.settings.chat_id.
            reply_markup: Optional inline keyboard markup.
            disable_notification: Sends message silently if True.
            parse_mode: Formatting mode ('HTML' by default).

        Returns:
            Dict[str, Any] representing sent Message, or None on failure.
        """
        if not self.is_configured:
            logger.debug("Cannot send_message: Bot is not configured.")
            return None

        target_chat = chat_id or self.settings.chat_id
        if not target_chat:
            logger.warning("Cannot send_message: No chat_id provided or configured.")
            return None

        if disable_notification is None:
            disable_notification = self.settings.silent_notifications

        payload: Dict[str, Any] = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        url = f"{self.bot_url}/sendMessage"
        try:
            resp = await self._client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                logger.error("sendMessage failed: %s", data.get("description"))
                return None
            return data.get("result")
        except Exception as exc:
            logger.error("Network error during sendMessage: %s", exc)
            return None

    async def edit_message_text(
        self,
        message_id: int,
        text: str,
        chat_id: Optional[int | str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML",
    ) -> Optional[Dict[str, Any]]:
        """
        Edits an existing text message. Handles 'message is not modified' gracefully.

        Args:
            message_id: ID of the message to edit.
            text: Updated text content.
            chat_id: Target chat ID. Falls back to self.settings.chat_id.
            reply_markup: Optional inline keyboard markup.
            parse_mode: Formatting mode ('HTML' by default).

        Returns:
            Dict[str, Any] with updated Message, or None on failure.
        """
        if not self.is_configured:
            return None

        target_chat = chat_id or self.settings.chat_id
        if not target_chat:
            return None

        payload: Dict[str, Any] = {
            "chat_id": target_chat,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        url = f"{self.bot_url}/editMessageText"
        try:
            resp = await self._client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "")
                if "message is not modified" in desc.lower():
                    logger.debug("editMessageText: Message content is identical.")
                    return {"message_id": message_id, "status": "not_modified"}
                logger.warning("editMessageText failed (msg_id: %d): %s", message_id, desc)
                return None
            return data.get("result")
        except Exception as exc:
            logger.error("Network error during editMessageText: %s", exc)
            return None

    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str = "",
        filename: str = "screenshot.jpg",
        chat_id: Optional[int | str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_notification: Optional[bool] = None,
        parse_mode: str = "HTML",
    ) -> Optional[Dict[str, Any]]:
        """
        Sends a photo with caption. Automatically splits caption if length exceeds
        Telegram's 1024-character limit to avoid 400 Bad Request errors.

        Args:
            photo_bytes: Raw JPEG/PNG image binary data.
            caption: Descriptive text or AI chronicle summary.
            filename: Multipart filename.
            chat_id: Target chat ID. Falls back to self.settings.chat_id.
            reply_markup: Optional inline keyboard markup.
            disable_notification: Sends message silently if True.
            parse_mode: Formatting mode ('HTML' by default).

        Returns:
            Dict[str, Any] representing sent photo Message, or None on failure.
        """
        if not self.is_configured:
            return None

        target_chat = chat_id or self.settings.chat_id
        if not target_chat:
            return None

        if disable_notification is None:
            disable_notification = self.settings.silent_notifications

        # Handle caption splitting if exceeding Telegram caption limit (1024 chars)
        followup_text = ""
        photo_caption = caption
        if len(caption) > self.CAPTION_MAX_LENGTH:
            # Find a convenient boundary (paragraph break, line break, or character cutoff)
            cutoff = 950
            break_idx = caption.rfind("\n\n", 0, cutoff)
            if break_idx == -1:
                break_idx = caption.rfind("\n", 0, cutoff)
            if break_idx == -1:
                break_idx = cutoff

            photo_caption = caption[:break_idx].strip()
            followup_text = caption[break_idx:].strip()

        data_fields: Dict[str, Any] = {
            "chat_id": str(target_chat),
            "caption": photo_caption,
            "parse_mode": parse_mode,
            "disable_notification": str(disable_notification).lower(),
        }
        if reply_markup and not followup_text:
            data_fields["reply_markup"] = json.dumps(reply_markup)

        files = {
            "photo": (filename, photo_bytes, "image/jpeg"),
        }

        url = f"{self.bot_url}/sendPhoto"
        try:
            resp = await self._client.post(url, data=data_fields, files=files)
            result_data = resp.json()
            if not result_data.get("ok"):
                logger.error("sendPhoto failed: %s", result_data.get("description"))
                return None

            result_msg = result_data.get("result")

            # Send remaining chronicle text as follow-up if caption was split
            if followup_text:
                await self.send_message(
                    text=followup_text,
                    chat_id=target_chat,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    parse_mode=parse_mode,
                )

            return result_msg
        except Exception as exc:
            logger.error("Network error during sendPhoto: %s", exc)
            return None

    async def pin_chat_message(
        self,
        message_id: int,
        chat_id: Optional[int | str] = None,
        disable_notification: bool = True,
    ) -> bool:
        """
        Pins a message in the chat.

        Args:
            message_id: ID of the message to pin.
            chat_id: Target chat ID.
            disable_notification: Mutes pin notification.

        Returns:
            bool: True if pinned successfully.
        """
        if not self.is_configured:
            return False

        target_chat = chat_id or self.settings.chat_id
        if not target_chat:
            return False

        payload = {
            "chat_id": target_chat,
            "message_id": message_id,
            "disable_notification": disable_notification,
        }
        url = f"{self.bot_url}/pinChatMessage"
        try:
            resp = await self._client.post(url, json=payload)
            data = resp.json()
            return bool(data.get("ok"))
        except Exception as exc:
            logger.error("Network error during pinChatMessage: %s", exc)
            return False

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        """
        Acknowledges a button click callback query.

        Args:
            callback_query_id: Unique callback query identifier.
            text: Optional notification message for the user.
            show_alert: Shows an alert modal if True.

        Returns:
            bool: True if acknowledged successfully.
        """
        if not self.is_configured:
            return False

        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        url = f"{self.bot_url}/answerCallbackQuery"
        try:
            resp = await self._client.post(url, json=payload)
            data = resp.json()
            return bool(data.get("ok"))
        except Exception as exc:
            logger.error("Network error during answerCallbackQuery: %s", exc)
            return False

    async def delete_message(
        self,
        message_id: int,
        chat_id: Optional[int | str] = None,
    ) -> bool:
        """
        Deletes a message from the chat.

        Args:
            message_id: ID of the message to delete.
            chat_id: Target chat ID.

        Returns:
            bool: True if deleted successfully.
        """
        if not self.is_configured:
            return False

        target_chat = chat_id or self.settings.chat_id
        if not target_chat:
            return False

        payload = {
            "chat_id": target_chat,
            "message_id": message_id,
        }
        url = f"{self.bot_url}/deleteMessage"
        try:
            resp = await self._client.post(url, json=payload)
            data = resp.json()
            return bool(data.get("ok"))
        except Exception as exc:
            logger.error("Network error during deleteMessage: %s", exc)
            return False

    # --------------------------------------------------------------------------
    # Event Notification Dispatchers
    # --------------------------------------------------------------------------

    async def notify_host_started(self, host_name: str) -> None:
        """
        Sends an alert when a host starts hosting the game and forces dashboard refresh.

        Args:
            host_name: Nickname of the host player.
        """
        if not self.is_configured:
            return

        try:
            if self.settings.notify_host_start:
                text = f"<b>[СЕРВЕР]</b> Игрок <b>{html.escape(host_name)}</b> запустил игру!"
                await self.send_message(text=text)
            await self.update_dashboard(force=True)
        except Exception as exc:
            logger.error("Error in notify_host_started: %s", exc)

    async def notify_guest_joined(self, guest_name: str) -> None:
        """
        Sends an alert when a guest connects to the multiplayer session and refreshes dashboard.

        Args:
            guest_name: Nickname of the connecting guest player.
        """
        if not self.is_configured:
            return

        try:
            if self.settings.notify_guest_join:
                text = f"<b>[СЕРВЕР]</b> Игрок <b>{html.escape(guest_name)}</b> подключился к игре."
                await self.send_message(text=text)
            await self.update_dashboard(force=True)
        except Exception as exc:
            logger.error("Error in notify_guest_joined: %s", exc)

    async def notify_guest_left(self, guest_name: str) -> None:
        """
        Sends an alert when a guest disconnects from the multiplayer session and refreshes dashboard.

        Args:
            guest_name: Nickname of the leaving guest player.
        """
        if not self.is_configured:
            return

        try:
            if self.settings.notify_guest_leave:
                text = f"<b>[СЕРВЕР]</b> Игрок <b>{html.escape(guest_name)}</b> покинул игру."
                await self.send_message(text=text)
            await self.update_dashboard(force=True)
        except Exception as exc:
            logger.error("Error in notify_guest_left: %s", exc)

    async def notify_rolling_save_saved(
        self,
        version: int,
        size_mb: float,
        uploaded_by: str,
    ) -> None:
        """
        Sends an alert when an intermediate autosave is backed up to the cloud safe.

        Args:
            version: Save file increment version.
            size_mb: Save archive size in megabytes.
            uploaded_by: Player who uploaded the save.
        """
        if not self.is_configured:
            return

        try:
            if self.settings.notify_rolling_save:
                text = (
                    f"<b>[СЕЙВ]</b> Автосейв v{version} ({size_mb:.1f} МБ) сохранен в сейф.\n"
                    f"Игрок: <b>{html.escape(uploaded_by)}</b>"
                )
                await self.send_message(text=text)
            await self.update_dashboard(force=True)
        except Exception as exc:
            logger.error("Error in notify_rolling_save_saved: %s", exc)

    async def notify_session_ended(
        self,
        host_name: Optional[str] = None,
        duration_str: Optional[str] = None,
        version: Optional[int] = None,
        size_mb: Optional[float] = None,
    ) -> None:
        """
        Sends an alert when a multiplayer session ends and the final world save is archived.

        Args:
            host_name: Nickname of the host player.
            duration_str: Formatted string representing session duration (e.g. '1 ч 20 мин').
            version: Save file increment version.
            size_mb: Save archive size in megabytes.
        """
        if not self.is_configured:
            return

        try:
            if self.settings.notify_session_end:
                details = []
                if host_name:
                    details.append(f"Хост: <b>{html.escape(host_name)}</b>")
                if duration_str:
                    details.append(f"Время в игре: {duration_str}")
                if version is not None and size_mb is not None:
                    details.append(f"Финальный мир: v{version} ({size_mb:.1f} МБ)")

                body_lines = "\n".join(details)
                text = (
                    f"<b>[СЕРВЕР]</b> Сессия завершена. Мир успешно выгружен в сейф.\n"
                    f"{body_lines}"
                )
                await self.send_message(text=text)
            await self.update_dashboard(force=True)
        except Exception as exc:
            logger.error("Error in notify_session_ended: %s", exc)

    async def notify_world_archived(
        self,
        world_name: str,
        archived_by: str,
        archive_name: Optional[str] = None,
    ) -> None:
        """
        Sends an alert when the active world is removed to archive, leaving the safe empty.
        Updates pinned live dashboard message.

        Args:
            world_name: Name of the world that was archived.
            archived_by: Player name or admin role that performed the action.
            archive_name: Optional name of the archive file on VPS.
        """
        if not self.is_configured:
            return

        try:
            w_name = html.escape(world_name)
            p_name = html.escape(archived_by)
            text = (
                f"<b>[СЕЙФ]</b> Мир <b>{w_name} [Cloud]</b> убран с сервера в архив (игрок: <b>{p_name}</b>).\n"
                "Сейчас на сервере не выбран никакой сейф — слот свободен для выбора или загрузки другого мира."
            )
            await self.send_message(text=text)
            await self.update_dashboard(force=True)
        except Exception as exc:
            logger.error("Error in notify_world_archived: %s", exc)

    # --------------------------------------------------------------------------
    # Pinned Live Dashboard
    # --------------------------------------------------------------------------

    def format_dashboard_html(self) -> str:
        """
        Formats a clean, informative HTML status card for the live dashboard.
        Strictly contains no emojis.
        """
        # Session state
        state = "idle"
        host_name = None
        started_at = None
        guests: List[Dict[str, Any]] = []

        if self.session_manager:
            status = self.session_manager.get_public_status()
            state = status.get("state", "idle")
            host_name = status.get("host_name")
            started_at = status.get("started_at")
            guests = status.get("active_guests", [])

        # State label
        if state == "idle":
            state_label = "[Свободен]"
        elif state == "claiming":
            state_label = "[Подготовка...]"
        elif state == "hosting_active":
            state_label = "[В игре!]"
        elif state == "uploading_final":
            state_label = "[Выгрузка сохранения...]"
        else:
            state_label = f"[{state}]"

        lines = [
            f"<b>{html.escape(self.settings.dashboard_title)}</b>",
            f"Статус: <b>{state_label}</b>",
        ]

        if host_name:
            lines.append(f"Хост: <b>{html.escape(str(host_name))}</b>")
            if started_at:
                elapsed_seconds = max(0, int(time.time() - started_at))
                hours = elapsed_seconds // 3600
                minutes = (elapsed_seconds % 3600) // 60
                lines.append(f"Время в игре: {hours} ч {minutes} мин")

        if guests:
            has_metrics = any(
                ("ping_ms" in g or "direct_p2p" in g or "mode" in g)
                for g in guests
            )
            if not self.settings.show_ping_in_dashboard or not has_metrics:
                guest_names = [html.escape(str(g.get("name", "Игрок"))) for g in guests]
                lines.append(f"Гости ({len(guests)}): {', '.join(guest_names)}")
            else:
                lines.append(f"Гости ({len(guests)}):")
                for g in guests:
                    g_name = html.escape(str(g.get("name", "Игрок")))
                    direct = g.get("direct_p2p", True)
                    raw_mode = g.get("mode")
                    g_mode = "Relay" if (raw_mode == "Relay" or not direct) else "P2P"
                    ping_val = g.get("ping_ms")
                    if ping_val is not None:
                        try:
                            ping_str = f"{int(round(float(ping_val)))} мс"
                        except (ValueError, TypeError):
                            ping_str = "замер..."
                    else:
                        ping_str = "замер..."
                    lines.append(f"• {g_name} — {ping_str} [{g_mode}]")
        elif state == "hosting_active":
            lines.append("Гости: нет подключений")

        # Save metadata
        if self.storage_manager:
            meta = self.storage_manager.get_current_save_metadata()
            if meta.get("exists"):
                version = meta.get("version_counter", meta.get("version", 1))
                size_mb = meta.get("size_bytes", 0) / (1024 * 1024)
                uploaded_by = html.escape(str(meta.get("uploaded_by", "Неизвестно")))
                world_name = meta.get("world_name")
                if world_name:
                    lines.append(f"Мир в сейфе: <b>{html.escape(str(world_name))} [Cloud]</b> — v{version} ({size_mb:.1f} МБ)")
                else:
                    lines.append(f"Мир в сейфе: v{version} ({size_mb:.1f} МБ)")
                lines.append(f"Сохранил: {uploaded_by}")
            else:
                lines.append("Мир в сейфе: нет активного сохранения")
                lines.append("Слот сервера: свободен для выбора нового мира")

        if self.settings.show_updated_time:
            now_str = self.format_time_with_tz(fmt="%H:%M:%S")
            lines.append(f"\n<i>Обновлено: {now_str}</i>")

        return "\n".join(lines)

    @staticmethod
    def _get_dashboard_structural_hash(text: str) -> str:
        """
        Computes SHA-256 hash of dashboard text stripped of dynamic ping numbers
        and update timestamp to isolate structural changes from network jitter.
        """
        cleaned = re.sub(r"<i>Обновлено:.*?</i>", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"\d+\s*мс", "X мс", cleaned)
        return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

    def build_dashboard_keyboard(self) -> Dict[str, Any]:
        """Builds inline keyboard markup for the pinned live dashboard."""
        return {
            "inline_keyboard": [
                [
                    {"text": "Обновить статус", "callback_data": "dash:refresh"},
                    {"text": "Миры в сейфе", "callback_data": "menu:worlds"},
                ],
                [
                    {"text": "Настройки бота", "callback_data": "menu:open"},
                ],
            ]
        }

    async def update_dashboard(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """
        Updates the pinned live dashboard message. Deduplicates updates via content hash.
        Creates and pins a new message if none exists or if editing fails.

        Args:
            force: Skips content hash check when True.

        Returns:
            Dict[str, Any] with message details, or None if skipped / disabled.
        """
        if not self.is_configured or not self.settings.live_dashboard_enabled:
            return None

        if not self.settings.chat_id:
            logger.debug("update_dashboard: No chat_id configured.")
            return None

        text = self.format_dashboard_html()
        keyboard = self.build_dashboard_keyboard()

        # Deduplicate using content hash (excluding timestamp to prevent ticking second spam)
        hash_source = re.sub(r"\n?<i>Обновлено:.*?</i>", "", text, flags=re.DOTALL)
        content_hash = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()
        structural_hash = self._get_dashboard_structural_hash(text)
        now_mono = time.monotonic()

        if not force:
            if content_hash == self._last_dashboard_hash:
                logger.debug("update_dashboard: Content unchanged, skipping API call.")
                return None

            # Anti-jitter throttling: if structural content (players, modes, save, state)
            # did not change and only ping ms fluctuated, throttle to at most once per 25s
            if (
                self._last_dashboard_structural_hash is not None
                and self._last_dashboard_structural_hash == structural_hash
                and (now_mono - self._last_dashboard_edit_ts) < 25.0
            ):
                logger.debug(
                    "update_dashboard: Throttling ping jitter (elapsed: %.1fs < 25.0s), skipping API call.",
                    now_mono - self._last_dashboard_edit_ts,
                )
                return None

        msg_id = self.settings.pinned_message_id
        result = None

        if msg_id:
            # Attempt to edit existing pinned message
            result = await self.edit_message_text(
                message_id=msg_id,
                text=text,
                reply_markup=keyboard,
            )
            if not result or result.get("status") == "not_modified":
                if result and result.get("status") == "not_modified":
                    self._last_dashboard_hash = content_hash
                    self._last_dashboard_structural_hash = structural_hash
                    self._last_dashboard_edit_ts = now_mono
                    return result
                # Edit failed: message might have been deleted by user
                logger.info("Pinned dashboard message %d could not be edited. Creating new.", msg_id)
                self.settings.pinned_message_id = None
                self.save_settings()

        if not self.settings.pinned_message_id:
            # Send new message and pin it
            result = await self.send_message(text=text, reply_markup=keyboard)
            if result and "message_id" in result:
                new_msg_id = result["message_id"]
                self.settings.pinned_message_id = new_msg_id
                self.save_settings()
                pinned_ok = await self.pin_chat_message(new_msg_id, disable_notification=True)
                logger.info("Created new dashboard message %d (pinned: %s)", new_msg_id, pinned_ok)

        self._last_dashboard_hash = content_hash
        self._last_dashboard_structural_hash = structural_hash
        self._last_dashboard_edit_ts = now_mono
        return result

    def format_ping_report_html(self) -> str:
        """
        Formats a network latency and tunnel mode diagnostic report.
        Strictly zero emojis.
        """
        lines = ["<b>Сетевая диагностика (P2P / Relay)</b>\n"]

        state = "idle"
        host_name = None
        udp_port = None
        guests: List[Dict[str, Any]] = []

        if self.session_manager:
            status = self.session_manager.get_public_status()
            state = status.get("state", "idle")
            host_name = status.get("host_name")
            udp_port = status.get("host_udp_port")
            guests = status.get("active_guests", [])

        if state == "idle":
            lines.append("Статус: <b>[Свободен]</b> — активной игровой сессии нет.")
            lines.append("Координатор VPS: <b>Онлайн (&lt;1 мс)</b>")
        else:
            h_name = html.escape(str(host_name or "Неизвестно"))
            port_str = str(udp_port) if udp_port else "авто"
            lines.append(f"Хост лобби: <b>{h_name}</b> (UDP порт: {port_str})")
            if not guests:
                lines.append("Подключенные гости: <b>нет</b>")
            else:
                lines.append(f"\n<b>Подключенные гости ({len(guests)}):</b>")
                for g in guests:
                    g_name = html.escape(str(g.get("name", "Игрок")))
                    direct = g.get("direct_p2p", True)
                    raw_mode = g.get("mode")
                    g_mode = "Relay" if (raw_mode == "Relay" or not direct) else "P2P"
                    ping_val = g.get("ping_ms")
                    if ping_val is not None:
                        try:
                            ping_str = f"{int(round(float(ping_val)))} мс"
                        except (ValueError, TypeError):
                            ping_str = "замер..."
                    else:
                        ping_str = "замер..."
                    lines.append(f"• <b>{g_name}</b> — {ping_str} [{g_mode}]")

            lines.append("\n<i>[P2P] — прямое соединение между игроками; [Relay] — ретрансляция через VPS.</i>")

        return "\n".join(lines)

    async def _handle_ping_command(self, chat_id: int) -> None:
        """Sends the network ping and tunnel mode diagnostic report to chat."""
        report = self.format_ping_report_html()
        await self.send_message(chat_id=chat_id, text=report)

    # --------------------------------------------------------------------------
    # Interactive Settings Menu
    # --------------------------------------------------------------------------

    @staticmethod
    def get_setting_subpage(field_name: str) -> str:
        """Determines which settings submenu a setting belongs to."""
        if field_name.startswith("notify_") or field_name == "silent_notifications":
            return "notifications"
        if field_name in (
            "live_dashboard_enabled",
            "show_ping_in_dashboard",
            "show_updated_time",
            "dashboard_tz_offset",
        ):
            return "dashboard"
        if field_name in ("ai_chronicle_enabled", "ai_schedule_mode", "ai_persona_style"):
            return "chronicle"
        return "main"

    def format_settings_menu_html(self, subpage: str = "main") -> str:
        """Formats the settings menu overview or submenu text."""
        if subpage == "notifications":
            return (
                "<b>Настройки уведомлений</b>\n\n"
                "Выберите, о каких игровых событиях бот должен присылать оповещения в чат:"
            )
        if subpage == "dashboard":
            return (
                "<b>Настройки закрепленного дашборда</b>\n\n"
                "Управление живым закрепленным статусом сервера и отображаемыми данными:"
            )
        if subpage == "chronicle":
            return (
                "<b>Настройки ИИ-хроники Наувиса</b>\n\n"
                "Параметры генерации иллюстрированных новостей и отчетов нейросетью:"
            )
        return (
            "<b>Настройки Telegram-бота Factorio</b>\n\n"
            "Выберите категорию для настройки параметров:"
        )

    def build_settings_keyboard(self, subpage: str = "main") -> Dict[str, Any]:
        """
        Constructs the interactive settings menu inline keyboard for the specified subpage.
        Strictly zero emojis.
        """
        def toggle_label(name: str, state: bool) -> str:
            status = "[ВКЛ]" if state else "[ВЫКЛ]"
            return f"{name}: {status}"

        # Schedule mode label lookup
        curr_sched = dict(AI_SCHEDULE_MODES).get(self.settings.ai_schedule_mode, "Каждый час")
        curr_style = dict(AI_PERSONA_STYLES).get(self.settings.ai_persona_style, "Репортер")
        tz_map = dict(DASHBOARD_TIMEZONES)
        curr_tz_label = tz_map.get(
            self.settings.dashboard_tz_offset,
            format_tz_label(self.settings.dashboard_tz_offset),
        )

        if subpage == "notifications":
            keyboard = [
                [
                    {
                        "text": toggle_label("Хост старт", self.settings.notify_host_start),
                        "callback_data": "toggle:notify_host_start",
                    },
                    {
                        "text": toggle_label("Финал сессии", self.settings.notify_session_end),
                        "callback_data": "toggle:notify_session_end",
                    },
                ],
                [
                    {
                        "text": toggle_label("Гость вошел", self.settings.notify_guest_join),
                        "callback_data": "toggle:notify_guest_join",
                    },
                    {
                        "text": toggle_label("Гость вышел", self.settings.notify_guest_leave),
                        "callback_data": "toggle:notify_guest_leave",
                    },
                ],
                [
                    {
                        "text": toggle_label("Автосейвы", self.settings.notify_rolling_save),
                        "callback_data": "toggle:notify_rolling_save",
                    },
                    {
                        "text": toggle_label("Без звука", self.settings.silent_notifications),
                        "callback_data": "toggle:silent_notifications",
                    },
                ],
                [
                    {"text": "« Назад", "callback_data": "menu:main"},
                ],
            ]
            return {"inline_keyboard": keyboard}

        if subpage == "dashboard":
            keyboard = [
                [
                    {
                        "text": toggle_label("Живой дашборд", self.settings.live_dashboard_enabled),
                        "callback_data": "toggle:live_dashboard_enabled",
                    },
                ],
                [
                    {
                        "text": toggle_label("Пинг в закрепе", self.settings.show_ping_in_dashboard),
                        "callback_data": "toggle:show_ping_in_dashboard",
                    },
                ],
                [
                    {
                        "text": toggle_label("Время обновления", self.settings.show_updated_time),
                        "callback_data": "toggle:show_updated_time",
                    },
                    {
                        "text": f"Пояс: {curr_tz_label}",
                        "callback_data": "cycle:dashboard_tz_offset",
                    },
                ],
                [
                    {"text": "Пересоздать закреп", "callback_data": "action:repin"},
                    {"text": "Изменить заголовок", "callback_data": "action:prompt_title"},
                ],
                [
                    {"text": "« Назад", "callback_data": "menu:main"},
                ],
            ]
            return {"inline_keyboard": keyboard}

        if subpage == "chronicle":
            keyboard = [
                [
                    {
                        "text": toggle_label("ИИ-хроника", self.settings.ai_chronicle_enabled),
                        "callback_data": "toggle:ai_chronicle_enabled",
                    },
                ],
                [
                    {
                        "text": f"Частота: {curr_sched}",
                        "callback_data": "cycle:ai_schedule_mode",
                    },
                    {
                        "text": f"Стиль: {curr_style}",
                        "callback_data": "cycle:ai_persona_style",
                    },
                ],
                [
                    {"text": "Задать ключ OpenRouter", "callback_data": "action:prompt_openrouter_key"},
                ],
                [
                    {"text": "« Назад", "callback_data": "menu:main"},
                ],
            ]
            return {"inline_keyboard": keyboard}

        # Main Settings Menu (subpage == "main")
        admin_count = len(self.settings.admin_user_ids)
        keyboard = [
            [
                {"text": "Уведомления", "callback_data": "menu:notifications"},
                {"text": "Закрепленный дашборд", "callback_data": "menu:dashboard"},
            ],
            [
                {"text": "ИИ-Хроника", "callback_data": "menu:chronicle"},
                {"text": f"Администраторы ({admin_count})", "callback_data": "action:admin_list"},
            ],
            [
                {"text": "Миры в сейфе / Архив", "callback_data": "menu:worlds"},
                {"text": "Создать Код Дружбы", "callback_data": "action:prompt_invite"},
            ],
            [
                {"text": "Закрыть меню", "callback_data": "menu:close"},
            ],
        ]
        return {"inline_keyboard": keyboard}

    def format_worlds_menu_html(self) -> str:
        """
        Formats the interactive safe world manager and archive overview text.
        Strictly zero emojis.
        """
        lines = ["<b>Управление мирами в Сейфе</b>\n"]

        # Server state
        state = "idle"
        host_name = ""
        if self.session_manager:
            status = self.session_manager.get_public_status()
            state = status.get("state", "idle")
            host_name = status.get("host_name") or "Хост"

        if state == "idle":
            lines.append("Статус сервера: <b>[Свободен]</b>")
        else:
            lines.append(f"Статус сервера: <b>[Занят: {html.escape(host_name)}]</b>")

        # Worlds info
        worlds_info: Dict[str, Any] = {}
        if self.storage_manager:
            worlds_info = self.storage_manager.list_saved_worlds()

        active = worlds_info.get("active_world")
        lines.append("\n<b>Активный мир на сервере:</b>")
        if active and active.get("exists", True):
            w_name = html.escape(str(active.get("world_name") or "cloud_world"))
            v = active.get("version_counter", 1)
            sz = active.get("size_bytes", 0) / (1024 * 1024)
            u = html.escape(str(active.get("uploaded_by") or "Неизвестно"))
            lines.append(f"• <b>{w_name} [Cloud]</b> — v{v} ({sz:.1f} МБ, сохранил: {u})")
        else:
            lines.append("• <b>Не выбран (на сервере пусто)</b> — можно выбрать мир из архива ниже или загрузить новый из лаунчера.")

        archived = worlds_info.get("archived_worlds", [])
        lines.append(f"\n<b>Архив сохранённых миров на сервере ({len(archived)}):</b>")
        if archived:
            for idx, w in enumerate(archived[:10], 1):
                w_name = html.escape(str(w.get("world_name") or "cloud_world"))
                v = w.get("version_counter", 1)
                sz = w.get("size_bytes", 0) / (1024 * 1024)
                dt_str = ""
                archived_at = w.get("archived_at")
                if archived_at:
                    try:
                        dt = datetime.fromisoformat(archived_at)
                        dt_str = f", {self.format_time_with_tz(dt, fmt='%Y-%m-%d %H:%M')}"
                    except Exception:
                        pass
                lines.append(f"{idx}. <b>{w_name}</b> (v{v}, {sz:.1f} МБ{dt_str})")
        else:
            lines.append("<i>Архив сохранённых миров пока пуст.</i>")

        backups = worlds_info.get("backups", [])
        lines.append(f"\n<i>Авто-бэкапов ротации: {len(backups)} шт.</i>")

        return "\n".join(lines)

    def build_worlds_menu_keyboard(self) -> Dict[str, Any]:
        """
        Constructs inline keyboard for world management:
        - Archiving current world to make safe empty
        - Activating any archived world
        - Refresh and back to settings navigation
        """
        keyboard: List[List[Dict[str, str]]] = []

        worlds_info: Dict[str, Any] = {}
        if self.storage_manager:
            worlds_info = self.storage_manager.list_saved_worlds()

        active = worlds_info.get("active_world")
        if active and active.get("exists", True):
            keyboard.append([
                {"text": "Убрать текущий мир в архив (очистить слот)", "callback_data": "world:archive"}
            ])

        archived = worlds_info.get("archived_worlds", [])
        for idx, w in enumerate(archived[:5]):
            w_name = w.get("world_name") or "cloud_world"
            v = w.get("version_counter", 1)
            btn_text = f"Выбрать на сервер: {w_name} (v{v})"
            keyboard.append([
                {"text": btn_text, "callback_data": f"world:activate:{idx}"}
            ])

        keyboard.append([
            {"text": "Обновить список", "callback_data": "menu:worlds"},
            {"text": "К настройкам", "callback_data": "menu:open"},
        ])

        return {"inline_keyboard": keyboard}

    def format_admin_list_html(self) -> str:
        """Formats the list of authorized administrators in HTML."""
        lines = ["<b>Список администраторов бота:</b>\n"]
        if not self.settings.admin_user_ids:
            lines.append("<i>Список администраторов пуст (доступ открыт для настройки).</i>")
        else:
            for idx, a_id in enumerate(self.settings.admin_user_ids, 1):
                tag = self.settings.admin_usernames.get(str(a_id), "")
                tag_str = f" ({html.escape(tag)})" if tag else ""
                role = " [Владелец]" if idx == 1 else " [Администратор]"
                lines.append(f"{idx}. <code>{a_id}</code>{tag_str}{role}")

        lines.append(
            "\n<b>Как назначить нового администратора:</b>\n"
            "• Ответом на сообщение друга в группе: <code>/admin add</code>\n"
            "• По никнейму (если друг писал в чат): <code>/admin add @username</code>\n"
            "• По личному номеру: <code>/admin add 123456789</code>\n\n"
            "<i>Узнать свой Telegram ID можно командой <code>/myid</code>.</i>"
        )
        return "\n".join(lines)

    @staticmethod
    def _format_readable_ttl(ttl_seconds: int) -> str:
        """Returns human-friendly string for duration in seconds."""
        if ttl_seconds < 3600:
            return f"{ttl_seconds // 60} мин."
        elif ttl_seconds < 86400:
            return f"{ttl_seconds // 3600} ч."
        elif ttl_seconds == 86400:
            return "24 часа (1 сутки)"
        elif ttl_seconds == 259200:
            return "3 дня"
        elif ttl_seconds == 604800:
            return "7 дней (1 неделя)"
        elif ttl_seconds == 2592000:
            return "30 дней (1 месяц)"
        else:
            return f"{ttl_seconds // 86400} дн."

    @staticmethod
    def _format_readable_uses(max_uses: int) -> str:
        """Returns human-friendly string for activation limits."""
        if max_uses == 1:
            return "1 игрок (одноразовый)"
        elif max_uses >= 100:
            return "Без ограничений"
        elif max_uses in (2, 3, 4):
            return f"{max_uses} игрока"
        else:
            return f"{max_uses} игроков"

    def _get_wizard_state(self, user_id: int) -> Dict[str, Any]:
        """Returns or creates transient interactive wizard state for a user."""
        if user_id not in self._wizard_state:
            self._wizard_state[user_id] = {
                "ttl_seconds": 86400,
                "max_uses": 1,
                "passphrase": str(secrets.randbelow(900000) + 100000),
                "is_custom": False,
                "awaiting_passphrase": False,
                "message_id": None,
            }
        return self._wizard_state[user_id]

    @staticmethod
    def build_invite_wizard_keyboard(
        selected_ttl: int = 86400,
        selected_uses: int = 1,
        is_custom: bool = False,
    ) -> Dict[str, Any]:
        """Builds interactive inline keyboard for selecting invite expiration, uses, and passphrase."""
        ttl_row1 = [
            (900, "15 мин"),
            (1800, "30 мин"),
            (7200, "2 ч"),
            (21600, "6 ч"),
        ]
        ttl_row2 = [
            (86400, "24 ч"),
            (259200, "3 дня"),
            (604800, "7 дней"),
            (2592000, "30 дней"),
        ]
        uses_row = [
            (1, "1 игрок"),
            (2, "2 игрока"),
            (5, "5 игроков"),
            (100, "Без лимита"),
        ]

        def _ttl_btn(sec: int, label: str) -> Dict[str, str]:
            text = f"[• {label}]" if sec == selected_ttl else label
            return {"text": text, "callback_data": f"wiz:s:{sec}:{selected_uses}"}

        def _uses_btn(count: int, label: str) -> Dict[str, str]:
            text = f"[• {label}]" if count == selected_uses else label
            return {"text": text, "callback_data": f"wiz:s:{selected_ttl}:{count}"}

        custom_btn_text = "[• Свой пароль]" if is_custom else "Ввести свой пароль"
        rand_btn_text = "Случайный PIN" if is_custom else "[• Случайный PIN]"

        keyboard = [
            [_ttl_btn(sec, lbl) for sec, lbl in ttl_row1],
            [_ttl_btn(sec, lbl) for sec, lbl in ttl_row2],
            [_uses_btn(cnt, lbl) for cnt, lbl in uses_row],
            [
                {"text": custom_btn_text, "callback_data": "wiz:prompt_pass"},
                {"text": rand_btn_text, "callback_data": "wiz:rand_pass"},
            ],
            [
                {
                    "text": "Создать Код Дружбы",
                    "callback_data": f"wiz:g:{selected_ttl}:{selected_uses}",
                }
            ],
            [
                {
                    "text": "Назад в настройки",
                    "callback_data": "menu:main",
                }
            ],
        ]
        return {"inline_keyboard": keyboard}

    @classmethod
    def format_invite_wizard_html(
        cls,
        selected_ttl: int = 86400,
        selected_uses: int = 1,
        passphrase: str = "",
        is_custom: bool = False,
    ) -> str:
        """Formats interactive invite constructor message in HTML."""
        ttl_str = cls._format_readable_ttl(selected_ttl)
        uses_str = cls._format_readable_uses(selected_uses)
        pass_mode = "задано вами" if is_custom else "случайный PIN"

        return (
            "<b>Конструктор Кода Дружбы (Invite Wizard)</b>\n\n"
            "Текущие параметры кода:\n"
            f"• <b>Срок действия:</b> {html.escape(ttl_str)}\n"
            f"• <b>Лимит игроков:</b> {html.escape(uses_str)}\n"
            f"• <b>Пароль:</b> <code>{html.escape(passphrase)}</code> ({pass_mode})\n\n"
            "<i>Выберите параметры кнопками ниже или задайте свой пароль, затем нажмите создание:</i>"
        )

    @classmethod
    def format_invite_success_html(
        cls,
        invite_info: Dict[str, Any],
        tz_offset: int = 3,
    ) -> str:
        """Formats generated invite code with single clean copyable pre block."""
        code = invite_info.get("invite_code", "")
        passphrase = invite_info.get("passphrase", "")
        ttl_sec = invite_info.get("ttl_seconds", 86400)
        uses = invite_info.get("max_uses", 1)
        expires_at = invite_info.get("expires_at", 0)

        ttl_str = cls._format_readable_ttl(ttl_sec)
        uses_str = cls._format_readable_uses(uses)
        offset = max(-12, min(14, int(tz_offset)))
        tz = timezone(timedelta(hours=offset))
        exp_dt = datetime.fromtimestamp(expires_at, tz=tz)
        tz_lbl = format_tz_label(offset)
        exp_str = f"{exp_dt.strftime('%Y-%m-%d %H:%M')} {tz_lbl}"

        return (
            "<b>Код Дружбы успешно создан!</b>\n\n"
            "<blockquote>\n"
            "<b>Параметры доступа к серверу:</b>\n"
            f"• <b>Срок действия:</b> {html.escape(ttl_str)} (до {exp_str})\n"
            f"• <b>Лимит игроков:</b> {html.escape(uses_str)}\n"
            "• <b>Шифрование:</b> PBKDF2-HMAC-SHA256 (FCT1)\n"
            "</blockquote>\n\n"
            "<b>Код подключения (нажмите на блок для копирования ключа):</b>\n"
            f"<pre><code>{html.escape(code)}</code></pre>\n\n"
            "<b>Секретное слово (пароль подключения):</b>\n"
            f"<code>{html.escape(passphrase)}</code>\n\n"
            "<blockquote>\n"
            "<b>Инструкция для друга:</b>\n"
            "1. Запустить лаунчер игры (клиент Vaultorio).\n"
            "2. Вставить <b>Код Дружбы</b> и <b>Пароль</b> в форму подключения.\n"
            "3. Нажать <b>«Подключиться»</b> для автоматической настройки и синхронизации сейва.\n"
            "</blockquote>"
        )

    @staticmethod
    def build_invite_success_keyboard(invite_code: str = "") -> Dict[str, Any]:
        """Builds action keyboard after invite code is generated."""
        rows: List[List[Dict[str, Any]]] = []
        if invite_code:
            rows.append([{"text": "Скопировать Код Дружбы", "copy_text": {"text": invite_code}}])
        rows.append([
            {"text": "Создать еще один код", "callback_data": "action:prompt_invite"},
            {"text": "Назад в настройки", "callback_data": "menu:main"},
        ])
        return {"inline_keyboard": rows}

    async def get_public_ip(self) -> str:
        """Resolves server public IPv4 address with caching."""
        if hasattr(self, "_cached_public_ip") and self._cached_public_ip:
            return self._cached_public_ip

        # 1. Environment variable override
        env_ip = os.getenv("FACTORIO_PUBLIC_IP", "").strip()
        if env_ip:
            self._cached_public_ip = env_ip
            return env_ip

        # 2. Config host if explicitly set to public address
        if self.config.http_host not in ("0.0.0.0", "127.0.0.1", "localhost", ""):
            self._cached_public_ip = self.config.http_host
            return self.config.http_host

        # 3. Asynchronous HTTP detection via echo services
        endpoints = ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"]
        for ep in endpoints:
            try:
                resp = await self._client.get(ep, timeout=3.0)
                if resp.status_code == 200:
                    cand = resp.text.strip()
                    parts = cand.split(".")
                    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                        self._cached_public_ip = cand
                        logger.info("Detected public server IP: %s", cand)
                        return cand
            except Exception:
                continue

        return "127.0.0.1"

    async def generate_packaged_invite(
        self,
        ttl_seconds: int = 86400,
        max_uses: int = 1,
        note: str = "Telegram Bot Wizard",
        passphrase: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generates an authenticated invite ticket and packages it into an encrypted FCT1 invite code.
        """
        if not self.auth_manager:
            raise RuntimeError("AuthManager is not configured on this server.")

        public_ip = await self.get_public_ip()
        hub_url = f"http://{public_ip}:{self.config.http_port}"
        stun_host = public_ip
        stun_port = self.config.udp_stun_port

        invite_data = self.auth_manager.create_invite(
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
            note=note,
            created_by="telegram_bot",
        )
        ticket = invite_data["ticket"]
        expires_at = int(invite_data["expires_at"])

        if not passphrase:
            passphrase = f"{secrets.randbelow(900000) + 100000}"

        from src.common.invite_codec import encode_invite_code

        invite_code = encode_invite_code(
            hub_http_url=hub_url,
            hub_stun_host=stun_host,
            hub_stun_port=stun_port,
            ticket=ticket,
            expires_at=expires_at,
            passphrase=passphrase,
        )

        return {
            "invite_code": invite_code,
            "passphrase": passphrase,
            "expires_at": expires_at,
            "ttl_seconds": ttl_seconds,
            "max_uses": max_uses,
            "ticket": ticket,
            "hub_url": hub_url,
            "stun_host": stun_host,
            "stun_port": stun_port,
        }

    def _is_user_authorized(self, user_id: int) -> bool:
        """Verifies if user has admin permission (if admin list is populated)."""
        if not self.settings.admin_user_ids:
            return True
        return user_id in self.settings.admin_user_ids

    async def handle_callback_query(self, callback_query: Dict[str, Any]) -> bool:
        """
        Dispatches incoming callback queries from inline buttons.

        Args:
            callback_query: Telegram CallbackQuery object dictionary.

        Returns:
            bool: True if handled successfully.
        """
        cb_id = callback_query.get("id", "")
        cb_data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        msg_id = message.get("message_id")
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        user = callback_query.get("from", {})
        user_id = user.get("id", 0)
        username = user.get("username", "")

        if user_id and username:
            clean_tag = username.lower().lstrip("@")
            if self.settings.known_users.get(clean_tag) != user_id:
                self.settings.known_users[clean_tag] = user_id
                self.settings.admin_usernames[str(user_id)] = f"@{username}"
                self.save_settings()

        logger.debug("Received callback: data='%s', user=%d, msg=%s", cb_data, user_id, msg_id)

        if cb_data == "dash:refresh":
            await self.update_dashboard(force=True)
            await self.answer_callback_query(callback_query_id=cb_id, text="Статус обновлен!")
            return True

        if not self._is_user_authorized(user_id):
            await self.answer_callback_query(
                callback_query_id=cb_id,
                text="У вас нет прав администратора для изменения настроек или создания кодов.",
                show_alert=True,
            )
            return True

        if cb_data in ("menu:open", "menu:main", "wiz:back"):
            menu_text = self.format_settings_menu_html(subpage="main")
            menu_kb = self.build_settings_keyboard(subpage="main")
            if cb_data in ("menu:main", "wiz:back") and msg_id and chat_id and msg_id != self.settings.pinned_message_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=menu_text,
                    reply_markup=menu_kb,
                )
            else:
                await self.send_message(text=menu_text, chat_id=chat_id, reply_markup=menu_kb)
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data in ("menu:notifications", "menu:dashboard", "menu:chronicle"):
            subpage = cb_data.split(":", 1)[1]
            sub_text = self.format_settings_menu_html(subpage=subpage)
            sub_kb = self.build_settings_keyboard(subpage=subpage)
            if msg_id and chat_id and msg_id != self.settings.pinned_message_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=sub_text,
                    reply_markup=sub_kb,
                )
            else:
                await self.send_message(text=sub_text, chat_id=chat_id, reply_markup=sub_kb)
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data == "menu:close":
            if msg_id and chat_id:
                await self.delete_message(message_id=msg_id, chat_id=chat_id)
            await self.answer_callback_query(callback_query_id=cb_id, text="Меню закрыто.")
            return True

        if cb_data == "menu:worlds":
            w_text = self.format_worlds_menu_html()
            w_kb = self.build_worlds_menu_keyboard()
            # Never overwrite pinned dashboard
            if msg_id and chat_id and msg_id != self.settings.pinned_message_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=w_text,
                    reply_markup=w_kb,
                )
            else:
                await self.send_message(text=w_text, chat_id=chat_id, reply_markup=w_kb)
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data == "world:archive":
            if not self.storage_manager:
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text="Хранилище не подключено.",
                    show_alert=True,
                )
                return True

            state = "idle"
            if self.session_manager:
                state = self.session_manager.get_public_status().get("state", "idle")

            if state != "idle":
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text="Нельзя убрать мир в архив, пока идет игра! Дождитесь завершения сессии.",
                    show_alert=True,
                )
                return True

            try:
                res = self.storage_manager.archive_and_remove_current_world(deleted_by="Telegram Admin")
                await self.notify_world_archived(
                    world_name=res.get("world_name") or "cloud_world",
                    archived_by="Telegram Admin",
                    archive_name=res.get("archive_name"),
                )
                if msg_id and chat_id and msg_id != self.settings.pinned_message_id:
                    await self.edit_message_text(
                        message_id=msg_id,
                        chat_id=chat_id,
                        text=self.format_worlds_menu_html(),
                        reply_markup=self.build_worlds_menu_keyboard(),
                    )
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text="Мир успешно убран в архив сервера.",
                )
            except Exception as exc:
                logger.error("Failed to archive world via telegram: %s", exc)
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text=f"Ошибка архивации: {exc}",
                    show_alert=True,
                )
            return True

        if cb_data.startswith("world:activate:"):
            if not self.storage_manager:
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text="Хранилище не подключено.",
                    show_alert=True,
                )
                return True

            state = "idle"
            if self.session_manager:
                state = self.session_manager.get_public_status().get("state", "idle")

            if state != "idle":
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text="Нельзя переключить мир, пока идет игра! Дождитесь завершения сессии.",
                    show_alert=True,
                )
                return True

            try:
                idx = int(cb_data.split(":", 2)[2])
                worlds_info = self.storage_manager.list_saved_worlds()
                archived = worlds_info.get("archived_worlds", [])
                if 0 <= idx < len(archived):
                    target_archive = archived[idx]["archive_name"]
                    res = self.storage_manager.activate_archived_world(
                        archive_name=target_archive,
                        activated_by="Telegram Admin",
                    )
                    w_name = res.get("world_name") or "cloud_world"
                    await self.send_message(
                        chat_id=chat_id,
                        text=f"<b>[СЕЙФ]</b> Мир <b>{html.escape(w_name)} [Cloud]</b> выбран активным миром на сервере!",
                    )
                    await self.update_dashboard(force=True)
                    if msg_id and chat_id and msg_id != self.settings.pinned_message_id:
                        await self.edit_message_text(
                            message_id=msg_id,
                            chat_id=chat_id,
                            text=self.format_worlds_menu_html(),
                            reply_markup=self.build_worlds_menu_keyboard(),
                        )
                    await self.answer_callback_query(
                        callback_query_id=cb_id,
                        text="Мир успешно активирован на сервере!",
                    )
                else:
                    await self.answer_callback_query(
                        callback_query_id=cb_id,
                        text="Выбранный архив не найден.",
                        show_alert=True,
                    )
            except Exception as exc:
                logger.error("Failed to activate world via telegram: %s", exc)
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text=f"Ошибка активации: {exc}",
                    show_alert=True,
                )
            return True

        if cb_data.startswith("toggle:"):
            field_name = cb_data.split(":", 1)[1]
            if hasattr(self.settings, field_name):
                current_val = getattr(self.settings, field_name)
                if isinstance(current_val, bool):
                    new_val = not current_val
                    setattr(self.settings, field_name, new_val)
                    self.save_settings()

                    # Refresh menu inline keyboard keeping user in the respective submenu
                    subpage = self.get_setting_subpage(field_name)
                    if msg_id and chat_id and msg_id != self.settings.pinned_message_id:
                        await self.edit_message_text(
                            message_id=msg_id,
                            chat_id=chat_id,
                            text=self.format_settings_menu_html(subpage=subpage),
                            reply_markup=self.build_settings_keyboard(subpage=subpage),
                        )

                    # Trigger dashboard update if live dashboard was re-enabled or ping/updated_time setting toggled
                    if (field_name == "live_dashboard_enabled" and new_val) or (field_name in ("show_ping_in_dashboard", "show_updated_time")):
                        await self.update_dashboard(force=True)

                    status_str = "ВКЛ" if new_val else "ВЫКЛ"
                    await self.answer_callback_query(
                        callback_query_id=cb_id,
                        text=f"{field_name}: {status_str}",
                    )
                    return True

        if cb_data.startswith("cycle:"):
            field_name = cb_data.split(":", 1)[1]
            if field_name == "ai_schedule_mode":
                modes = [m[0] for m in AI_SCHEDULE_MODES]
                curr_idx = modes.index(self.settings.ai_schedule_mode) if self.settings.ai_schedule_mode in modes else 0
                next_mode = modes[(curr_idx + 1) % len(modes)]
                self.settings.ai_schedule_mode = next_mode
                self.save_settings()
            elif field_name == "ai_persona_style":
                styles = [s[0] for s in AI_PERSONA_STYLES]
                curr_idx = styles.index(self.settings.ai_persona_style) if self.settings.ai_persona_style in styles else 0
                next_style = styles[(curr_idx + 1) % len(styles)]
                self.settings.ai_persona_style = next_style
                self.save_settings()
            elif field_name == "dashboard_tz_offset":
                offsets = [item[0] for item in DASHBOARD_TIMEZONES]
                curr_idx = offsets.index(self.settings.dashboard_tz_offset) if self.settings.dashboard_tz_offset in offsets else 0
                next_offset = offsets[(curr_idx + 1) % len(offsets)]
                self.settings.dashboard_tz_offset = next_offset
                self.save_settings()
                await self.update_dashboard(force=True)

            subpage = self.get_setting_subpage(field_name)
            if msg_id and chat_id and msg_id != self.settings.pinned_message_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=self.format_settings_menu_html(subpage=subpage),
                    reply_markup=self.build_settings_keyboard(subpage=subpage),
                )
            await self.answer_callback_query(callback_query_id=cb_id, text="Параметр обновлен")
            return True

        if cb_data == "action:repin":
            self.settings.pinned_message_id = None
            self.save_settings()
            await self.update_dashboard(force=True)
            await self.answer_callback_query(callback_query_id=cb_id, text="Закрепленный дашборд пересоздан!")
            return True

        if cb_data == "action:prompt_invite":
            if not self.auth_manager:
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text="Управление билетами не активно (auth_manager не подключен).",
                    show_alert=True,
                )
                return True

            state = self._get_wizard_state(user_id)
            if msg_id:
                state["message_id"] = msg_id

            wizard_text = self.format_invite_wizard_html(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                passphrase=state["passphrase"],
                is_custom=state["is_custom"],
            )
            wizard_kb = self.build_invite_wizard_keyboard(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                is_custom=state["is_custom"],
            )
            if msg_id and chat_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=wizard_text,
                    reply_markup=wizard_kb,
                )
            else:
                sent = await self.send_message(
                    chat_id=chat_id,
                    text=wizard_text,
                    reply_markup=wizard_kb,
                )
                if sent and "message_id" in sent:
                    state["message_id"] = sent["message_id"]
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data.startswith("wiz:s:"):
            state = self._get_wizard_state(user_id)
            parts = cb_data.split(":")
            if len(parts) == 4:
                try:
                    state["ttl_seconds"] = int(parts[2])
                    state["max_uses"] = int(parts[3])
                except ValueError:
                    pass
            wizard_text = self.format_invite_wizard_html(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                passphrase=state["passphrase"],
                is_custom=state["is_custom"],
            )
            wizard_kb = self.build_invite_wizard_keyboard(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                is_custom=state["is_custom"],
            )
            if msg_id and chat_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=wizard_text,
                    reply_markup=wizard_kb,
                )
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data == "wiz:prompt_pass":
            state = self._get_wizard_state(user_id)
            state["awaiting_passphrase"] = True
            if msg_id:
                state["message_id"] = msg_id

            prompt_text = (
                "<b>Ввод секретного слова (пароля подключения)</b>\n\n"
                "Отправьте желаемый пароль сообщением в этот чат (например: <code>наувис-2026</code> или <code>секрет</code>).\n\n"
                "<i>Сообщение с паролем будет автоматически удалено из чата сразу после обработки.</i>"
            )
            cancel_kb = {
                "inline_keyboard": [
                    [{"text": "Отмена / Оставить текущий", "callback_data": "wiz:cancel_pass"}]
                ]
            }
            if msg_id and chat_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=prompt_text,
                    reply_markup=cancel_kb,
                )
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data == "wiz:cancel_pass":
            state = self._get_wizard_state(user_id)
            state["awaiting_passphrase"] = False
            wizard_text = self.format_invite_wizard_html(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                passphrase=state["passphrase"],
                is_custom=state["is_custom"],
            )
            wizard_kb = self.build_invite_wizard_keyboard(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                is_custom=state["is_custom"],
            )
            if msg_id and chat_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=wizard_text,
                    reply_markup=wizard_kb,
                )
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data == "wiz:rand_pass":
            state = self._get_wizard_state(user_id)
            state["passphrase"] = str(secrets.randbelow(900000) + 100000)
            state["is_custom"] = False
            state["awaiting_passphrase"] = False
            wizard_text = self.format_invite_wizard_html(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                passphrase=state["passphrase"],
                is_custom=False,
            )
            wizard_kb = self.build_invite_wizard_keyboard(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                is_custom=False,
            )
            if msg_id and chat_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=wizard_text,
                    reply_markup=wizard_kb,
                )
            await self.answer_callback_query(
                callback_query_id=cb_id,
                text=f"Сгенерирован PIN: {state['passphrase']}",
            )
            return True

        if cb_data.startswith("wiz:g:"):
            state = self._get_wizard_state(user_id)
            parts = cb_data.split(":")
            ttl = state["ttl_seconds"]
            uses = state["max_uses"]
            if len(parts) == 4:
                try:
                    ttl = int(parts[2])
                    uses = int(parts[3])
                except ValueError:
                    pass

            if not self.auth_manager:
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text="Управление билетами не активно.",
                    show_alert=True,
                )
                return True

            try:
                invite_info = await self.generate_packaged_invite(
                    ttl_seconds=ttl,
                    max_uses=uses,
                    passphrase=state.get("passphrase"),
                )
                res_text = self.format_invite_success_html(
                    invite_info,
                    tz_offset=self.settings.dashboard_tz_offset,
                )
                res_kb = self.build_invite_success_keyboard(invite_code=invite_info["invite_code"])
                if msg_id and chat_id:
                    await self.edit_message_text(
                        message_id=msg_id,
                        chat_id=chat_id,
                        text=res_text,
                        reply_markup=res_kb,
                    )
                else:
                    await self.send_message(chat_id=chat_id, text=res_text, reply_markup=res_kb)
                await self.answer_callback_query(callback_query_id=cb_id, text="Код Дружбы успешно создан!")
            except Exception as exc:
                logger.error("Failed to create packaged invite: %s", exc)
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text=f"Ошибка генерации: {exc}",
                    show_alert=True,
                )
            return True

        if cb_data == "action:prompt_openrouter_key":
            await self.send_message(
                chat_id=chat_id,
                text=(
                    "Для установки ключа OpenRouter отправьте сообщение командой:\n"
                    "<code>/set_key ВАШ_КЛЮЧ</code>\n\n"
                    "Сообщение с ключом будет удалено сразу после сохранения в целях безопасности."
                ),
            )
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data == "action:prompt_title":
            await self.send_message(
                chat_id=chat_id,
                text=(
                    "Для изменения заголовка дашборда отправьте команду:\n"
                    "<code>/set_title НОВЫЙ_ЗАГОЛОВОК</code>"
                ),
            )
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data == "action:admin_list":
            admin_text = self.format_admin_list_html()
            back_kb = {
                "inline_keyboard": [
                    [{"text": "Назад в настройки", "callback_data": "menu:main"}]
                ]
            }
            if msg_id and chat_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=admin_text,
                    reply_markup=back_kb,
                )
            else:
                await self.send_message(chat_id=chat_id, text=admin_text, reply_markup=back_kb)
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        await self.answer_callback_query(callback_query_id=cb_id)
        return False

    # --------------------------------------------------------------------------
    # Bot Command Handling
    # --------------------------------------------------------------------------

    async def handle_message(self, message: Dict[str, Any]) -> bool:
        """
        Parses incoming text messages and routes commands.

        Args:
            message: Telegram Message dictionary.

        Returns:
            bool: True if command was handled.
        """
        text = message.get("text", "").strip()
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        user = message.get("from", {})
        user_id = user.get("id", 0)
        username = user.get("username", "")
        msg_id = message.get("message_id")

        if not text or not chat_id:
            return False

        if user_id and username:
            clean_tag = username.lower().lstrip("@")
            if self.settings.known_users.get(clean_tag) != user_id:
                self.settings.known_users[clean_tag] = user_id
                self.settings.admin_usernames[str(user_id)] = f"@{username}"
                self.save_settings()

        reply_to = message.get("reply_to_message", {})
        if reply_to:
            r_user = reply_to.get("from", {})
            r_id = r_user.get("id", 0)
            r_uname = r_user.get("username", "")
            if r_id and r_uname:
                clean_r_tag = r_uname.lower().lstrip("@")
                if self.settings.known_users.get(clean_r_tag) != r_id:
                    self.settings.known_users[clean_r_tag] = r_id
                    self.settings.admin_usernames[str(r_id)] = f"@{r_uname}"
                    self.save_settings()

        # Check if user is entering a custom passphrase in the invite wizard
        state = self._wizard_state.get(user_id)
        if state and state.get("awaiting_passphrase"):
            clean_pass = text.strip()
            if clean_pass.startswith("/"):
                state["awaiting_passphrase"] = False
            else:
                state["passphrase"] = clean_pass
                state["is_custom"] = True
                state["awaiting_passphrase"] = False

                if msg_id and chat_id:
                    await self.delete_message(message_id=msg_id, chat_id=chat_id)

                wizard_text = self.format_invite_wizard_html(
                    selected_ttl=state["ttl_seconds"],
                    selected_uses=state["max_uses"],
                    passphrase=state["passphrase"],
                    is_custom=True,
                )
                wizard_kb = self.build_invite_wizard_keyboard(
                    selected_ttl=state["ttl_seconds"],
                    selected_uses=state["max_uses"],
                    is_custom=True,
                )
                wiz_msg_id = state.get("message_id")
                if wiz_msg_id and chat_id:
                    await self.edit_message_text(
                        message_id=wiz_msg_id,
                        chat_id=chat_id,
                        text=wizard_text,
                        reply_markup=wizard_kb,
                    )
                else:
                    sent = await self.send_message(
                        chat_id=chat_id,
                        text=wizard_text,
                        reply_markup=wizard_kb,
                    )
                    if sent and "message_id" in sent:
                        state["message_id"] = sent["message_id"]
                return True

        if not text.startswith("/"):
            return False

        parts = text.split(maxsplit=1)
        raw_cmd = parts[0].lower()
        cmd = raw_cmd.split("@")[0]  # Strip bot mention (e.g. /start@MyBot -> /start)
        args = parts[1].strip() if len(parts) > 1 else ""

        logger.info("Routing bot command: '%s', user=%d, chat=%s", cmd, user_id, chat_id)

        if cmd in ("/start", "/setup"):
            # Auto-claim owner if no admin is set yet
            if not self.settings.admin_user_ids and user_id:
                self.settings.admin_user_ids.append(user_id)
                self.save_settings()
                logger.info("Auto-registered user %d as primary bot administrator.", user_id)

            if cmd == "/setup" and self.settings.admin_user_ids and not self._is_user_authorized(user_id):
                await self.send_message(
                    chat_id=chat_id,
                    text="У вас нет прав администратора для перепривязки чата бота.",
                )
                return True

            # Bind chat_id to current chat if not bound or explicitly requested via /setup
            if not self.settings.chat_id or cmd == "/setup":
                self.settings.chat_id = chat_id
                self.save_settings()
                logger.info("Bound bot to chat_id %s", chat_id)

            await self.send_message(
                chat_id=chat_id,
                text=(
                    "Factorio Bot готов к работе!\n"
                    "Чат успешно привязан для координации сессий и уведомлений.\n"
                    "Используйте /settings для настроек или /status для просмотра текущего состояния."
                ),
            )
            await self.update_dashboard(force=True)
            return True

        if cmd == "/settings":
            if not self._is_user_authorized(user_id):
                await self.send_message(
                    chat_id=chat_id,
                    text="У вас нет прав администратора. Меню настроек доступно только владельцу.",
                )
                return True
            menu_text = self.format_settings_menu_html()
            menu_kb = self.build_settings_keyboard()
            await self.send_message(chat_id=chat_id, text=menu_text, reply_markup=menu_kb)
            return True

        if cmd == "/status":
            status_text = self.format_dashboard_html()
            await self.send_message(chat_id=chat_id, text=status_text)
            return True

        if cmd == "/worlds":
            w_text = self.format_worlds_menu_html()
            w_kb = self.build_worlds_menu_keyboard()
            await self.send_message(chat_id=chat_id, text=w_text, reply_markup=w_kb)
            return True

        if cmd == "/ping":
            await self._handle_ping_command(chat_id)
            return True

        if cmd in ("/id", "/myid"):
            u_tag = f"@{username}" if username else "отсутствует"
            f_name = html.escape(user.get("first_name", "") or "Пользователь")
            is_adm = "Да" if self._is_user_authorized(user_id) else "Нет"
            await self.send_message(
                chat_id=chat_id,
                text=(
                    "<b>Информация об аккаунте:</b>\n\n"
                    f"• <b>Имя:</b> {f_name}\n"
                    f"• <b>Username:</b> {u_tag}\n"
                    f"• <b>Telegram ID:</b> <code>{user_id}</code> (нажмите для копирования)\n"
                    f"• <b>Права администратора:</b> {is_adm}"
                ),
            )
            return True

        if cmd == "/admin":
            if not self._is_user_authorized(user_id):
                await self.send_message(
                    chat_id=chat_id,
                    text="У вас нет прав администратора для управления доступом.",
                )
                return True

            subcmd = ""
            subarg = ""
            if args:
                subparts = args.split(maxsplit=1)
                subcmd = subparts[0].lower()
                subarg = subparts[1].strip() if len(subparts) > 1 else ""

            if not subcmd or subcmd == "list":
                admin_text = self.format_admin_list_html()
                await self.send_message(chat_id=chat_id, text=admin_text)
                return True

            if subcmd == "add":
                target_id: Optional[int] = None
                target_tag: str = ""

                # 1. Check if replying to message
                reply_to = message.get("reply_to_message", {})
                reply_user = reply_to.get("from", {})
                if reply_user and reply_user.get("id"):
                    target_id = reply_user["id"]
                    if reply_user.get("username"):
                        target_tag = f"@{reply_user['username']}"

                # 2. Check arguments
                if not target_id and subarg:
                    clean_arg = subarg.strip()
                    if clean_arg.isdigit():
                        target_id = int(clean_arg)
                        target_tag = self.settings.admin_usernames.get(str(target_id), "")
                    else:
                        clean_uname = clean_arg.lstrip("@").lower()
                        target_id = self.settings.known_users.get(clean_uname)
                        target_tag = f"@{clean_arg.lstrip('@')}"

                if not target_id:
                    if subarg and (subarg.startswith("@") or not subarg.isdigit()):
                        clean_uname = subarg.lstrip("@")
                        await self.send_message(
                            chat_id=chat_id,
                            text=(
                                f"Пользователь @{clean_uname} пока не писал боту или в эту группу.\n\n"
                                "Telegram не позволяет ботам находить чужие ID только по никнейму.\n\n"
                                "<b>Как его добавить:</b>\n"
                                "1. Ответьте на любое его сообщение в группе командой <code>/admin add</code>\n"
                                "2. Или попросите его написать в группу или боту команду <code>/myid</code> и добавьте по номеру: <code>/admin add ID</code>"
                            ),
                        )
                    else:
                        await self.send_message(
                            chat_id=chat_id,
                            text=(
                                "Укажите пользователя для добавления:\n"
                                "• Ответом на сообщение: <code>/admin add</code>\n"
                                "• По никнейму: <code>/admin add @username</code>\n"
                                "• По номеру: <code>/admin add 123456789</code>"
                            ),
                        )
                    return True

                if target_id in self.settings.admin_user_ids:
                    tag_display = f" ({target_tag})" if target_tag else ""
                    await self.send_message(
                        chat_id=chat_id,
                        text=f"Пользователь <code>{target_id}</code>{tag_display} уже является администратором.",
                    )
                    return True

                self.settings.admin_user_ids.append(target_id)
                if target_tag:
                    self.settings.admin_usernames[str(target_id)] = target_tag
                self.save_settings()

                tag_display = f" ({target_tag})" if target_tag else ""
                await self.send_message(
                    chat_id=chat_id,
                    text=(
                        f"Пользователь <code>{target_id}</code>{tag_display} успешно добавлен в список администраторов.\n"
                        "Теперь ему доступны все команды управления сервером и генерации Кодов Дружбы."
                    ),
                )
                return True

            if subcmd in ("remove", "del", "delete", "rm"):
                target_id: Optional[int] = None
                target_tag: str = ""

                reply_to = message.get("reply_to_message", {})
                reply_user = reply_to.get("from", {})
                if reply_user and reply_user.get("id"):
                    target_id = reply_user["id"]
                    if reply_user.get("username"):
                        target_tag = f"@{reply_user['username']}"

                if not target_id and subarg:
                    clean_arg = subarg.strip()
                    if clean_arg.isdigit():
                        target_id = int(clean_arg)
                    else:
                        clean_uname = clean_arg.lstrip("@").lower()
                        target_id = self.settings.known_users.get(clean_uname)
                        target_tag = f"@{clean_arg.lstrip('@')}"

                if not target_id:
                    await self.send_message(
                        chat_id=chat_id,
                        text=(
                            "Укажите пользователя для удаления:\n"
                            "• Ответом на сообщение: <code>/admin remove</code>\n"
                            "• По никнейму: <code>/admin remove @username</code>\n"
                            "• По номеру: <code>/admin remove 123456789</code>"
                        ),
                    )
                    return True

                if target_id not in self.settings.admin_user_ids:
                    await self.send_message(
                        chat_id=chat_id,
                        text=f"Пользователь <code>{target_id}</code> не найден в списке администраторов.",
                    )
                    return True

                if self.settings.admin_user_ids and self.settings.admin_user_ids[0] == target_id:
                    await self.send_message(
                        chat_id=chat_id,
                        text="Нельзя удалить главного владельца сервера из администраторов.",
                    )
                    return True

                self.settings.admin_user_ids.remove(target_id)
                self.save_settings()

                tag_display = f" ({target_tag})" if target_tag else ""
                await self.send_message(
                    chat_id=chat_id,
                    text=f"Пользователь <code>{target_id}</code>{tag_display} удален из списка администраторов.",
                )
                return True

        if cmd == "/invite":
            if not self._is_user_authorized(user_id):
                await self.send_message(
                    chat_id=chat_id,
                    text="У вас нет прав администратора. Создавать Коды Дружбы может только владелец.",
                )
                return True
            if not self.auth_manager:
                await self.send_message(chat_id=chat_id, text="Менеджер авторизации не подключен.")
                return True

            state = self._get_wizard_state(user_id)
            if args:
                state["passphrase"] = args
                state["is_custom"] = True

            wizard_text = self.format_invite_wizard_html(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                passphrase=state["passphrase"],
                is_custom=state["is_custom"],
            )
            wizard_kb = self.build_invite_wizard_keyboard(
                selected_ttl=state["ttl_seconds"],
                selected_uses=state["max_uses"],
                is_custom=state["is_custom"],
            )
            sent = await self.send_message(chat_id=chat_id, text=wizard_text, reply_markup=wizard_kb)
            if sent and "message_id" in sent:
                state["message_id"] = sent["message_id"]
            return True

        if cmd == "/set_key":
            if not self._is_user_authorized(user_id):
                await self.send_message(chat_id=chat_id, text="У вас нет прав администратора.")
                return True

            if not args:
                await self.send_message(chat_id=chat_id, text="Укажите ключ: <code>/set_key ВАШ_КЛЮЧ</code>")
                return True

            # Save key
            self.settings.openrouter_api_key = args
            self.save_settings()

            # Delete user message with sensitive key for security
            if msg_id:
                await self.delete_message(message_id=msg_id, chat_id=chat_id)

            await self.send_message(chat_id=chat_id, text="Ключ OpenRouter успешно сохранен.")
            return True

        if cmd == "/set_title":
            if not self._is_user_authorized(user_id):
                await self.send_message(chat_id=chat_id, text="У вас нет прав администратора.")
                return True

            if not args:
                await self.send_message(
                    chat_id=chat_id,
                    text="Укажите заголовок: <code>/set_title НАЗВАНИЕ</code>",
                )
                return True

            self.settings.dashboard_title = args
            self.save_settings()
            await self.update_dashboard(force=True)
            await self.send_message(
                chat_id=chat_id,
                text=f"Заголовок дашборда обновлен на:\n<b>{html.escape(args)}</b>",
            )
            return True

        if cmd == "/set_tz":
            if not self._is_user_authorized(user_id):
                await self.send_message(chat_id=chat_id, text="У вас нет прав администратора.")
                return True

            offset_val: Optional[int] = None
            if args:
                try:
                    val = int(args.lstrip("+"))
                    if -12 <= val <= 14:
                        offset_val = val
                except ValueError:
                    pass

            if offset_val is None:
                await self.send_message(
                    chat_id=chat_id,
                    text=(
                        "Использование: <code>/set_tz +3</code>\n"
                        "Допустимо целое смещение от -12 до +14 часов от UTC."
                    ),
                )
                return True

            self.settings.dashboard_tz_offset = offset_val
            self.save_settings()
            await self.update_dashboard(force=True)
            lbl = format_tz_label(offset_val)
            await self.send_message(
                chat_id=chat_id,
                text=f"<b>[НАСТРОЙКИ]</b> Часовой пояс обновлен: <b>{lbl}</b>",
            )
            return True

        return False

    # --------------------------------------------------------------------------
    # Long Polling Update Loop
    # --------------------------------------------------------------------------

    async def register_commands(self) -> bool:
        """
        Registers default command list with Telegram via setMyCommands.
        Enables command autocomplete in chat input.
        """
        if not self.is_configured:
            return False

        commands = [
            {"command": "ping", "description": "Пинг игроков и режим сети (P2P / Relay)"},
            {"command": "status", "description": "Текущий статус игрового сервера"},
            {"command": "worlds", "description": "Миры в сейфе, архив и переключение слота"},
            {"command": "settings", "description": "Интерактивное меню настроек"},
            {"command": "set_tz", "description": "Установить часовой пояс (от -12 до +14)"},
            {"command": "invite", "description": "Создать Код Дружбы"},
            {"command": "myid", "description": "Узнать свой Telegram ID"},
            {"command": "admin", "description": "Управление администраторами"},
        ]
        url = f"{self.bot_url}/setMyCommands"
        try:
            resp = await self._client.post(url, json={"commands": commands})
            data = resp.json()
            return bool(data.get("ok"))
        except Exception as exc:
            logger.debug("Network error during setMyCommands: %s", exc)
            return False

    async def start_polling(self) -> None:
        """Starts background tasks for getUpdates long-polling and periodic dashboard refresh."""
        if self._is_polling:
            return

        if not self.is_configured:
            logger.info("Telegram Bot is not configured. Polling not started.")
            return

        self._is_polling = True
        self._polling_task = asyncio.create_task(self._polling_loop(), name="tg_bot_polling")
        self._dashboard_timer_task = asyncio.create_task(
            self._dashboard_timer_loop(),
            name="tg_bot_dashboard_timer",
        )
        asyncio.create_task(self.register_commands(), name="tg_bot_register_commands")
        logger.info("Telegram Bot long-polling and dashboard timer started.")

    async def start(self) -> None:
        """Convenience alias for start_polling."""
        await self.start_polling()

    async def stop_polling(self) -> None:
        """Cancels and awaits termination of polling and timer tasks."""
        self._is_polling = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

        if self._dashboard_timer_task:
            self._dashboard_timer_task.cancel()
            try:
                await self._dashboard_timer_task
            except asyncio.CancelledError:
                pass
            self._dashboard_timer_task = None

        logger.info("Telegram Bot long-polling stopped.")

    async def stop(self) -> None:
        """Convenience alias for stop_polling."""
        await self.stop_polling()

    async def _polling_loop(self) -> None:
        """Continuous long-polling loop retrieving updates via getUpdates."""
        url = f"{self.bot_url}/getUpdates"

        while self._is_polling:
            try:
                payload = {
                    "offset": self._update_offset,
                    "timeout": 25,
                    "allowed_updates": ["message", "callback_query"],
                }
                resp = await self._client.post(url, json=payload, timeout=35.0)
                data = resp.json()

                if not data.get("ok"):
                    logger.warning("getUpdates returned non-ok: %s", data.get("description"))
                    await asyncio.sleep(5)
                    continue

                updates: List[Dict[str, Any]] = data.get("result", [])
                for upd in updates:
                    self._update_offset = max(self._update_offset, upd.get("update_id", 0) + 1)

                    if "callback_query" in upd:
                        await self.handle_callback_query(upd["callback_query"])
                    elif "message" in upd:
                        await self.handle_message(upd["message"])

            except asyncio.CancelledError:
                break
            except httpx.TimeoutException:
                # Normal for long-polling
                continue
            except Exception as exc:
                logger.error("Error in Telegram polling loop: %s", exc)
                await asyncio.sleep(3)

    async def _dashboard_timer_loop(self) -> None:
        """Periodic background refresh of the pinned dashboard (every 60 seconds)."""
        while self._is_polling:
            try:
                await asyncio.sleep(60)
                if self._is_polling and self.settings.live_dashboard_enabled:
                    await self.update_dashboard(force=False)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Error in dashboard timer loop: %s", exc)
                await asyncio.sleep(10)
