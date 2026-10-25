"""Модуль и CLI-скрипт живой диагностики и верификации бесплатных мультимодальных нейросетей (Vision AI).

Обеспечивает:
1. Генерацию образцового PNG-скриншота базы без внешних зависимостей (чистый zlib/struct).
2. Формирование реалистичной телеметрии событий Factorio (смерти, технологии, постройка реактора).
3. Динамический опрос живого каталога OpenRouter (GET https://openrouter.ai/api/v1/models) без API-ключа
   для обнаружения всех актуальных бесплатных моделей с поддержкой изображений.
4. 3-ступенчатую проверку каждой модели:
   - Проба 1: Текстовый запрос (доступность API).
   - Проба 2: Запрос с base64-изображением (поддержка формата image_url).
   - Проба 3: Полный боевой запрос (системный промпт + лог Factorio + скриншот -> русский пост).
5. Проверку резервного открытого Vision API без ключа (Zero-Key Pollinations OpenAI-совместимый эндпоинт).
6. Автономный консольный интерфейс (CLI) для проверки моделей перед интеграцией в сервис хроники.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("server.ai_probe")

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
POLLINATIONS_CHAT_URL = "https://text.pollinations.ai/openai/chat/completions"

DEFAULT_FREE_VISION_CANDIDATES: list[str] = [
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "qwen/qwen2.5-vl-72b-instruct:free",
    "qwen/qwen2.5-vl-32b-instruct:free",
    "moonshotai/kimi-vl-a3b-thinking:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "openrouter/free",
]

DEFAULT_SYSTEM_PROMPT = (
    "You are an embedded sci-fi war correspondent on planet Nauvis in Factorio. "
    "Analyze the attached game telemetry logs and the in-game screenshot. "
    "Write a vivid, witty summary post IN RUSSIAN (400-800 characters) using clean Telegram HTML tags "
    "(<b>, <i>, <code>). Highlight key events like player deaths, tech breakthroughs, and defense status."
)


def create_sample_test_png(width: int = 64, height: int = 64) -> bytes:
    """Генерирует валидный цветной PNG-файл без внешних графических библиотек (RFC 2083).

    Создает двухцветный индустриальный узор (оранжевый и темно-серый).

    Args:
        width: Ширина изображения в пикселях.
        height: Высота изображения в пикселях.

    Returns:
        Байты валидного PNG-файла.
    """
    width = max(1, int(width))
    height = max(1, int(height))

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        return length + chunk_type + data + crc

    header = b"\x89PNG\r\n\x1a\n"
    # IHDR: width (4B), height (4B), bit_depth=8 (1B), color_type=2 (RGB, 1B), compression=0, filter=0, interlace=0
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)

    raw_lines: list[bytes] = []
    for y in range(height):
        line = bytearray([0])  # filter byte: 0 (None)
        for x in range(width):
            # Шахматный индустриальный узор 8x8 пикселей
            if (x // 8 + y // 8) % 2 == 0:
                line.extend([235, 120, 20])  # Factorio Оранжевый
            else:
                line.extend([40, 45, 50])    # Металлический темно-серый
        raw_lines.append(bytes(line))

    compressed = zlib.compress(b"".join(raw_lines), level=6)
    idat = _chunk(b"IDAT", compressed)
    iend = _chunk(b"IEND", b"")
    return header + ihdr + idat + iend


def create_sample_factorio_telemetry() -> list[dict[str, Any]]:
    """Формирует реалистичный набор телеметрии событий Factorio для тестирования."""
    return [
        {
            "type": "player_died",
            "important": True,
            "tick": 14200,
            "player": "graypikachu",
            "cause": "big-biter",
            "position": {"x": 124.5, "y": -58.2},
            "screenshot": "shot_death_1_14200.jpg",
        },
        {
            "type": "research_finished",
            "important": False,
            "tick": 48000,
            "tech": "nuclear-power",
            "level": 1,
        },
        {
            "type": "milestone_built",
            "important": True,
            "tick": 92000,
            "player": "graypikachu",
            "entity": "nuclear-reactor",
            "screenshot": "shot_milestone_nuclear-reactor_92000.jpg",
        },
        {
            "type": "hourly_snapshot",
            "important": False,
            "tick": 216000,
            "evolution_factor": 0.542,
            "players_online": 2,
            "screenshot": "shot_hourly_216000.jpg",
        },
    ]


def format_telemetry_prompt_text(
    events: list[dict[str, Any]],
    host_name: str = "graypikachu",
    session_duration_min: int = 60,
) -> str:
    """Преобразует список событий сессии в текстовое описание для пользователя в промпте."""
    lines: list[str] = [
        f"Session summary for Nauvis factory:",
        f"- Host engineer: {host_name}",
        f"- Active session playtime: ~{session_duration_min} minutes",
        "- Key timeline events:",
    ]

    for ev in events:
        ev_type = ev.get("type", "unknown")
        tick = ev.get("tick", 0)
        if ev_type == "player_died":
            lines.append(
                f"  * [tick {tick}] Engineer '{ev.get('player')}' was killed by '{ev.get('cause')}' near pos {ev.get('position')}."
            )
        elif ev_type == "research_finished":
            lines.append(
                f"  * [tick {tick}] Research completed: '{ev.get('tech')}' (level {ev.get('level', 1)})."
            )
        elif ev_type == "milestone_built":
            lines.append(
                f"  * [tick {tick}] Critical milestone constructed: '{ev.get('entity')}' by '{ev.get('player')}'."
            )
        elif ev_type == "rocket_launched":
            lines.append(f"  * [tick {tick}] Rocket successfully launched into space.")
        elif ev_type == "hourly_snapshot":
            lines.append(
                f"  * [tick {tick}] Hourly status: enemy evolution {ev.get('evolution_factor')}, online: {ev.get('players_online')}."
            )
        else:
            lines.append(f"  * [tick {tick}] Event: {ev}")

    lines.append(
        "\nBased on this game log and the visual scene in the attached screenshot, "
        "compose the Russian Telegram post now:"
    )
    return "\n".join(lines)


async def discover_openrouter_free_vision_models(
    http_client: Optional[httpx.AsyncClient] = None,
    timeout_sec: float = 15.0,
) -> list[dict[str, Any]]:
    """Динамически запрашивает каталог моделей OpenRouter и находит бесплатные Vision-модели.

    Запрос выполняется без API-ключа. Модель считается подходящей, если:
    1. Цена prompt равна 0 (или id оканчивается на ':free').
    2. В input_modalities или архитектуре присутствует 'image'.

    Args:
        http_client: Экземпляр httpx.AsyncClient (опционально).
        timeout_sec: Таймаут запроса в секундах.

    Returns:
        Список словарей с описанием найденных моделей.
    """
    owns_client = False
    client = http_client
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_sec)
        owns_client = True

    try:
        resp = await client.get(
            OPENROUTER_MODELS_URL,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
        models_data = payload.get("data", [])
        if not isinstance(models_data, list):
            logger.warning("Неожиданный формат каталога OpenRouter (data не является списком).")
            return []

        free_vision_models: list[dict[str, Any]] = []

        for m in models_data:
            if not isinstance(m, dict):
                continue
            model_id = str(m.get("id", ""))
            pricing = m.get("pricing") or {}
            architecture = m.get("architecture") or {}

            # Проверка бесплатности
            is_free = False
            if model_id.endswith(":free"):
                is_free = True
            else:
                prompt_price = pricing.get("prompt")
                try:
                    if prompt_price is not None and float(prompt_price) == 0.0:
                        is_free = True
                except (ValueError, TypeError):
                    pass

            if not is_free:
                continue

            # Проверка поддержки изображений
            input_modalities = architecture.get("input_modalities") or []
            modality_str = str(architecture.get("modality", "")).lower()

            has_image = ("image" in input_modalities) or ("image" in modality_str)
            # Известные vision-модели
            if not has_image and any(tag in model_id.lower() for tag in ("-vl-", "vision", "gemini", "gemma-3")):
                has_image = True

            if has_image:
                free_vision_models.append(
                    {
                        "id": model_id,
                        "name": m.get("name", model_id),
                        "context_length": m.get("context_length", 0),
                        "description": m.get("description", ""),
                        "pricing": pricing,
                    }
                )

        logger.info(f"Обнаружено {len(free_vision_models)} бесплатных Vision-моделей на OpenRouter.")
        return free_vision_models
    except Exception as err:
        logger.warning(f"Ошибка при запросе каталога OpenRouter ({OPENROUTER_MODELS_URL}): {err}")
        return []
    finally:
        if owns_client:
            await client.aclose()


async def probe_vision_endpoint(
    endpoint_url: str,
    model: str,
    api_key: str = "",
    http_client: Optional[httpx.AsyncClient] = None,
    timeout_sec: float = 35.0,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> dict[str, Any]:
    """Выполняет 3-ступенчатую проверку эндпоинта модели (Текст -> Картинка -> Полный боевой запрос).

    Args:
        endpoint_url: URL эндпоинта (/chat/completions).
        model: Идентификатор модели.
        api_key: Ключ API (пустой для Zero-Key Pollinations).
        http_client: Опциональный клиент httpx.AsyncClient.
        timeout_sec: Таймаут на каждую пробу в секундах.
        system_prompt: Системный промпт для боевой пробы.

    Returns:
        Словарь с результатами каждой пробы, задержкой и итоговым статусом.
    """
    owns_client = False
    client = http_client
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_sec)
        owns_client = True

    headers = {
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/factorio-coop-sync",
        "X-Title": "Factorio P2P Cloud Sync",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    result: dict[str, Any] = {
        "model": model,
        "endpoint": endpoint_url,
        "probe1_text": {"status": "NOT_RUN"},
        "probe2_image": {"status": "NOT_RUN"},
        "probe3_full": {"status": "NOT_RUN"},
        "overall_status": "FAIL",
    }

    sample_png_bytes = create_sample_test_png(64, 64)
    png_b64 = base64.b64encode(sample_png_bytes).decode("ascii")
    image_data_uri = f"data:image/png;base64,{png_b64}"

    try:
        # =====================================================================
        # Проба 1: Текстовый запрос
        # =====================================================================
        p1_payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": "Ping test. Are you online? Answer with 'OK' and one Russian sentence."}
            ],
            "max_tokens": 80,
            "temperature": 0.3,
        }
        t0 = time.monotonic()
        try:
            r1 = await client.post(endpoint_url, headers=headers, json=p1_payload)
            latency1 = int((time.monotonic() - t0) * 1000)
            if r1.status_code == 200:
                data1 = r1.json()
                content1 = data1.get("choices", [{}])[0].get("message", {}).get("content", "")
                result["probe1_text"] = {
                    "status": "PASS",
                    "latency_ms": latency1,
                    "content": content1.strip()[:150],
                }
            elif r1.status_code == 429:
                result["probe1_text"] = {"status": "RATE_LIMITED_429", "latency_ms": latency1, "error": r1.text[:200]}
            elif r1.status_code == 404:
                result["probe1_text"] = {"status": "MODEL_NOT_FOUND_404", "latency_ms": latency1, "error": r1.text[:200]}
            else:
                result["probe1_text"] = {"status": f"HTTP_{r1.status_code}", "latency_ms": latency1, "error": r1.text[:200]}
        except Exception as e1:
            result["probe1_text"] = {"status": "NETWORK_ERROR", "error": str(e1)}

        # =====================================================================
        # Проба 2: Запрос с картинкой (image_url)
        # =====================================================================
        p2_payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe the colors or shapes of this test icon in one Russian sentence:"},
                        {"type": "image_url", "image_url": {"url": image_data_uri}},
                    ],
                }
            ],
            "max_tokens": 120,
            "temperature": 0.4,
        }
        t0 = time.monotonic()
        try:
            r2 = await client.post(endpoint_url, headers=headers, json=p2_payload)
            latency2 = int((time.monotonic() - t0) * 1000)
            if r2.status_code == 200:
                data2 = r2.json()
                content2 = data2.get("choices", [{}])[0].get("message", {}).get("content", "")
                result["probe2_image"] = {
                    "status": "PASS",
                    "latency_ms": latency2,
                    "content": content2.strip()[:150],
                }
            elif r2.status_code == 429:
                result["probe2_image"] = {"status": "RATE_LIMITED_429", "latency_ms": latency2, "error": r2.text[:200]}
            else:
                result["probe2_image"] = {"status": f"HTTP_{r2.status_code}", "latency_ms": latency2, "error": r2.text[:200]}
        except Exception as e2:
            result["probe2_image"] = {"status": "NETWORK_ERROR", "error": str(e2)}

        # =====================================================================
        # Проба 3: Полный боевой запрос (Английский промпт + логи Factorio + скриншот -> русский пост)
        # =====================================================================
        telemetry = create_sample_factorio_telemetry()
        prompt_text = format_telemetry_prompt_text(telemetry)

        p3_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": image_data_uri}},
                    ],
                },
            ],
            "max_tokens": 500,
            "temperature": 0.7,
        }
        t0 = time.monotonic()
        try:
            r3 = await client.post(endpoint_url, headers=headers, json=p3_payload)
            latency3 = int((time.monotonic() - t0) * 1000)
            if r3.status_code == 200:
                data3 = r3.json()
                content3 = data3.get("choices", [{}])[0].get("message", {}).get("content", "")
                has_cyrillic = any("\u0400" <= ch <= "\u04ff" for ch in content3)
                result["probe3_full"] = {
                    "status": "PASS" if has_cyrillic else "PASS_NO_CYRILLIC",
                    "latency_ms": latency3,
                    "content": content3.strip(),
                    "has_cyrillic": has_cyrillic,
                }
            elif r3.status_code == 429:
                result["probe3_full"] = {"status": "RATE_LIMITED_429", "latency_ms": latency3, "error": r3.text[:200]}
            else:
                result["probe3_full"] = {"status": f"HTTP_{r3.status_code}", "latency_ms": latency3, "error": r3.text[:200]}
        except Exception as e3:
            result["probe3_full"] = {"status": "NETWORK_ERROR", "error": str(e3)}

        # Оценка общего статуса
        p1_ok = result["probe1_text"].get("status") == "PASS"
        p2_ok = result["probe2_image"].get("status") == "PASS"
        p3_ok = result["probe3_full"].get("status") in ("PASS", "PASS_NO_CYRILLIC")

        if p1_ok and p2_ok and p3_ok:
            result["overall_status"] = "PASS"
        elif p2_ok or p3_ok:
            result["overall_status"] = "PARTIAL"
        else:
            result["overall_status"] = "FAIL"

    finally:
        if owns_client:
            await client.aclose()

    return result


async def run_full_probe(
    openrouter_api_key: str = "",
    save_report_path: Optional[Path | str] = None,
    openrouter_only: bool = False,
    pollinations_only: bool = False,
    custom_model: Optional[str] = None,
) -> dict[str, Any]:
    """Выполняет полный прогон диагностики моделей и возвращает структурированный отчет."""
    report: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "openrouter_catalog_count": 0,
        "openrouter_free_vision_models": [],
        "probed_results": [],
        "best_working_models": [],
        "zero_key_fallback_working": False,
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        # Шаг 1: Опрос каталога OpenRouter
        if not pollinations_only:
            logger.info("Запрос актуального каталога моделей OpenRouter...")
            catalog = await discover_openrouter_free_vision_models(http_client=client)
            report["openrouter_catalog_count"] = len(catalog)
            report["openrouter_free_vision_models"] = [m["id"] for m in catalog]

        # Шаг 2: Формирование списка кандидатов для проверки
        models_to_probe: list[Tuple[str, str, str]] = []  # (endpoint_url, model, api_key)

        if custom_model:
            ep = POLLINATIONS_CHAT_URL if "pollinations" in custom_model else OPENROUTER_CHAT_URL
            key = "" if "pollinations" in custom_model else openrouter_api_key
            models_to_probe.append((ep, custom_model, key))
        else:
            # 2.1 Контур 2: Открытый Zero-Key Pollinations API
            if not openrouter_only:
                models_to_probe.append((POLLINATIONS_CHAT_URL, "openai", ""))

            # 2.2 Контур 1: Модели OpenRouter
            if not pollinations_only and openrouter_api_key:
                # Берем пересечение найденных в каталоге с приоритетным списком
                discovered_ids = set(report.get("openrouter_free_vision_models", []))
                prioritized = [m for m in DEFAULT_FREE_VISION_CANDIDATES if m in discovered_ids]
                # Добавляем остальные найденные
                for mid in report.get("openrouter_free_vision_models", []):
                    if mid not in prioritized and len(prioritized) < 6:
                        prioritized.append(mid)

                # Если каталог был недоступен, берем дефолтный список
                if not prioritized:
                    prioritized = DEFAULT_FREE_VISION_CANDIDATES[:4]

                for mid in prioritized:
                    models_to_probe.append((OPENROUTER_CHAT_URL, mid, openrouter_api_key))

        # Шаг 3: Запуск проб
        for endpoint, model, key in models_to_probe:
            logger.info(f"Запуск 3-ступенчатой пробы для модели '{model}' на {endpoint}...")
            res = await probe_vision_endpoint(
                endpoint_url=endpoint,
                model=model,
                api_key=key,
                http_client=client,
            )
            report["probed_results"].append(res)
            if res.get("overall_status") == "PASS":
                report["best_working_models"].append(model)
                if endpoint == POLLINATIONS_CHAT_URL:
                    report["zero_key_fallback_working"] = True

    # Сохранение отчета в JSON
    if save_report_path:
        p = Path(save_report_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Диагностический отчет сохранен в {p}")

    return report


def print_cli_report(report: dict[str, Any]) -> None:
    """Выводит красиво отформатированный текстовый отчет в консоль."""
    print("\n" + "=" * 76)
    print("  FACTORIO LIVE VISION AI PROBE REPORT")
    print(f"  Time: {report.get('timestamp')}")
    print(f"  OpenRouter free vision models in catalog: {report.get('openrouter_catalog_count')}")
    print("=" * 76)

    discovered = report.get("openrouter_free_vision_models", [])
    if discovered:
        print("\n[DISCOVERED FREE VISION MODELS IN OPENROUTER CATALOG]")
        for i, mid in enumerate(discovered, 1):
            print(f"  {i}. {mid}")

    print("\n[PROBE RESULTS]")
    print(f"{'MODEL':<42} | {'TEXT':<7} | {'IMAGE':<7} | {'FULL_LOGS':<9} | {'OVERALL':<7}")
    print("-" * 76)

    for item in report.get("probed_results", []):
        m = item.get("model", "")[:40]
        s1 = item.get("probe1_text", {}).get("status", "N/A")[:7]
        s2 = item.get("probe2_image", {}).get("status", "N/A")[:7]
        s3 = item.get("probe3_full", {}).get("status", "N/A")[:9]
        ov = item.get("overall_status", "N/A")[:7]
        print(f"{m:<42} | {s1:<7} | {s2:<7} | {s3:<9} | {ov:<7}")

        # Если есть боевой текст сводки, покажем фрагмент
        p3_content = item.get("probe3_full", {}).get("content")
        if p3_content:
            preview = p3_content.strip().replace("\n", " ")[:200]
            print(f"   -> AI Russian post preview: \"{preview}...\"")

    print("\n" + "=" * 76)
    working = report.get("best_working_models", [])
    if working:
        print(f"STATUS: SUCCESS. Working Vision Models found: {', '.join(working)}")
    else:
        print("STATUS: WARNING. No fully working vision models responded in this session.")
    print("=" * 76 + "\n")


def main() -> int:
    """Точка входа CLI `python -m src.server.ai_model_probe`."""
    parser = argparse.ArgumentParser(
        description="Factorio Live Vision AI Models Probe & Verification Tool",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("FACTORIO_OPENROUTER_API_KEY", ""),
        help="OpenRouter API Key (fallback: FACTORIO_OPENROUTER_API_KEY env var)",
    )
    parser.add_argument(
        "--save-report",
        type=str,
        default=None,
        help="Путь для сохранения JSON-отчета (например, .memory_bank/tests/ai_probe_report.json)",
    )
    parser.add_argument(
        "--openrouter-only",
        action="store_true",
        help="Проверять только модели OpenRouter",
    )
    parser.add_argument(
        "--pollinations-only",
        action="store_true",
        help="Проверять только открытый Zero-Key Pollinations API",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Проверить конкретную одиночную модель",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

    report = asyncio.run(
        run_full_probe(
            openrouter_api_key=args.api_key,
            save_report_path=args.save_report,
            openrouter_only=args.openrouter_only,
            pollinations_only=args.pollinations_only,
            custom_model=args.model,
        )
    )

    print_cli_report(report)

    # Успех, если хотя бы один эндпоинт или каталог ответил
    has_catalog = report.get("openrouter_catalog_count", 0) > 0
    has_working_model = len(report.get("best_working_models", [])) > 0
    return 0 if (has_catalog or has_working_model or args.pollinations_only) else 1


if __name__ == "__main__":
    sys.exit(main())
