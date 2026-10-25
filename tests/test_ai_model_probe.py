"""Модульные тесты для модуля диагностики и верификации Vision AI моделей (Задача 032A)."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from unittest import mock

import httpx
import pytest

from src.server.ai_model_probe import (
    DEFAULT_FREE_VISION_CANDIDATES,
    OPENROUTER_CHAT_URL,
    OPENROUTER_MODELS_URL,
    POLLINATIONS_CHAT_URL,
    create_sample_factorio_telemetry,
    create_sample_test_png,
    discover_openrouter_free_vision_models,
    format_telemetry_prompt_text,
    main,
    probe_vision_endpoint,
    run_full_probe,
)


class TestSampleDataGenerators:
    """Тесты генераторов образцовых данных (PNG и телеметрии)."""

    def test_create_sample_test_png_valid_format(self) -> None:
        png_bytes = create_sample_test_png(64, 64)
        assert isinstance(png_bytes, bytes)
        assert len(png_bytes) > 60

        # Сигнатура PNG (8 байт: 0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A)
        assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")

        # Чтение IHDR чанка
        ihdr_len = struct.unpack(">I", png_bytes[8:12])[0]
        assert ihdr_len == 13
        chunk_type = png_bytes[12:16]
        assert chunk_type == b"IHDR"

        width, height, bit_depth, color_type = struct.unpack(">IIBB", png_bytes[16:26])
        assert width == 64
        assert height == 64
        assert bit_depth == 8
        assert color_type == 2  # RGB

        # Проверка IEND чанка в конце
        assert png_bytes.endswith(b"IEND\xaeB`\x82")

    def test_create_sample_test_png_custom_dimensions(self) -> None:
        png_bytes = create_sample_test_png(32, 16)
        width, height = struct.unpack(">II", png_bytes[16:24])
        assert width == 32
        assert height == 16

    def test_create_sample_factorio_telemetry(self) -> None:
        telemetry = create_sample_factorio_telemetry()
        assert isinstance(telemetry, list)
        assert len(telemetry) >= 4

        types = [e.get("type") for e in telemetry]
        assert "player_died" in types
        assert "research_finished" in types
        assert "milestone_built" in types
        assert "hourly_snapshot" in types

    def test_format_telemetry_prompt_text(self) -> None:
        telemetry = create_sample_factorio_telemetry()
        prompt_text = format_telemetry_prompt_text(telemetry, host_name="test_engineer", session_duration_min=45)
        assert "test_engineer" in prompt_text
        assert "45 minutes" in prompt_text
        assert "graypikachu" in prompt_text
        assert "big-biter" in prompt_text
        assert "nuclear-reactor" in prompt_text


class TestOpenRouterCatalogDiscovery:
    """Тесты функции discover_openrouter_free_vision_models."""

    @pytest.mark.anyio
    async def test_discover_models_filters_free_and_vision_correctly(self) -> None:
        mock_catalog_payload = {
            "data": [
                {
                    "id": "google/gemma-3-27b-it:free",
                    "name": "Google Gemma 3 27B Free",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"modality": "text+image->text", "input_modalities": ["text", "image"]},
                },
                {
                    # Платная модель -> должна быть отфильтрована
                    "id": "anthropic/claude-3.5-sonnet",
                    "name": "Claude 3.5 Sonnet",
                    "pricing": {"prompt": "0.003", "completion": "0.015"},
                    "architecture": {"modality": "text+image->text", "input_modalities": ["text", "image"]},
                },
                {
                    # Бесплатная, но только текст -> должна быть отфильтрована
                    "id": "meta-llama/llama-3-8b-instruct:free",
                    "name": "Llama 3 8B Instruct Free",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"modality": "text->text", "input_modalities": ["text"]},
                },
                {
                    # Бесплатная по цене 0.0 с vision
                    "id": "qwen/qwen2.5-vl-72b-instruct:free",
                    "name": "Qwen 2.5 VL 72B Free",
                    "pricing": {"prompt": 0.0, "completion": 0.0},
                    "architecture": {"modality": "text+image->text", "input_modalities": ["text", "image"]},
                },
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(OPENROUTER_MODELS_URL)
            return httpx.Response(200, json=mock_catalog_payload)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            discovered = await discover_openrouter_free_vision_models(http_client=client)

        discovered_ids = [m["id"] for m in discovered]
        assert "google/gemma-3-27b-it:free" in discovered_ids
        assert "qwen/qwen2.5-vl-72b-instruct:free" in discovered_ids
        assert "anthropic/claude-3.5-sonnet" not in discovered_ids
        assert "meta-llama/llama-3-8b-instruct:free" not in discovered_ids

    @pytest.mark.anyio
    async def test_discover_models_handles_error_gracefully(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            discovered = await discover_openrouter_free_vision_models(http_client=client)

        assert discovered == []


class TestProbeVisionEndpoint:
    """Тесты 3-ступенчатого верификатора probe_vision_endpoint."""

    @pytest.mark.anyio
    async def test_probe_vision_endpoint_all_pass(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            messages = body.get("messages", [])
            last_msg = messages[-1].get("content")

            # Ответ на Пробу 1 (текст)
            if isinstance(last_msg, str) and "Ping test" in last_msg:
                return httpx.Response(200, json={"choices": [{"message": {"content": "OK. Система работает штатно."}}]})

            # Ответ на Пробу 2 (картинка)
            if isinstance(last_msg, list) and any(item.get("type") == "image_url" for item in last_msg) and len(messages) == 1:
                return httpx.Response(200, json={"choices": [{"message": {"content": "Изображение оранжево-серого квадрата."}}]})

            # Ответ на Пробу 3 (боевой запрос со сводкой)
            if len(messages) == 2 and messages[0].get("role") == "system":
                russian_post = (
                    "<b>Фронтовая сводка с Наувиса</b>\n\n"
                    "Инженер <code>graypikachu</code> попал в окружение крупных кусак. "
                    "Несмотря на потери, введен в строй <b>ядерный реактор</b>!"
                )
                return httpx.Response(200, json={"choices": [{"message": {"content": russian_post}}]})

            return httpx.Response(400, json={"error": "Unexpected payload"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await probe_vision_endpoint(
                endpoint_url="http://test-vision/v1/chat/completions",
                model="test-vision-model",
                api_key="test-key",
                http_client=client,
            )

        assert res["overall_status"] == "PASS"
        assert res["probe1_text"]["status"] == "PASS"
        assert res["probe2_image"]["status"] == "PASS"
        assert res["probe3_full"]["status"] == "PASS"
        assert res["probe3_full"]["has_cyrillic"] is True
        assert "ядерный реактор" in res["probe3_full"]["content"]

    @pytest.mark.anyio
    async def test_probe_vision_endpoint_rate_limited(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await probe_vision_endpoint(
                endpoint_url="http://test-vision/v1/chat/completions",
                model="test-vision-model",
                api_key="test-key",
                http_client=client,
            )

        assert res["overall_status"] == "FAIL"
        assert res["probe1_text"]["status"] == "RATE_LIMITED_429"

    @pytest.mark.anyio
    async def test_probe_vision_endpoint_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await probe_vision_endpoint(
                endpoint_url="http://offline-endpoint/v1/chat/completions",
                model="test-vision-model",
                http_client=client,
            )

        assert res["overall_status"] == "FAIL"
        assert res["probe1_text"]["status"] == "NETWORK_ERROR"


class TestFullProbeRunnerAndCli:
    """Тесты сквозного выполнения run_full_probe и CLI."""

    @pytest.mark.anyio
    async def test_run_full_probe_generates_report(self, tmp_path: Path) -> None:
        report_file = tmp_path / "report.json"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(OPENROUTER_MODELS_URL):
                return httpx.Response(200, json={"data": [{"id": "google/gemma-3-27b-it:free", "pricing": {"prompt": "0"}, "architecture": {"input_modalities": ["image"]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Сводка готова."}}]})

        transport = httpx.MockTransport(handler)

        with mock.patch("src.server.ai_model_probe.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            report = await run_full_probe(
                openrouter_api_key="mock-key",
                save_report_path=report_file,
                pollinations_only=False,
            )

        assert report["openrouter_catalog_count"] == 1
        assert len(report["probed_results"]) > 0
        assert report_file.is_file()
        saved_data = json.loads(report_file.read_text(encoding="utf-8"))
        assert "timestamp" in saved_data
        assert "best_working_models" in saved_data

    def test_cli_main_mock(self, tmp_path: Path) -> None:
        dummy_report = {
            "timestamp": "2026-10-25 00:00:00 UTC",
            "openrouter_catalog_count": 3,
            "openrouter_free_vision_models": ["google/gemma-3-27b-it:free"],
            "probed_results": [
                {
                    "model": "openai",
                    "probe1_text": {"status": "PASS"},
                    "probe2_image": {"status": "PASS"},
                    "probe3_full": {"status": "PASS", "content": "Тестовая русская сводка"},
                    "overall_status": "PASS",
                }
            ],
            "best_working_models": ["openai"],
            "zero_key_fallback_working": True,
        }

        with mock.patch("src.server.ai_model_probe.run_full_probe", return_value=dummy_report):
            with mock.patch("sys.argv", ["ai_model_probe", "--pollinations-only"]):
                code = main()
                assert code == 0
