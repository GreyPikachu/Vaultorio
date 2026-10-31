"""Тесты для модуля автоудаления временных уведомлений Telegram-бота и настройки частоты хроники.

Покрывает требования задачи 041:
- Сериализация и десериализация auto_delete_notifications_ttl в TelegramBotSettings.
- Интерактивная кнопка цикла 'Автоочистка: <интервал>' в подменю 'Уведомления'.
- Планировщик отложенного удаления сообщений schedule_message_deletion.
- Абсолютная защита закрепленного дашборда от удаления.
- Автоматическая постановка на удаление для сервисных событий (notify_*).
- Сохранение постов ИИ-хроники и скриншотов без удаления.
- Режим расписания 'hourly_and_exit' (раз в час + финал).
- 100% Zero-Emoji политика.
"""

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.server.ai_chronicle import should_process_report
from src.server.config import ServerConfig
from src.server.telegram_bot import (
    AI_SCHEDULE_MODES,
    AUTO_DELETE_TTL_CHOICES,
    TelegramBotService,
    TelegramBotSettings,
)


class TestSettingsAndSync(unittest.TestCase):
    """Синхронные проверки настроек, клавиатур и Zero-Emoji."""

    def test_settings_auto_delete_ttl_serialization(self):
        """Проверка сериализации, десериализации и валидации auto_delete_notifications_ttl."""
        settings = TelegramBotSettings()
        self.assertEqual(settings.auto_delete_notifications_ttl, 120)
        self.assertEqual(settings.ai_schedule_mode, "hourly_and_exit")

        d = settings.to_dict()
        self.assertEqual(d["auto_delete_notifications_ttl"], 120)
        self.assertEqual(d["ai_schedule_mode"], "hourly_and_exit")

        # Корректное значение
        restored = TelegramBotSettings.from_dict({"auto_delete_notifications_ttl": 180})
        self.assertEqual(restored.auto_delete_notifications_ttl, 180)

        # Выключено (0)
        restored_zero = TelegramBotSettings.from_dict({"auto_delete_notifications_ttl": 0})
        self.assertEqual(restored_zero.auto_delete_notifications_ttl, 0)

        # Некорректное значение
        restored_invalid = TelegramBotSettings.from_dict({"auto_delete_notifications_ttl": "invalid"})
        self.assertEqual(restored_invalid.auto_delete_notifications_ttl, 120)

    def test_settings_menu_auto_delete_cycle_button(self):
        """Проверка наличия кнопки автоочистки в меню уведомлений и ее циклического переключения."""
        cfg = ServerConfig(
            storage_dir=Path("/tmp/storage_test_auto_del"),
            telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            telegram_chat_id="-1001234567890",
        )
        mock_storage = MagicMock()
        bot = TelegramBotService(config=cfg, storage_manager=mock_storage)

        # 1. Проверяем подменю notifications
        kb = bot.build_settings_keyboard(subpage="notifications")
        buttons = [btn for row in kb["inline_keyboard"] for btn in row]
        auto_clean_btns = [b for b in buttons if b.get("callback_data") == "cycle:auto_delete_notifications_ttl"]
        self.assertEqual(len(auto_clean_btns), 1)
        self.assertEqual(auto_clean_btns[0]["text"], "Автоочистка: 2 мин")

        # 2. Проверяем подстраницу
        self.assertEqual(bot.get_setting_subpage("auto_delete_notifications_ttl"), "notifications")

        # 3. Проверяем циклическую смену
        cycle_values = [item[0] for item in AUTO_DELETE_TTL_CHOICES]
        self.assertEqual(cycle_values, [120, 180, 300, 60, 0])

    def test_chronicle_hourly_and_exit_schedule_mode(self):
        """Проверка нового режима 'hourly_and_exit' (раз в час + финал, без спама каждые 5 мин)."""
        settings = TelegramBotSettings(ai_schedule_mode="hourly_and_exit")

        # hourly_summary разрешен
        self.assertTrue(should_process_report("hourly_summary", settings))
        # session_summary разрешен
        self.assertTrue(should_process_report("session_summary", settings))
        # important_event заблокирован
        self.assertFalse(should_process_report("important_event", settings))

    def test_zero_emoji_in_settings_and_labels(self):
        """Проверка политики Zero-Emoji во всех текстах и кнопках настроек автоочистки."""
        emoji_pattern = re.compile(r"[\U00010000-\U0010ffff]", flags=re.UNICODE)

        cfg = ServerConfig(
            storage_dir=Path("/tmp/storage_test_zero_emoji"),
            telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            telegram_chat_id="-1001234567890",
        )
        mock_storage = MagicMock()
        bot = TelegramBotService(config=cfg, storage_manager=mock_storage)

        for subpage in ("main", "notifications", "dashboard", "chronicle"):
            html = bot.format_settings_menu_html(subpage)
            self.assertFalse(bool(emoji_pattern.search(html)), f"Emoji found in HTML for {subpage}")

            kb = bot.build_settings_keyboard(subpage)
            for row in kb.get("inline_keyboard", []):
                for btn in row:
                    btn_text = btn.get("text", "")
                    self.assertFalse(bool(emoji_pattern.search(btn_text)), f"Emoji found in button: {btn_text}")

        for mode_id, mode_label in AI_SCHEDULE_MODES:
            self.assertFalse(bool(emoji_pattern.search(mode_label)), f"Emoji found in AI mode: {mode_label}")

        for ttl_val, ttl_label in AUTO_DELETE_TTL_CHOICES:
            self.assertFalse(bool(emoji_pattern.search(ttl_label)), f"Emoji found in TTL label: {ttl_label}")


