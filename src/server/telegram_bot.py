"""
Asynchronous Telegram Bot Service for Factorio P2P Cloud Sync & Coordinator.
Provides lightweight Telegram Bot API integration via httpx.AsyncClient (~3-4 MB RAM),
a pinned live status dashboard, interactive inline settings menu, and long-polling updates.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
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

    def to_dict(self) -> Dict[str, Any]:
        """Serializes settings to a dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TelegramBotSettings":
        """Instantiates settings from a dictionary, ignoring unknown keys."""
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
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
        self._polling_task: Optional[asyncio.Task] = None
        self._dashboard_timer_task: Optional[asyncio.Task] = None
        self._is_polling = False
        self._update_offset = 0

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
            guest_names = [html.escape(str(g.get("name", "Игрок"))) for g in guests]
            lines.append(f"Гости ({len(guests)}): {', '.join(guest_names)}")
        elif state == "hosting_active":
            lines.append("Гости: нет подключений")

        # Save metadata
        if self.storage_manager:
            meta = self.storage_manager.get_current_save_metadata()
            if meta.get("exists"):
                version = meta.get("version", 1)
                size_mb = meta.get("size_bytes", 0) / (1024 * 1024)
                uploaded_by = html.escape(str(meta.get("uploaded_by", "Неизвестно")))
                lines.append(f"Мир в сейфе: v{version} ({size_mb:.1f} МБ)")
                lines.append(f"Сохранил: {uploaded_by}")
            else:
                lines.append("Мир в сейфе: нет активного сохранения")

        utc_now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        lines.append(f"\n<i>Обновлено: {utc_now}</i>")

        return "\n".join(lines)

    def build_dashboard_keyboard(self) -> Dict[str, Any]:
        """Builds inline keyboard markup for the pinned live dashboard."""
        return {
            "inline_keyboard": [
                [
                    {"text": "Обновить статус", "callback_data": "dash:refresh"},
                    {"text": "Настройки бота", "callback_data": "menu:open"},
                ]
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

        # Deduplicate using content hash
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not force and content_hash == self._last_dashboard_hash:
            logger.debug("update_dashboard: Content unchanged, skipping API call.")
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
        return result

    # --------------------------------------------------------------------------
    # Interactive Settings Menu
    # --------------------------------------------------------------------------

    def format_settings_menu_html(self) -> str:
        """Formats the settings menu overview text."""
        return (
            "<b>Настройки Telegram-бота Factorio</b>\n\n"
            "Управляйте уведомлениями, закрепленным дашбордом и ИИ-хроникой:\n"
            "Нажимайте кнопки ниже для переключения параметров."
        )

    def build_settings_keyboard(self) -> Dict[str, Any]:
        """
        Constructs the interactive settings menu inline keyboard.
        Strictly zero emojis.
        """
        def toggle_label(name: str, state: bool) -> str:
            status = "[ВКЛ]" if state else "[ВЫКЛ]"
            return f"{name}: {status}"

        # Schedule mode label lookup
        curr_sched = dict(AI_SCHEDULE_MODES).get(self.settings.ai_schedule_mode, "Каждый час")
        curr_style = dict(AI_PERSONA_STYLES).get(self.settings.ai_persona_style, "Репортер")

        keyboard: List[List[Dict[str, str]]] = [
            # Notification forwarding toggles
            [
                {
                    "text": toggle_label("Хост старт", self.settings.notify_host_start),
                    "callback_data": "toggle:notify_host_start",
                },
                {
                    "text": toggle_label("Гость вошел", self.settings.notify_guest_join),
                    "callback_data": "toggle:notify_guest_join",
                },
            ],
            [
                {
                    "text": toggle_label("Гость вышел", self.settings.notify_guest_leave),
                    "callback_data": "toggle:notify_guest_leave",
                },
                {
                    "text": toggle_label("Автосейвы", self.settings.notify_rolling_save),
                    "callback_data": "toggle:notify_rolling_save",
                },
            ],
            [
                {
                    "text": toggle_label("Финал сессии", self.settings.notify_session_end),
                    "callback_data": "toggle:notify_session_end",
                },
                {
                    "text": toggle_label("Без звука", self.settings.silent_notifications),
                    "callback_data": "toggle:silent_notifications",
                },
            ],
            [
                {
                    "text": toggle_label("Живой дашборд", self.settings.live_dashboard_enabled),
                    "callback_data": "toggle:live_dashboard_enabled",
                },
            ],
            # AI Chronicle settings
            [
                {
                    "text": toggle_label("ИИ-хроника", self.settings.ai_chronicle_enabled),
                    "callback_data": "toggle:ai_chronicle_enabled",
                },
            ],
            [
                {
                    "text": f"Частота ИИ: {curr_sched}",
                    "callback_data": "cycle:ai_schedule_mode",
                },
            ],
            [
                {
                    "text": f"Стиль ИИ: {curr_style}",
                    "callback_data": "cycle:ai_persona_style",
                },
            ],
            # Actions
            [
                {"text": "Задать ключ OpenRouter", "callback_data": "action:prompt_openrouter_key"},
                {"text": "Изменить заголовок", "callback_data": "action:prompt_title"},
            ],
            [
                {"text": "Пересоздать закреп", "callback_data": "action:repin"},
                {"text": "Создать Код Дружбы", "callback_data": "action:prompt_invite"},
            ],
            [
                {"text": "Закрыть меню", "callback_data": "menu:close"},
            ],
        ]

        return {"inline_keyboard": keyboard}

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

        logger.debug("Received callback: data='%s', user=%d, msg=%s", cb_data, user_id, msg_id)

        if not self._is_user_authorized(user_id):
            await self.answer_callback_query(
                callback_query_id=cb_id,
                text="У вас нет прав администратора для изменения настроек.",
                show_alert=True,
            )
            return True

        if cb_data == "dash:refresh":
            await self.update_dashboard(force=True)
            await self.answer_callback_query(callback_query_id=cb_id, text="Статус обновлен!")
            return True

        if cb_data == "menu:open":
            menu_text = self.format_settings_menu_html()
            menu_kb = self.build_settings_keyboard()
            await self.send_message(text=menu_text, chat_id=chat_id, reply_markup=menu_kb)
            await self.answer_callback_query(callback_query_id=cb_id)
            return True

        if cb_data == "menu:close":
            if msg_id and chat_id:
                await self.delete_message(message_id=msg_id, chat_id=chat_id)
            await self.answer_callback_query(callback_query_id=cb_id, text="Меню закрыто.")
            return True

        if cb_data.startswith("toggle:"):
            field_name = cb_data.split(":", 1)[1]
            if hasattr(self.settings, field_name):
                current_val = getattr(self.settings, field_name)
                if isinstance(current_val, bool):
                    new_val = not current_val
                    setattr(self.settings, field_name, new_val)
                    self.save_settings()

                    # Refresh menu inline keyboard
                    if msg_id and chat_id:
                        await self.edit_message_text(
                            message_id=msg_id,
                            chat_id=chat_id,
                            text=self.format_settings_menu_html(),
                            reply_markup=self.build_settings_keyboard(),
                        )

                    # Trigger dashboard update if live dashboard was re-enabled
                    if field_name == "live_dashboard_enabled" and new_val:
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

            if msg_id and chat_id:
                await self.edit_message_text(
                    message_id=msg_id,
                    chat_id=chat_id,
                    text=self.format_settings_menu_html(),
                    reply_markup=self.build_settings_keyboard(),
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
            if self.auth_manager:
                try:
                    invite = self.auth_manager.create_invite(ttl_seconds=86400, note="Telegram Bot")
                    ticket = invite.get("ticket", "")
                    await self.send_message(
                        chat_id=chat_id,
                        text=(
                            f"Новый Код Дружбы создан:\n"
                            f"<code>{ticket}</code>\n\n"
                            f"Срок действия: 24 часа. Одноразовый."
                        ),
                    )
                    await self.answer_callback_query(callback_query_id=cb_id, text="Код Дружбы создан!")
                except Exception as exc:
                    logger.error("Failed to create invite via bot callback: %s", exc)
                    await self.answer_callback_query(
                        callback_query_id=cb_id,
                        text="Ошибка создания кода",
                        show_alert=True,
                    )
            else:
                await self.answer_callback_query(
                    callback_query_id=cb_id,
                    text="Управление билетами не активно (auth_manager не подключен).",
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
        msg_id = message.get("message_id")

        if not text or not chat_id:
            return False

        if not text.startswith("/"):
            return False

        parts = text.split(maxsplit=1)
        raw_cmd = parts[0].lower()
        cmd = raw_cmd.split("@")[0]  # Strip bot mention (e.g. /start@MyBot -> /start)
        args = parts[1].strip() if len(parts) > 1 else ""

        logger.info("Routing bot command: '%s', user=%d, chat=%s", cmd, user_id, chat_id)

        if cmd in ("/start", "/setup"):
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
            menu_text = self.format_settings_menu_html()
            menu_kb = self.build_settings_keyboard()
            await self.send_message(chat_id=chat_id, text=menu_text, reply_markup=menu_kb)
            return True

        if cmd == "/status":
            status_text = self.format_dashboard_html()
            await self.send_message(chat_id=chat_id, text=status_text)
            return True

        if cmd == "/invite":
            if not self._is_user_authorized(user_id):
                await self.send_message(chat_id=chat_id, text="У вас нет прав для создания кодов.")
                return True
            if self.auth_manager:
                try:
                    invite = self.auth_manager.create_invite(ttl_seconds=86400, note="Telegram Bot")
                    ticket = invite.get("ticket", "")
                    await self.send_message(
                        chat_id=chat_id,
                        text=f"Код Дружбы: <code>{ticket}</code>\nДействует 24 часа.",
                    )
                except Exception as exc:
                    await self.send_message(chat_id=chat_id, text=f"Ошибка создания кода: {exc}")
            else:
                await self.send_message(chat_id=chat_id, text="Менеджер авторизации не подключен.")
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

        return False

    # --------------------------------------------------------------------------
    # Long Polling Update Loop
    # --------------------------------------------------------------------------

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
        logger.info("Telegram Bot long-polling and dashboard timer started.")

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
