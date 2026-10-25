"""
Серверный модуль генерации ИИ-хроники и сводок событий Factorio (OpenRouter Vision AI + Zero-Key Fallback).

Обеспечивает:
1. Английские системные промпты для персон ('reporter', 'ship_ai', 'friendly') с выводом строго на русском языке.
2. Форматирование телеметрии событий Factorio (смерти, исследования, запуски ракет, постройки) в читаемый лог для LLM.
3. 3-уровневый отказоустойчивый каскад генерации (3-Tier Fallback):
   - Уровень 1: OpenRouter Free Vision API (полный анализ скриншота и телеметрии с каскадом моделей).
   - Уровень 2: Zero-Key Text-Only AI через Pollinations (генерация сводки по логам без передачи картинок).
   - Уровень 3: Локальный детерминированный шаблон на русском языке (полная автономность при отсутствии сети).
4. Очистку и валидацию Telegram HTML (безопасные теги <b>, <i>, <code>, <pre>).
5. Проверку правил расписания (ai_schedule_mode: 'hourly_and_events', 'important_only', 'only_on_exit').
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.server.ai_model_probe import (
    DEFAULT_FREE_VISION_CANDIDATES,
    OPENROUTER_CHAT_URL,
    POLLINATIONS_CHAT_URL,
    discover_openrouter_free_vision_models,
)
from src.server.config import ServerConfig
from src.server.telegram_bot import TelegramBotSettings

logger = logging.getLogger("server.ai_chronicle")

# Приоритетный список проверенных бесплатных мультимодальных моделей
VERIFIED_FREE_VISION_MODELS: list[str] = [
    "openrouter/free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "thinkingmachines/inkling:free",
    "thinkingmachines/inkling-small:free",
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "qwen/qwen2.5-vl-72b-instruct:free",
    "qwen/qwen2.5-vl-32b-instruct:free",
    "moonshotai/kimi-vl-a3b-thinking:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
]

# Системные промпты для различных персон
SYSTEM_PROMPTS: dict[str, str] = {
    "reporter": (
        "You are an embedded frontline war correspondent on planet Nauvis in Factorio. "
        "Analyze the attached game telemetry logs and in-game screenshot. "
        "Write an engaging, witty, and dramatic battlefield chronicle IN RUSSIAN (350-750 characters). "
        "Highlight key events: player deaths from biter attacks, tech breakthroughs, nuclear expansions, and base defenses. "
        "Use clean Telegram HTML tags (<b>, <i>, <code>). "
        "CRITICAL: Do NOT use Markdown formatting (no asterisks or markdown code blocks). Write exclusively in Russian."
    ),
    "ship_ai": (
        "You are an analytical, sarcastic ship AI monitoring the engineer's survival on planet Nauvis from orbital satellite. "
        "Analyze the attached game telemetry logs and in-game screenshot. "
        "Write an orbital observation report IN RUSSIAN (350-750 characters) with dry sci-fi wit, calculating survival probabilities "
        "and commenting on human engineering choices and casualties. "
        "Use clean Telegram HTML tags (<b>, <i>, <code>). "
        "CRITICAL: Do NOT use Markdown formatting (no asterisks). Write exclusively in Russian."
    ),
    "friendly": (
        "You are an enthusiastic and supportive co-op companion summarizing team progress on planet Nauvis in Factorio. "
        "Analyze the attached game telemetry logs and in-game screenshot. "
        "Write an uplifting, warm, and humorous progress update IN RUSSIAN (350-750 characters) celebrating new technologies, "
        "laughing off biter incidents, and praising team milestones. "
        "Use clean Telegram HTML tags (<b>, <i>, <code>). "
        "CRITICAL: Do NOT use Markdown formatting (no asterisks). Write exclusively in Russian."
    ),
}

FALLBACK_SYSTEM_PROMPT = SYSTEM_PROMPTS["reporter"]


def format_events_for_llm(
    events: list[dict[str, Any]],
    trigger_type: str = "hourly_summary",
    host_name: str = "Player",
    session_duration_min: int = 0,
) -> str:
    """Форматирует структурированный список событий в компактный лог для передачи в LLM.

    Args:
        events: Список словарей событий из events.jsonl.
        trigger_type: Тип триггера ('important_event', 'hourly_summary', 'session_summary').
        host_name: Имя хоста сессии.
        session_duration_min: Длительность игровой сессии в минутах.

    Returns:
        Строковое текстовое представление для промпта LLM.
    """
    trigger_labels = {
        "important_event": "Important Event (Alert)",
        "hourly_summary": "Hourly Base Summary",
        "session_summary": "Session Conclusion (Host disconnected)",
    }
    trigger_desc = trigger_labels.get(trigger_type, trigger_type)

    lines: list[str] = [
        f"Session Context:",
        f"- Trigger: {trigger_desc}",
        f"- Host Engineer: {host_name}",
        f"- Session Playtime: {session_duration_min} minutes",
        f"- Event Count: {len(events)}",
        "",
        "Logged Events Timeline:",
    ]

    if not events:
        lines.append("- No specific incident events recorded in this interval (peaceful factory expansion).")
    else:
        for idx, ev in enumerate(events[:30], 1):
            if not isinstance(ev, dict):
                continue
            tick = ev.get("tick", 0)
            etype = ev.get("event_type", "event")
            data = ev.get("data") or {}

            if etype == "player_died":
                p_name = data.get("player_name", "engineer")
                cause = data.get("cause", "unknown hazards")
                pos = data.get("position", {})
                pos_str = f"({pos.get('x', '?')}, {pos.get('y', '?')})" if pos else ""
                lines.append(f"  {idx}. [tick {tick}] DEATH: Player '{p_name}' killed by '{cause}' {pos_str}")
            elif etype == "research_finished":
                tech = data.get("technology", "tech")
                lvl = data.get("level", 1)
                lines.append(f"  {idx}. [tick {tick}] RESEARCH: Technology completed '{tech}' (level {lvl})")
            elif etype == "rocket_launched":
                cargo = data.get("cargo_count", 0)
                lines.append(f"  {idx}. [tick {tick}] VICTORY: Rocket launched into orbit! (cargo: {cargo})")
            elif etype == "built_entity":
                ent = data.get("entity_name", "building")
                builder = data.get("player_name", host_name)
                lines.append(f"  {idx}. [tick {tick}] MILESTONE: Entity '{ent}' constructed by '{builder}'")
            elif etype == "hourly_stats" or etype == "periodic_stats":
                evo = data.get("evolution_factor", 0.0)
                alive = data.get("players_online", 1)
                lines.append(f"  {idx}. [tick {tick}] STATS: Evolution factor {evo:.3f}, Players online: {alive}")
            else:
                lines.append(f"  {idx}. [tick {tick}] {etype}: {data}")

    lines.append("")
    lines.append("Please analyze the logged timeline above and write the Russian Telegram chronicle post:")
    return "\n".join(lines)


def clean_telegram_html(raw_text: str) -> str:
    """Очищает и нормализует HTML-разметку для безопасной отправки в Telegram API.

    Преобразует частые артефакты Markdown в HTML, удаляет неподдерживаемые теги,
    балансирует парные теги.

    Args:
        raw_text: Сырой ответ от нейросети.

    Returns:
        Валидная строка с безопасными тегами Telegram HTML.
    """
    if not raw_text or not isinstance(raw_text, str):
        return ""

    text = raw_text.strip()

    # 1. Снятие Markdown code blocks ```html ... ``` или ``` ... ```
    if text.startswith("```"):
        text = re.sub(r"^```(?:html)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # 2. Конвертация Markdown заголовков в жирный текст
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 3. Конвертация Markdown **жирный** и __жирный__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # 4. Конвертация Markdown *курсив* (осторожно, чтобы не ломать списки)
    text = re.sub(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)", r"<i>\1</i>", text)

    # 5. Конвертация инлайн-кода `code`
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)

    # 6. Замена переносов <br> на \n
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # 7. Фильтрация разрешенных тегов Telegram
    allowed_tags = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a"}

    def _tag_replacer(match: re.Match[str]) -> str:
        full_tag = match.group(0)
        is_closing = full_tag.startswith("</")
        tag_name_match = re.search(r"[a-zA-Z]+", full_tag)
        if not tag_name_match:
            return ""
        tag_name = tag_name_match.group(0).lower()

        if tag_name not in allowed_tags:
            return ""  # Удаляем неподдерживаемый тег

        if is_closing:
            return f"</{tag_name}>"
        elif tag_name == "a":
            # Сохраняем href
            href_m = re.search(r'href=[\'"]([^\'"]+)[\'"]', full_tag, flags=re.IGNORECASE)
            if href_m:
                return f'<a href="{href_m.group(1)}">'
            return "<a>"
        return f"<{tag_name}>"

    sanitized = re.sub(r"</?[a-zA-Z][^>]*>", _tag_replacer, text)

    # 8. Защита от несбалансированных тегов: если тег открыт, но не закрыт
    for tag in ("b", "i", "code", "pre", "u", "s"):
        open_count = len(re.findall(rf"<{tag}>", sanitized))
        close_count = len(re.findall(rf"</{tag}>", sanitized))
        if open_count > close_count:
            sanitized += f"</{tag}>" * (open_count - close_count)

    return sanitized.strip()


def format_fallback_chronicle_post(
    events: list[dict[str, Any]],
    trigger_type: str = "hourly_summary",
    host_name: str = "Player",
    session_duration_min: int = 0,
) -> str:
    """Формирует структурированный локальный шаблон сводки на русском языке (Уровень 3).

    Используется, если все нейросетевые шлюзы недоступны или нет подключения к интернету.

    Args:
        events: Список событий из events.jsonl.
        trigger_type: Тип триггера.
        host_name: Имя хоста.
        session_duration_min: Длительность сессии в минутах.

    Returns:
        Готовый текст для публикации в Telegram с HTML-разметкой.
    """
    header_titles = {
        "important_event": "Экстренная сводка с Наувиса",
        "hourly_summary": "Регулярный отчет хроники базы",
        "session_summary": "Итоги исследовательской смены",
    }
    title = header_titles.get(trigger_type, "Хроника планеты Наувис")

    deaths: list[str] = []
    researches: list[str] = []
    milestones: list[str] = []
    rockets: int = 0
    evolution: Optional[float] = None

    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("event_type")
        data = ev.get("data") or {}

        if etype == "player_died":
            p = data.get("player_name", "Инженер")
            c = data.get("cause", "кусаки")
            deaths.append(f"{p} (причина: {c})")
        elif etype == "research_finished":
            tech = data.get("technology", "исследование")
            researches.append(tech)
        elif etype == "rocket_launched":
            rockets += 1
        elif etype == "built_entity":
            ent = data.get("entity_name", "постройка")
            milestones.append(ent)
        elif etype in ("hourly_stats", "periodic_stats"):
            evo = data.get("evolution_factor")
            if evo is not None:
                evolution = float(evo)

    lines: list[str] = [
        f"<b>{title}</b>",
        f"Хост: <b>{html.escape(host_name)}</b> | Время в игре: <code>{session_duration_min} мин.</code>",
        "",
    ]

    has_content = False

    if deaths:
        has_content = True
        death_summary = ", ".join(deaths[:4])
        if len(deaths) > 4:
            death_summary += f" и еще {len(deaths) - 4}"
        lines.append(f"<b>Потери персонала:</b> {html.escape(death_summary)}")

    if researches:
        has_content = True
        tech_summary = ", ".join(researches[:5])
        if len(researches) > 5:
            tech_summary += f" (+{len(researches) - 5})"
        lines.append(f"<b>Завершенные исследования:</b> <code>{html.escape(tech_summary)}</code>")

    if rockets > 0:
        has_content = True
        lines.append(f"<b>Запуски ракет:</b> отправлено на орбиту: <b>{rockets}</b>")

    if milestones:
        has_content = True
        unique_m = list(dict.fromkeys(milestones))[:4]
        lines.append(f"<b>Новые рубежи:</b> <code>{html.escape(', '.join(unique_m))}</code>")

    if evolution is not None:
        lines.append(f"<b>Фактор эволюции фауны:</b> <code>{evolution:.3f}</code>")

    if not has_content:
        lines.append("<i>Производственные линии работают в штатном режиме. За интервал инцидентов не зафиксировано.</i>")

    lines.append("")
    lines.append("<i>Сводка сформирована локальным протоколом хроники.</i>")

    return "\n".join(lines)


def should_process_report(trigger_type: str, settings: Optional[TelegramBotSettings]) -> bool:
    """Проверяет, разрешена ли обработка данного триггера текущими настройками бота.

    Args:
        trigger_type: Тип триггера ('important_event', 'hourly_summary', 'session_summary').
        settings: Текущие настройки TelegramBotSettings.

    Returns:
        True, если отчет должен быть сгенерирован и отправлен в Telegram.
    """
    if not settings:
        return True

    if not getattr(settings, "ai_chronicle_enabled", True):
        logger.debug("ИИ-хроника отключена в настройках бота (ai_chronicle_enabled=False).")
        return False

    schedule_mode = getattr(settings, "ai_schedule_mode", "hourly_and_events")

    if schedule_mode == "hourly_and_events":
        return trigger_type in ("important_event", "hourly_summary", "session_summary")
    elif schedule_mode == "important_only":
        return trigger_type in ("important_event", "session_summary")
    elif schedule_mode == "only_on_exit":
        return trigger_type == "session_summary"

    return True


class OpenRouterChronicleService:
    """Серверный сервис формирования и публикации иллюстрированной хроники базы Factorio.

    Реализует 3-уровневый каскад:
    1. OpenRouter Vision API (с каскадом бесплатных мультимодальных моделей).
    2. Zero-Key Pollinations Text API (генерация текста по логам без скриншотов).
    3. Локальный шаблонный генератор (на случай отсутствия сети).
    """

    DISCOVERY_CACHE_TTL_SEC: float = 3600.0  # Кэш каталога моделей OpenRouter на 1 час

    def __init__(
        self,
        settings: Optional[TelegramBotSettings] = None,
        config: Optional[ServerConfig] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self._owns_client = False
        self._client = http_client
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=45.0)
            self._owns_client = True

        self._cached_models: list[str] = []
        self._last_discovery_time: float = 0.0

    async def close(self) -> None:
        """Освобождает внутренний HTTP-клиент."""
        if self._owns_client and self._client:
            await self._client.aclose()

    async def get_available_vision_models(self) -> list[str]:
        """Возвращает приоритетный список активных бесплатных мультимодальных моделей.

        Объединяет:
        1. Модель из настроек пользователя (settings.openrouter_model).
        2. Верифицированные стабильные модели (VERIFIED_FREE_VISION_MODELS).
        3. Динамически обнаруженные модели каталога OpenRouter (с кэшированием на 1 час).

        Returns:
            Список идентификаторов моделей в порядке приоритета.
        """
        now = time.monotonic()
        if self._cached_models and (now - self._last_discovery_time < self.DISCOVERY_CACHE_TTL_SEC):
            return list(self._cached_models)

        models_list: list[str] = []

        # 1. Модель из настроек бота
        configured_model = ""
        if self.settings and getattr(self.settings, "openrouter_model", ""):
            configured_model = self.settings.openrouter_model.strip()
            if configured_model:
                models_list.append(configured_model)

        # 2. Верифицированные бесплатные Vision-модели
        for vm in VERIFIED_FREE_VISION_MODELS:
            if vm not in models_list:
                models_list.append(vm)

        # 3. Динамическое обнаружение через API OpenRouter
        try:
            discovered = await discover_openrouter_free_vision_models(http_client=self._client, timeout_sec=10.0)
            for m in discovered:
                mid = m.get("id") if isinstance(m, dict) else str(m)
                if mid and mid not in models_list:
                    models_list.append(mid)
        except Exception as err:
            logger.debug(f"Не удалось обновить каталог моделей OpenRouter: {err}")

        # Дополнительные статические кандидаты
        for cand in DEFAULT_FREE_VISION_CANDIDATES:
            if cand not in models_list:
                models_list.append(cand)

        self._cached_models = models_list
        self._last_discovery_time = now
        logger.info(f"Сформирован каскад моделей хроники ({len(self._cached_models)} шт.): {self._cached_models[:4]}")
        return list(self._cached_models)

    async def generate_chronicle_post(
        self,
        events: list[dict[str, Any]],
        trigger_type: str = "hourly_summary",
        host_name: str = "Player",
        session_duration_min: int = 0,
        screenshot_bytes: Optional[bytes] = None,
        mime_type: str = "image/jpeg",
    ) -> tuple[str, str]:
        """Генерирует текст сводки для Telegram по 3-уровневому каскаду.

        Args:
            events: Список событий из events.jsonl.
            trigger_type: Тип триггера.
            host_name: Имя хоста.
            session_duration_min: Длительность сессии в минутах.
            screenshot_bytes: Опциональные байты скриншота (JPEG или PNG).
            mime_type: MIME-тип скриншота ('image/jpeg' или 'image/png').

        Returns:
            Кортеж (текст_поста, использованный_уровень: 'openrouter_vision' | 'pollinations_text' | 'fallback_template').
        """
        # Определение персоны и системного промпта
        persona_style = "reporter"
        if self.settings:
            persona_style = getattr(self.settings, "ai_persona_style", "reporter")
        system_prompt = SYSTEM_PROMPTS.get(persona_style, FALLBACK_SYSTEM_PROMPT)

        # Формирование текстового контекста событий
        telemetry_prompt = format_events_for_llm(
            events=events,
            trigger_type=trigger_type,
            host_name=host_name,
            session_duration_min=session_duration_min,
        )

        api_key = ""
        if self.settings and getattr(self.settings, "openrouter_api_key", ""):
            api_key = self.settings.openrouter_api_key.strip()
        elif self.config and getattr(self.config, "openrouter_api_key", ""):
            api_key = self.config.openrouter_api_key.strip()

        # =====================================================================
        # Уровень 1: OpenRouter Vision API (при наличии API-ключа)
        # =====================================================================
        if api_key:
            logger.info("Запуск генерации хроники через OpenRouter Vision API (Уровень 1)...")
            models_to_try = await self.get_available_vision_models()

            # Кодирование изображения в base64 (если предоставлено)
            image_data_uri = None
            if screenshot_bytes:
                b64_str = base64.b64encode(screenshot_bytes).decode("ascii")
                image_data_uri = f"data:{mime_type};base64,{b64_str}"

            user_content: list[dict[str, Any]] = [{"type": "text", "text": telemetry_prompt}]
            if image_data_uri:
                user_content.append({"type": "image_url", "image_url": {"url": image_data_uri}})

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/factorio-coop-sync",
                "X-Title": "Factorio P2P Cloud Sync",
            }

            for model_id in models_to_try[:6]:  # Пробуем до 6 лучших моделей
                payload = {
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "reasoning": {"max_tokens": 0},
                    "max_tokens": 700,
                    "temperature": 0.7,
                }
                try:
                    t0 = time.monotonic()
                    resp = await self._client.post(
                        OPENROUTER_CHAT_URL,
                        headers=headers,
                        json=payload,
                        timeout=35.0,
                    )
                    latency = int((time.monotonic() - t0) * 1000)

                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            raw_content = choices[0].get("message", {}).get("content", "")
                            if raw_content and len(raw_content.strip()) > 30:
                                cleaned = clean_telegram_html(raw_content)
                                logger.info(
                                    f"Успешная генерация хроники через OpenRouter ({model_id}) за {latency} мс."
                                )
                                return cleaned, "openrouter_vision"
                        logger.warning(f"Модель {model_id} вернула пустой ответ: {data}")
                    elif resp.status_code in (429, 402):
                        logger.warning(
                            f"Модель {model_id} превысила лимит (HTTP {resp.status_code}), переход к следующей..."
                        )
                    elif resp.status_code == 404:
                        logger.warning(f"Модель {model_id} не найдена (404), переход к следующей...")
                    else:
                        logger.warning(f"Ошибка модели {model_id}: HTTP {resp.status_code} - {resp.text[:200]}")
                except Exception as model_err:
                    logger.warning(f"Сбой при обращении к OpenRouter ({model_id}): {model_err}")

        # =====================================================================
        # Уровень 2: Zero-Key Pollinations Text API (только текст телеметрии)
        # =====================================================================
        logger.info("Запуск генерации хроники через Zero-Key Pollinations Text API (Уровень 2)...")
        try:
            # Pollinations на бесплатном тарифе принимает только текст
            poll_payload = {
                "model": "openai",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"{telemetry_prompt}\n\n"
                            f"NOTE: The factory screenshot is being uploaded separately. "
                            f"Please write the witty Russian summary based on the telemetry log above."
                        ),
                    },
                ],
                "max_tokens": 650,
                "temperature": 0.7,
            }
            t0 = time.monotonic()
            poll_resp = await self._client.post(
                POLLINATIONS_CHAT_URL,
                headers={"Content-Type": "application/json"},
                json=poll_payload,
                timeout=30.0,
            )
            latency = int((time.monotonic() - t0) * 1000)
            if poll_resp.status_code == 200:
                poll_data = poll_resp.json()
                choices = poll_data.get("choices", [])
                if choices:
                    raw_text = choices[0].get("message", {}).get("content", "")
                    if raw_text and len(raw_text.strip()) > 30:
                        cleaned = clean_telegram_html(raw_text)
                        logger.info(f"Успешная генерация хроники через Pollinations за {latency} мс.")
                        return cleaned, "pollinations_text"
            else:
                logger.warning(f"Pollinations вернул HTTP {poll_resp.status_code}: {poll_resp.text[:200]}")
        except Exception as poll_err:
            logger.warning(f"Сбой при обращении к Pollinations Text API: {poll_err}")

        # =====================================================================
        # Уровень 3: Локальный детерминированный шаблон (Offline Fallback)
        # =====================================================================
        logger.info("Все нейросетевые сервисы недоступны. Формирование локальной шаблонной сводки (Уровень 3)...")
        fallback_post = format_fallback_chronicle_post(
            events=events,
            trigger_type=trigger_type,
            host_name=host_name,
            session_duration_min=session_duration_min,
        )
        return fallback_post, "fallback_template"