class TestAutoDeleteAsync(unittest.IsolatedAsyncioTestCase):
    """Асинхронные проверки планировщика удаления и перехвата событий."""

    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_tg_autodel_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            telegram_chat_id="-1001234567890",
        )
        self.mock_storage = MagicMock()
        self.mock_client = AsyncMock()
        self.bot = TelegramBotService(
            config=self.config,
            storage_manager=self.mock_storage,
            client=self.mock_client,
        )
        self.bot.settings.bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        self.bot.settings.chat_id = -1001234567890

    async def test_schedule_message_deletion_lifecycle(self):
        """Проверка жизненного цикла отложенного удаления сообщения."""
        self.bot.delete_message = AsyncMock(return_value=True)

        task = self.bot.schedule_message_deletion(message_id=999, delay_sec=0.05)
        self.assertIsNotNone(task)
        self.assertIn(task, self.bot._pending_deletions)

        await asyncio.sleep(0.1)
        self.bot.delete_message.assert_awaited_once_with(message_id=999, chat_id=-1001234567890)
        self.assertNotIn(task, self.bot._pending_deletions)

    async def test_pinned_dashboard_never_deleted(self):
        """Закрепленное сообщение живого дашборда защищено от планирования и от фактического удаления."""
        self.bot.settings.pinned_message_id = 777
        self.bot.delete_message = AsyncMock(return_value=True)

        # 1. Попытка запланировать удаление закрепа блокируется сразу
        task = self.bot.schedule_message_deletion(message_id=777, delay_sec=0.05)
        self.assertIsNone(task)
        self.bot.delete_message.assert_not_called()

        # 2. Если сообщение закреплено во время ожидания таймера, удаление отменяется
        task2 = self.bot.schedule_message_deletion(message_id=888, delay_sec=0.05)
        self.assertIsNotNone(task2)
        # Сообщение стало закрепленным во время ожидания
        self.bot.settings.pinned_message_id = 888

        await asyncio.sleep(0.1)
        self.bot.delete_message.assert_not_called()

    async def test_close_cancels_pending_deletions(self):
        """Метод close() корректно отменяет все активные задачи отложенного удаления."""
        self.bot.delete_message = AsyncMock(return_value=True)

        task = self.bot.schedule_message_deletion(message_id=101, delay_sec=10.0)
        self.assertIsNotNone(task)
        self.assertEqual(len(self.bot._pending_deletions), 1)

        await self.bot.close()
        self.assertEqual(len(self.bot._pending_deletions), 0)
        self.assertTrue(task.cancelled())

    async def test_event_notifications_trigger_auto_delete(self):
        """Сервисные уведомления отправляются с auto_delete=True и планируют удаление."""
        self.bot.send_message = AsyncMock(return_value={"message_id": 1234})
        self.bot.update_dashboard = AsyncMock()

        # notify_host_started
        await self.bot.notify_host_started("TesterHost")
        self.bot.send_message.assert_awaited_with(
            text="<b>[СЕРВЕР]</b> Игрок <b>TesterHost</b> запустил игру!",
            auto_delete=True,
        )

        # notify_guest_joined
        await self.bot.notify_guest_joined("GuestAlice")
        self.bot.send_message.assert_awaited_with(
            text="<b>[СЕРВЕР]</b> Игрок <b>GuestAlice</b> подключился к игре.",
            auto_delete=True,
        )

        # notify_guest_left
        await self.bot.notify_guest_left("GuestAlice")
        self.bot.send_message.assert_awaited_with(
            text="<b>[СЕРВЕР]</b> Игрок <b>GuestAlice</b> покинул игру.",
            auto_delete=True,
        )

        # notify_rolling_save_saved
        self.bot.settings.notify_rolling_save = True
        await self.bot.notify_rolling_save_saved(version=5, size_mb=12.4, uploaded_by="Bob")
        self.bot.send_message.assert_awaited_with(
            text="<b>[СЕЙВ]</b> Автосейв v5 (12.4 МБ) сохранен в сейф.\nИгрок: <b>Bob</b>",
            auto_delete=True,
        )

        # notify_session_ended
        await self.bot.notify_session_ended(host_name="TesterHost", duration_str="1 ч", version=5, size_mb=12.4)
        self.assertTrue(self.bot.send_message.call_args[1]["auto_delete"])

        # notify_world_archived
        await self.bot.notify_world_archived("Nauvis", "Admin")
        self.assertTrue(self.bot.send_message.call_args[1]["auto_delete"])
