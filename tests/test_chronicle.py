"""Модульные и интеграционные тесты для микро-мода хроники и демона ChronicleWatcher (Задача 031)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

import httpx
import pytest

from src.client.chronicle_mod import (
    MOD_NAME,
    MOD_VERSION,
    build_mod_info,
    enable_mod_in_mod_list,
    ensure_chronicle_mod_installed,
)
from src.client.chronicle_watcher import ChronicleWatcher
from src.client.factorio_env import FactorioEnvironment
from src.client.host_workers import HostWorkersManager
from src.client.hub_client import HubClient


class TestChronicleModInstallation:
    """Тесты генератора и установщика микро-мода factorio-coop-chronicle."""

    def test_build_mod_info_versions(self) -> None:
        info_2 = build_mod_info("2.0.15")
        assert info_2["name"] == MOD_NAME
        assert info_2["version"] == MOD_VERSION
        assert info_2["factorio_version"] == "2.0"

        info_1 = build_mod_info("1.1.100")
        assert info_1["name"] == MOD_NAME
        assert info_1["factorio_version"] == "1.1"

    def test_ensure_chronicle_mod_installed_creates_folder_and_files(self, tmp_path: Path) -> None:
        mods_dir = tmp_path / "mods"
        mod_dir = ensure_chronicle_mod_installed(mods_dir, factorio_version="2.0")

        assert mod_dir.is_dir()
        assert mod_dir.name == f"{MOD_NAME}_{MOD_VERSION}"

        # Проверка info.json
        info_json = mod_dir / "info.json"
        assert info_json.is_file()
        info_data = json.loads(info_json.read_text(encoding="utf-8"))
        assert info_data["name"] == MOD_NAME
        assert info_data["version"] == MOD_VERSION
        assert info_data["factorio_version"] == "2.0"

        # Проверка control.lua
        control_lua = mod_dir / "control.lua"
        assert control_lua.is_file()
        lua_content = control_lua.read_text(encoding="utf-8")
        assert "defines.events.on_player_died" in lua_content
        assert "defines.events.on_research_finished" in lua_content
        assert "defines.events.on_rocket_launched" in lua_content
        assert "defines.events.on_built_entity" in lua_content
        assert "script.on_nth_tick" in lua_content
        assert "pcall" in lua_content
        assert "daytime = 0.5" in lua_content
        assert "coop_chronicle/events.jsonl" in lua_content

    def test_mod_list_activation(self, tmp_path: Path) -> None:
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir(parents=True)
        mod_list_file = mods_dir / "mod-list.json"

        # Начальный список с другим модом
        initial_content = {
            "mods": [
                {"name": "base", "enabled": True},
                {"name": "space-age", "enabled": True},
            ]
        }
        mod_list_file.write_text(json.dumps(initial_content), encoding="utf-8")

        # Установка мода
        ensure_chronicle_mod_installed(mods_dir)

        # Проверка, что старые моды сохранились, а наш добавлен и включен
        updated_data = json.loads(mod_list_file.read_text(encoding="utf-8"))
        names = {m["name"]: m["enabled"] for m in updated_data["mods"]}
        assert names.get("base") is True
        assert names.get("space-age") is True
        assert names.get(MOD_NAME) is True

    def test_mod_list_created_if_missing(self, tmp_path: Path) -> None:
        mods_dir = tmp_path / "mods"
        mod_list_file = mods_dir / "mod-list.json"
        assert not mod_list_file.exists()

        ensure_chronicle_mod_installed(mods_dir)

        assert mod_list_file.is_file()
        updated_data = json.loads(mod_list_file.read_text(encoding="utf-8"))
        names = {m["name"]: m["enabled"] for m in updated_data["mods"]}
        assert names.get(MOD_NAME) is True

    def test_ensure_chronicle_mod_installed_idempotent(self, tmp_path: Path) -> None:
        mods_dir = tmp_path / "mods"
        dir1 = ensure_chronicle_mod_installed(mods_dir)
        dir2 = ensure_chronicle_mod_installed(mods_dir)
        assert dir1 == dir2
        assert (dir1 / "control.lua").is_file()


class TestHubClientChronicleReport:
    """Тесты метода HubClient.send_chronicle_report."""

    def test_send_chronicle_report_without_screenshot(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/chronicle/report"
            assert request.method == "POST"
            # В multipart/form-data проверяем наличие данных
            body_bytes = request.content
            assert b"events_json" in body_bytes
            assert b"player_died" in body_bytes
            assert b"important_event" in body_bytes
            return httpx.Response(200, json={"success": True, "post_published": True})

        transport = httpx.MockTransport(handler)
        mock_http = httpx.Client(transport=transport, base_url="http://testserver")

        client = HubClient(http_client=mock_http)
        events = [{"type": "player_died", "important": True, "player": "Pikachu"}]
        res = client.send_chronicle_report(
            events=events,
            trigger_type="important_event",
            session_token="test-session-123",
        )
        assert res.get("success") is True
        assert res.get("post_published") is True

    def test_send_chronicle_report_with_screenshot(self, tmp_path: Path) -> None:
        shot_file = tmp_path / "shot_death_1.jpg"
        shot_file.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIFfakejpegdata")

        received_filename: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/chronicle/report"
            content = request.content
            assert b"events_json" in content
            assert b"hourly_summary" in content
            assert b"fakejpegdata" in content
            if b'filename="shot_death_1.jpg"' in content:
                received_filename.append("shot_death_1.jpg")
            return httpx.Response(200, json={"success": True, "screenshot_received": True})

        transport = httpx.MockTransport(handler)
        mock_http = httpx.Client(transport=transport, base_url="http://testserver")

        client = HubClient(http_client=mock_http)
        events = [{"type": "hourly_snapshot", "tick": 216000}]
        res = client.send_chronicle_report(
            events=events,
            trigger_type="hourly_summary",
            screenshot_path=shot_file,
        )
        assert res.get("success") is True
        assert len(received_filename) == 1

    def test_send_chronicle_report_handles_server_error_gracefully(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "Internal AI error"})

        transport = httpx.MockTransport(handler)
        mock_http = httpx.Client(transport=transport, base_url="http://testserver")

        client = HubClient(http_client=mock_http)
        res = client.send_chronicle_report(
            events=[{"type": "dummy"}],
            trigger_type="hourly_summary",
        )
        # Не должно выбрасывать исключений
        assert res.get("success") is False
        assert "error" in res


class TestChronicleWatcher:
    """Тесты фонового демона ChronicleWatcher."""

    def test_reads_new_events_from_events_jsonl(self, tmp_path: Path) -> None:
        script_output_dir = tmp_path / "script-output"
        chronicle_dir = script_output_dir / "coop_chronicle"
        chronicle_dir.mkdir(parents=True)
        events_file = chronicle_dir / "events.jsonl"

        mock_hub = mock.MagicMock()
        mock_hub.send_chronicle_report.return_value = {"success": True}

        watcher = ChronicleWatcher(
            script_output_dir=script_output_dir,
            hub_client=mock_hub,
            poll_interval_sec=0.1,
            important_cooldown_sec=1.0,
            wait_screenshot_sec=0.01,
        )

        # Записываем два события
        events_file.write_text(
            json.dumps({"type": "research_finished", "tech": "automation", "important": False}) + "\n" +
            json.dumps({"type": "stats_snapshot", "players_online": 2, "important": False}) + "\n",
            encoding="utf-8",
        )

        # Вызываем один опрос
        watcher._poll_once()

        with watcher._lock:
            assert len(watcher._pending_events) == 2
            assert watcher._pending_events[0]["type"] == "research_finished"
            assert watcher._pending_events[1]["type"] == "stats_snapshot"

        # Повторный опрос без новых строк не добавляет дубли
        watcher._poll_once()
        with watcher._lock:
            assert len(watcher._pending_events) == 2

    def test_fallback_parsing_from_factorio_log(self, tmp_path: Path) -> None:
        script_output_dir = tmp_path / "script-output"
        script_output_dir.mkdir(parents=True)
        log_file = tmp_path / "factorio-current.log"

        log_file.write_text(
            "2026-10-25 02:00:00 [JOIN] Player1 joined the game\n"
            "2026-10-25 02:01:00 [CHAT] Player1 was killed by biter-spitter\n",
            encoding="utf-8",
        )

        mock_hub = mock.MagicMock()
        # Указываем is_confirmed_getter=lambda: False, чтобы триггеры не отправляли и не очищали буфер
        watcher = ChronicleWatcher(
            script_output_dir=script_output_dir,
            hub_client=mock_hub,
            log_path=log_file,
            poll_interval_sec=0.1,
            wait_screenshot_sec=0.01,
            is_confirmed_getter=lambda: False,
        )

        watcher._poll_once()

        with watcher._lock:
            assert len(watcher._pending_events) == 1
            ev = watcher._pending_events[0]
            assert ev["type"] == "player_died"
            assert ev["player"] == "Player1"
            assert ev["cause"] == "biter-spitter"
            assert ev["important"] is True

    def test_trigger_important_event_and_cooldown(self, tmp_path: Path) -> None:
        script_output_dir = tmp_path / "script-output"
        chronicle_dir = script_output_dir / "coop_chronicle"
        chronicle_dir.mkdir(parents=True)
        events_file = chronicle_dir / "events.jsonl"

        # Создаем скриншот
        shot_path = chronicle_dir / "shot_death_1_100.jpg"
        shot_path.write_bytes(b"fake-image-bytes")

        mock_hub = mock.MagicMock()
        mock_hub.send_chronicle_report.return_value = {"success": True}

        watcher = ChronicleWatcher(
            script_output_dir=script_output_dir,
            hub_client=mock_hub,
            session_token="token-abc",
            poll_interval_sec=0.1,
            important_cooldown_sec=5.0,  # 5 секунд кулдаун
            wait_screenshot_sec=0.01,
        )

        # 1. Записываем важное событие
        event1 = {
            "type": "player_died",
            "important": True,
            "tick": 100,
            "player": "Hero",
            "screenshot": "shot_death_1_100.jpg",
        }
        events_file.write_text(json.dumps(event1) + "\n", encoding="utf-8")

        watcher._poll_once()

        assert mock_hub.send_chronicle_report.call_count == 1
        args, kwargs = mock_hub.send_chronicle_report.call_args
        assert kwargs["trigger_type"] == "important_event"
        assert kwargs["screenshot_path"] == shot_path
        assert kwargs["session_token"] == "token-abc"

        # 2. Сразу записываем второе важное событие во время кулдауна
        event2 = {
            "type": "rocket_launched",
            "important": True,
            "tick": 200,
        }
        with events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event2) + "\n")

        watcher._poll_once()

        # Вызов не должен повториться из-за кулдауна!
        assert mock_hub.send_chronicle_report.call_count == 1

        # Событие остается в очереди
        with watcher._lock:
            assert len(watcher._pending_events) == 1
            assert watcher._pending_events[0]["type"] == "rocket_launched"

    def test_trigger_hourly_summary(self, tmp_path: Path) -> None:
        script_output_dir = tmp_path / "script-output"
        chronicle_dir = script_output_dir / "coop_chronicle"
        chronicle_dir.mkdir(parents=True)
        events_file = chronicle_dir / "events.jsonl"

        mock_hub = mock.MagicMock()
        mock_hub.send_chronicle_report.return_value = {"success": True}

        watcher = ChronicleWatcher(
            script_output_dir=script_output_dir,
            hub_client=mock_hub,
            poll_interval_sec=0.1,
            hourly_interval_sec=60.0,
            wait_screenshot_sec=0.01,
        )
        # Имитируем истечение времени с прошлого часового отчета (более 60 секунд)
        watcher._last_hourly_sent_time = time.time() - 100.0

        events_file.write_text(json.dumps({"type": "research_finished", "tech": "optics"}) + "\n", encoding="utf-8")

        watcher._poll_once()

        assert mock_hub.send_chronicle_report.call_count == 1
        _, kwargs = mock_hub.send_chronicle_report.call_args
        assert kwargs["trigger_type"] == "hourly_summary"

    def test_flush_on_exit(self, tmp_path: Path) -> None:
        script_output_dir = tmp_path / "script-output"
        chronicle_dir = script_output_dir / "coop_chronicle"
        chronicle_dir.mkdir(parents=True)
        events_file = chronicle_dir / "events.jsonl"

        mock_hub = mock.MagicMock()
        mock_hub.send_chronicle_report.return_value = {"success": True, "flushed": True}

        watcher = ChronicleWatcher(
            script_output_dir=script_output_dir,
            hub_client=mock_hub,
            poll_interval_sec=0.1,
            wait_screenshot_sec=0.01,
        )

        # Без данных сброс возвращает flushed_empty
        empty_res = watcher.flush_on_exit()
        assert empty_res.get("action") == "flushed_empty"
        assert mock_hub.send_chronicle_report.call_count == 0

        # Добавляем событие
        events_file.write_text(json.dumps({"type": "milestone_built", "entity": "nuclear-reactor"}) + "\n", encoding="utf-8")

        res = watcher.flush_on_exit()
        assert res.get("flushed") is True
        assert mock_hub.send_chronicle_report.call_count == 1
        _, kwargs = mock_hub.send_chronicle_report.call_args
        assert kwargs["trigger_type"] == "session_summary"

    def test_start_and_stop_lifecycle(self, tmp_path: Path) -> None:
        script_output_dir = tmp_path / "script-output"
        script_output_dir.mkdir(parents=True)
        mock_hub = mock.MagicMock()

        watcher = ChronicleWatcher(
            script_output_dir=script_output_dir,
            hub_client=mock_hub,
            poll_interval_sec=0.05,
        )

        assert not watcher.is_running
        watcher.start()
        assert watcher.is_running
        time.sleep(0.1)
        watcher.stop()
        assert not watcher.is_running


class TestChronicleIntegration:
    """Интеграционные тесты с FactorioEnvironment и HostWorkersManager."""

    def test_factorio_env_script_output_dir(self, tmp_path: Path) -> None:
        env = FactorioEnvironment(
            custom_game_root=tmp_path / "game",
            custom_user_data_dir=tmp_path / "userdata",
        )
        assert env.script_output_dir == tmp_path / "userdata" / "script-output"
        assert not env.script_output_dir.exists()

        env.ensure_dirs()
        assert env.script_output_dir.is_dir()

    def test_host_workers_manager_chronicle_lifecycle(self, tmp_path: Path) -> None:
        log_path = tmp_path / "factorio-current.log"
        log_path.write_text("Starting factorio...\n", encoding="utf-8")
        saves_dir = tmp_path / "saves"
        saves_dir.mkdir(parents=True)

        mock_hub = mock.MagicMock()
        mock_hub.send_chronicle_report.return_value = {"success": True}

        manager = HostWorkersManager(
            log_path=log_path,
            saves_dir=saves_dir,
            hub_client=mock_hub,
            session_token="test-token",
            detector_timeout_sec=5.0,
            detector_check_interval=0.1,
            heartbeat_interval_sec=5.0,
            autosave_poll_interval_sec=5.0,
            script_output_dir=tmp_path / "script-output",
        )

        assert isinstance(manager.chronicle_watcher, ChronicleWatcher)
        assert not manager.chronicle_watcher.is_running

        manager.start()
        assert manager.chronicle_watcher.is_running

        manager.stop()
        assert not manager.chronicle_watcher.is_running
