"""
Модульные и интеграционные тесты для OpenRouterChronicleService и эндпоинта POST /chronicle/report.
Покрывают:
1. Выбор системных промптов и персон (reporter, ship_ai, friendly).
2. Форматирование телеметрии событий Factorio (format_events_for_llm).
3. Санитайзер Telegram HTML (clean_telegram_html).
4. Локальный шаблонный генератор (format_fallback_chronicle_post).
5. Фильтрацию расписания (should_process_report).
6. Кэширование каталога моделей OpenRouter.
7. 3-уровневый каскад генерации (OpenRouter Vision -> Pollinations Text -> Offline Template).
8. Авторизацию, валидацию и работу эндпоинта POST /chronicle/report (включая multipart-скриншоты).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from src.server.ai_chronicle import (
    FALLBACK_SYSTEM_PROMPT,
    SYSTEM_PROMPTS,
    VERIFIED_FREE_VISION_MODELS,
    OpenRouterChronicleService,
    clean_telegram_html,
    format_events_for_llm,
    format_fallback_chronicle_post,
    should_process_report,
)
from src.server.api import create_app
from src.server.auth_manager import AuthManager
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager
from src.server.storage import SaveStorageManager
from src.server.telegram_bot import TelegramBotService, TelegramBotSettings


class TestAiChronicleUnit(unittest.TestCase):
    """Модульные тесты вспомогательных функций и логики ai_chronicle."""

    def test_system_prompts_and_styles(self):
        """Проверяет наличие всех стилей персон и требований к русскому языку."""
        self.assertIn("reporter", SYSTEM_PROMPTS)
        self.assertIn("ship_ai", SYSTEM_PROMPTS)
        self.assertIn("friendly", SYSTEM_PROMPTS)

        for style, prompt in SYSTEM_PROMPTS.items():
            self.assertIn("IN RUSSIAN", prompt)
            self.assertIn("Telegram HTML", prompt)
            self.assertTrue(len(prompt) > 100)

    def test_format_events_for_llm(self):
        """Проверяет форматирование событий телеметрии в читаемый лог для LLM."""
        events = [
            {
                "tick": 14200,
                "event_type": "player_died",
                "data": {"player_name": "graypikachu", "cause": "big-biter", "position": {"x": 10, "y": -20}},
            },
            {
                "tick": 48000,
                "event_type": "research_finished",
                "data": {"technology": "nuclear-power", "level": 1},
            },
            {
                "tick": 92000,
                "event_type": "rocket_launched",
                "data": {"cargo_count": 1},
            },
            {
                "tick": 95000,
                "event_type": "built_entity",
                "data": {"entity_name": "nuclear-reactor", "player_name": "graypikachu"},
            },
            {
                "tick": 100000,
                "event_type": "hourly_stats",
                "data": {"evolution_factor": 0.4567, "players_online": 2},
            },
        ]
        text = format_events_for_llm(
            events=events,
            trigger_type="important_event",
            host_name="graypikachu",
            session_duration_min=45,
        )
        self.assertIn("Important Event (Alert)", text)
        self.assertIn("graypikachu", text)
        self.assertIn("45 minutes", text)
        self.assertIn("DEATH: Player 'graypikachu' killed by 'big-biter'", text)
        self.assertIn("RESEARCH: Technology completed 'nuclear-power'", text)
        self.assertIn("VICTORY: Rocket launched", text)
        self.assertIn("MILESTONE: Entity 'nuclear-reactor'", text)
        self.assertIn("STATS: Evolution factor 0.457", text)

    def test_format_events_for_llm_empty(self):
        """Проверяет форматирование при отсутствии событий (мирное развитие)."""
        text = format_events_for_llm(events=[], trigger_type="hourly_summary", host_name="Alice", session_duration_min=60)
        self.assertIn("Hourly Base Summary", text)
        self.assertIn("No specific incident events recorded", text)

    def test_clean_telegram_html(self):
        """Проверяет корректность очистки и конвертации HTML-тегов для Telegram."""
        # Markdown bold и italic
        raw1 = "**Внимание!** База под *атакой* кусак. Код: `nuclear-power`."
        clean1 = clean_telegram_html(raw1)
        self.assertIn("<b>Внимание!</b>", clean1)
        self.assertIn("<i>атакой</i>", clean1)
        self.assertIn("<code>nuclear-power</code>", clean1)

        # Markdown заголовки и блоки кода
        raw2 = "```html\n### Отчет инженера\nЛиния работает.<br>Все в норме.\n```"
        clean2 = clean_telegram_html(raw2)
        self.assertIn("<b>Отчет инженера</b>", clean2)
        self.assertIn("Линия работает.\nВсе в норме.", clean2)
        self.assertNotIn("```", clean2)

        # Удаление неподдерживаемых тегов
        raw3 = "<div><h1>Заголовок</h1><p>Текст с <b>жирным</b> и <script>alert(1)</script></p></div>"
        clean3 = clean_telegram_html(raw3)
        self.assertNotIn("<div>", clean3)
        self.assertNotIn("<h1>", clean3)
        self.assertNotIn("<script>", clean3)
        self.assertIn("<b>жирным</b>", clean3)

        # Балансировка незакрытых тегов
        raw4 = "<b>Незакрытый жирный текст"
        clean4 = clean_telegram_html(raw4)
        self.assertTrue(clean4.endswith("</b>"))

    def test_format_fallback_chronicle_post(self):
        """Проверяет генерацию автономного локального шаблона сводки на русском языке."""
        events = [
            {"event_type": "player_died", "data": {"player_name": "Bob", "cause": "поезд"}},
            {"event_type": "research_finished", "data": {"technology": "laser"}},
            {"event_type": "built_entity", "data": {"entity_name": "artillery-turret"}},
            {"event_type": "rocket_launched", "data": {}},
            {"event_type": "hourly_stats", "data": {"evolution_factor": 0.52}},
        ]
        post = format_fallback_chronicle_post(
            events=events,
            trigger_type="hourly_summary",
            host_name="Alice",
            session_duration_min=120,
        )
        self.assertIn("Регулярный отчет хроники базы", post)
        self.assertIn("Alice", post)
        self.assertIn("120 мин.", post)
        self.assertIn("<b>Потери персонала:</b> Bob (причина: поезд)", post)
        self.assertIn("<b>Завершенные исследования:</b> <code>laser</code>", post)
        self.assertIn("<b>Запуски ракет:</b> отправлено на орбиту: <b>1</b>", post)
        self.assertIn("<b>Новые рубежи:</b> <code>artillery-turret</code>", post)
        self.assertIn("<b>Фактор эволюции фауны:</b> <code>0.520</code>", post)
        self.assertIn("Сводка сформирована локальным протоколом хроники", post)

    def test_format_fallback_chronicle_post_empty(self):
        """Проверяет локальный шаблон при отсутствии инцидентов."""
        post = format_fallback_chronicle_post(events=[], trigger_type="session_summary", host_name="Alice")
        self.assertIn("Итоги исследовательской смены", post)
        self.assertIn("Производственные линии работают в штатном режиме", post)

    def test_should_process_report_modes(self):
        """Проверяет фильтрацию типов триггеров по режимам расписания."""
        # 1. Хроника отключена
        s_disabled = TelegramBotSettings(ai_chronicle_enabled=False)
        self.assertFalse(should_process_report("important_event", s_disabled))
        self.assertFalse(should_process_report("hourly_summary", s_disabled))
        self.assertFalse(should_process_report("session_summary", s_disabled))

        # 2. Режим hourly_and_events
        s_hourly = TelegramBotSettings(ai_chronicle_enabled=True, ai_schedule_mode="hourly_and_events")
        self.assertTrue(should_process_report("important_event", s_hourly))
        self.assertTrue(should_process_report("hourly_summary", s_hourly))
        self.assertTrue(should_process_report("session_summary", s_hourly))

        # 3. Режим important_only
        s_important = TelegramBotSettings(ai_chronicle_enabled=True, ai_schedule_mode="important_only")
        self.assertTrue(should_process_report("important_event", s_important))
        self.assertFalse(should_process_report("hourly_summary", s_important))
        self.assertTrue(should_process_report("session_summary", s_important))

        # 4. Режим only_on_exit
        s_exit = TelegramBotSettings(ai_chronicle_enabled=True, ai_schedule_mode="only_on_exit")
        self.assertFalse(should_process_report("important_event", s_exit))
        self.assertFalse(should_process_report("hourly_summary", s_exit))
        self.assertTrue(should_process_report("session_summary", s_exit))

        # 5. None settings
        self.assertTrue(should_process_report("hourly_summary", None))


class TestAiChronicleServiceCascade(unittest.IsolatedAsyncioTestCase):
    """Асинхронные тесты 3-уровневого каскада генерации OpenRouterChronicleService."""

    async def test_model_caching(self):
        """Проверяет кэширование списка моделей OpenRouter на 1 час."""
        call_count = 0

        def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if "api/v1/models" in str(request.url):
                call_count += 1
                return httpx.Response(200, json={"data": [{"id": "google/gemma-4-31b-it:free", "pricing": {"prompt": "0"}, "architecture": {"input_modalities": ["image"]}}]})
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = OpenRouterChronicleService(http_client=client)
            models1 = await service.get_available_vision_models()
            models2 = await service.get_available_vision_models()
            self.assertEqual(call_count, 1)  # Запрос каталога выполнен ровно 1 раз
            self.assertIn("google/gemma-4-31b-it:free", models1)
            self.assertEqual(models1, models2)

    async def test_tier1_openrouter_vision_success(self):
        """Проверяет успешную генерацию сводки через OpenRouter Vision API (Уровень 1)."""
        settings = TelegramBotSettings(
            openrouter_api_key="sk-or-test-key",
            openrouter_model="google/gemma-4-31b-it:free",
        )

        def mock_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "openrouter.ai/api/v1/chat/completions" in url_str:
                self.assertIn("Bearer sk-or-test-key", request.headers.get("Authorization", ""))
                payload = json.loads(request.content.decode("utf-8"))
                # Проверяем наличие base64-картинки в user_content
                messages = payload.get("messages", [])
                user_msg = messages[1]["content"]
                has_image = any(item.get("type") == "image_url" for item in user_msg)
                self.assertTrue(has_image)

                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "<b>Сводка с Наувиса:</b> Инженер успешно запустил ядерный реактор! Оборона держится стойко."
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = OpenRouterChronicleService(settings=settings, http_client=client)
            text, tier = await service.generate_chronicle_post(
                events=[{"event_type": "built_entity", "data": {"entity_name": "nuclear-reactor"}}],
                trigger_type="important_event",
                host_name="graypikachu",
                session_duration_min=30,
                screenshot_bytes=b"\x89PNG\r\n\x1a\nfakeimagebytes",
                mime_type="image/png",
            )
            self.assertEqual(tier, "openrouter_vision")
            self.assertIn("Инженер успешно запустил ядерный реактор", text)
            self.assertIn("<b>Сводка с Наувиса:</b>", text)

    async def test_tier1_fallback_to_next_model_on_429(self):
        """Проверяет переход на следующую модель OpenRouter при коде 429 (Rate Limit)."""
        settings = TelegramBotSettings(openrouter_api_key="sk-or-test-key")
        attempted_models = []

        def mock_handler(request: httpx.Request) -> httpx.Response:
            if "api/v1/chat/completions" in str(request.url):
                payload = json.loads(request.content.decode("utf-8"))
                model = payload.get("model")
                attempted_models.append(model)
                if len(attempted_models) == 1:
                    # Первая модель перегружена (429)
                    return httpx.Response(429, json={"error": {"message": "Rate limit reached"}})
                # Вторая модель отвечает успешно
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"content": "<b>Вторая модель:</b> База расширяется планово."}}
                        ]
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = OpenRouterChronicleService(settings=settings, http_client=client)
            text, tier = await service.generate_chronicle_post(
                events=[],
                trigger_type="hourly_summary",
                host_name="Alice",
            )
            self.assertEqual(tier, "openrouter_vision")
            self.assertEqual(len(attempted_models), 2)
            self.assertIn("Вторая модель", text)

    async def test_tier2_pollinations_when_no_api_key(self):
        """Проверяет вызов Zero-Key Pollinations Text API (Уровень 2), если ключ OpenRouter не задан."""
        settings = TelegramBotSettings(openrouter_api_key="")  # Ключ не задан

        def mock_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "text.pollinations.ai" in url_str:
                payload = json.loads(request.content.decode("utf-8"))
                # Проверяем, что в Pollinations передается ТОЛЬКО текст (без image_url)
                messages = payload.get("messages", [])
                user_content = messages[1]["content"]
                self.assertIsInstance(user_content, str)  # Чистая строка текста
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "<b>Хроника Pollinations:</b> Автоматизация производства пластика завершена!"
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = OpenRouterChronicleService(settings=settings, http_client=client)
            text, tier = await service.generate_chronicle_post(
                events=[{"event_type": "research_finished", "data": {"technology": "plastics"}}],
                trigger_type="hourly_summary",
                host_name="Bob",
                screenshot_bytes=b"fakepng",  # Скриншот есть, но в Pollinations он не отправляется
            )
            self.assertEqual(tier, "pollinations_text")
            self.assertIn("Хроника Pollinations", text)

    async def test_tier3_offline_template_fallback(self):
        """Проверяет откат на локальный шаблон (Уровень 3), когда все сетевые сервисы падают."""
        settings = TelegramBotSettings(openrouter_api_key="sk-key")

        def mock_handler(request: httpx.Request) -> httpx.Response:
            # Все эндпоинты возвращают сетевую ошибку 500
            return httpx.Response(500, text="Internal Server Error")

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = OpenRouterChronicleService(settings=settings, http_client=client)
            text, tier = await service.generate_chronicle_post(
                events=[{"event_type": "player_died", "data": {"player_name": "Alice", "cause": "поезд"}}],
                trigger_type="important_event",
                host_name="Alice",
                session_duration_min=50,
            )
            self.assertEqual(tier, "fallback_template")
            self.assertIn("Экстренная сводка с Наувиса", text)
            self.assertIn("<b>Потери персонала:</b> Alice (причина: поезд)", text)


class TestApiChronicleReportEndpoint(unittest.TestCase):
    """Интеграционные тесты эндпоинта POST /chronicle/report через TestClient."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.tmp_dir.name)
        self.config = ServerConfig(
            storage_dir=self.storage_dir,
            require_device_auth=True,  # Включаем проверку токена
        )
        self.auth_manager = AuthManager(self.config)
        self.session_manager = SessionLockManager(self.config)
        self.storage_manager = SaveStorageManager(self.config)

        # Создаем валидное зарегистрированное устройство
        invite = self.auth_manager.create_invite()
        redeem_res = self.auth_manager.redeem_invite(invite["ticket"], player_name="TestPlayer")
        self.device_token = redeem_res["device_token"]

        # Мокируем TelegramBotService и OpenRouterChronicleService
        self.mock_tg_bot = mock.MagicMock(spec=TelegramBotService)
        self.mock_tg_bot.settings = TelegramBotSettings(
            ai_chronicle_enabled=True,
            ai_schedule_mode="hourly_and_events",
        )
        self.mock_tg_bot.send_photo = mock.AsyncMock(return_value={"message_id": 999})
        self.mock_tg_bot.send_message = mock.AsyncMock(return_value={"message_id": 998})

        self.mock_chronicle = mock.MagicMock(spec=OpenRouterChronicleService)
        self.mock_chronicle.generate_chronicle_post = mock.AsyncMock(
            return_value=("<b>Хроника:</b> Все работает отлично!", "openrouter_vision")
        )
        self.mock_chronicle.close = mock.AsyncMock()

        self.app = create_app(
            config=self.config,
            storage=self.storage_manager,
            session_manager=self.session_manager,
            auth_manager=self.auth_manager,
            telegram_bot=self.mock_tg_bot,
            chronicle_service=self.mock_chronicle,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_chronicle_report_unauthorized(self):
        """Проверяет отклонение запроса без валидного X-Device-Token (403 Forbidden)."""
        res = self.client.post(
            "/chronicle/report",
            data={"events_json": "[]", "trigger_type": "hourly_summary"},
        )
        self.assertEqual(res.status_code, 403)

    def test_chronicle_report_success_with_screenshot(self):
        """Проверяет успешный прием отчета с multipart-скриншотом и передачу в send_photo."""
        events = [{"event_type": "built_entity", "data": {"entity_name": "rocket-silo"}}]
        screenshot_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

        res = self.client.post(
            "/chronicle/report",
            headers={"X-Device-Token": self.device_token},
            data={
                "events_json": json.dumps(events),
                "trigger_type": "important_event",
            },
            files={"screenshot": ("base.png", screenshot_data, "image/png")},
        )
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertTrue(json_data.get("success"))
        self.assertEqual(json_data.get("status"), "queued")

        # Проверяем, что фоновая задача вызвала генерацию и send_photo
        self.mock_chronicle.generate_chronicle_post.assert_called_once()
        self.mock_tg_bot.send_photo.assert_called_once()
        call_kwargs = self.mock_tg_bot.send_photo.call_args.kwargs
        self.assertEqual(call_kwargs["photo_bytes"], screenshot_data)
        self.assertIn("Все работает отлично!", call_kwargs["caption"])
        self.assertEqual(call_kwargs["filename"], "base_screenshot.png")

    def test_chronicle_report_without_screenshot(self):
        """Проверяет отправку текстовой сводки через send_message, если скриншот отсутствует."""
        res = self.client.post(
            "/chronicle/report",
            headers={"X-Device-Token": self.device_token},
            data={
                "events_json": json.dumps([{"event_type": "player_died", "data": {"cause": "кусака"}}]),
                "trigger_type": "session_summary",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("success"))

        self.mock_chronicle.generate_chronicle_post.assert_called_once()
        self.mock_tg_bot.send_message.assert_called_once()
        self.assertIn("Все работает отлично!", self.mock_tg_bot.send_message.call_args.kwargs["text"])

    def test_chronicle_report_ignored_by_schedule_mode(self):
        """Проверяет игнорирование отчета, если триггер заблокирован режимом расписания."""
        # Устанавливаем режим: только при выходе
        self.mock_tg_bot.settings.ai_schedule_mode = "only_on_exit"

        res = self.client.post(
            "/chronicle/report",
            headers={"X-Device-Token": self.device_token},
            data={
                "events_json": "[]",
                "trigger_type": "hourly_summary",  # Заблокирован для only_on_exit
            },
        )
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertTrue(json_data.get("success"))
        self.assertTrue(json_data.get("ignored"))
        self.assertEqual(json_data.get("reason"), "schedule_disabled")

        # Генерация и отправка не должны вызываться
        self.mock_chronicle.generate_chronicle_post.assert_not_called()
        self.mock_tg_bot.send_photo.assert_not_called()
        self.mock_tg_bot.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
