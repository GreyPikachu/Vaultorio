"""Модуль генерации и автоустановки микро-мода Factorio (factorio-coop-chronicle).

Создает легковесный автономный Lua-мод для Factorio 2.0 / 1.1, который:
1. Перехватывает события гибели игроков (defines.events.on_player_died).
2. Регистрирует завершение исследований (defines.events.on_research_finished).
3. Фиксирует запуск ракет (defines.events.on_rocket_launched).
4. Отслеживает постройку знаковых объектов (реакторы, шахты ракет, паукотроны).
5. Раз в час делает обзорный снимок базы с дневным освещением (daytime = 0.5).
6. Записывает события в script-output/coop_chronicle/events.jsonl.
7. Активирует мод в mod-list.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("factorio_client.chronicle_mod")

MOD_NAME = "factorio-coop-chronicle"
MOD_VERSION = "1.0.0"
MOD_FOLDER_NAME = f"{MOD_NAME}_{MOD_VERSION}"

CONTROL_LUA_CONTENT = """-- factorio-coop-chronicle / control.lua
-- Автономный микро-мод для фиксации событий и съемки скриншотов хроники базы.

local MILESTONE_ENTITIES = {
    ["rocket-silo"] = true,
    ["nuclear-reactor"] = true,
    ["artillery-turret"] = true,
    ["spidertron"] = true,
    ["cargo-landing-pad"] = true
}

local function escape_json(str)
    if not str then return "" end
    str = tostring(str)
    str = str:gsub('\\\\', '\\\\\\\\')
    str = str:gsub('"', '\\\\"')
    str = str:gsub('\\n', '\\\\n')
    str = str:gsub('\\r', '\\\\r')
    str = str:gsub('\\t', '\\\\t')
    return str
end

local function write_chronicle_line(line)
    pcall(function()
        if helpers and helpers.write_file then
            helpers.write_file("coop_chronicle/events.jsonl", line .. "\\n", true, 0)
        elseif game and game.write_file then
            game.write_file("coop_chronicle/events.jsonl", line .. "\\n", true, 0)
        end
    end)
end

-- 1. Гибель игрока
script.on_event(defines.events.on_player_died, function(event)
    pcall(function()
        local player = game.get_player(event.player_index)
        if not player then return end
        local cause_name = "unknown"
        if event.cause and event.cause.valid then
            cause_name = event.cause.name
        end
        local tick = event.tick or game.tick
        local pos = player.position or {x = 0, y = 0}
        local shot_name = string.format("shot_death_%d_%d.jpg", player.index, tick)
        local shot_rel = "coop_chronicle/" .. shot_name

        pcall(function()
            game.take_screenshot{
                player = player,
                position = pos,
                resolution = {x = 1280, y = 720},
                zoom = 0.75,
                path = shot_rel,
                show_gui = false,
                daytime = 0.5
            }
        end)

        local line = string.format('{"type":"player_died","important":true,"tick":%d,"player":"%s","cause":"%s","position":{"x":%.1f,"y":%.1f},"screenshot":"%s"}',
            tick, escape_json(player.name), escape_json(cause_name), pos.x or 0, pos.y or 0, shot_name)
        write_chronicle_line(line)
    end)
end)

-- 2. Изучение технологий
script.on_event(defines.events.on_research_finished, function(event)
    pcall(function()
        local research = event.research
        if not (research and research.valid) then return end
        local tick = event.tick or game.tick
        local level = research.level or 1
        local line = string.format('{"type":"research_finished","important":false,"tick":%d,"tech":"%s","level":%d}',
            tick, escape_json(research.name), level)
        write_chronicle_line(line)
    end)
end)

-- 3. Запуск ракеты
script.on_event(defines.events.on_rocket_launched, function(event)
    pcall(function()
        local silo = event.rocket_silo
        local tick = event.tick or game.tick
        local shot_name = string.format("shot_rocket_%d.jpg", tick)
        local shot_rel = "coop_chronicle/" .. shot_name
        if silo and silo.valid then
            pcall(function()
                game.take_screenshot{
                    surface = silo.surface,
                    position = silo.position,
                    resolution = {x = 1280, y = 720},
                    zoom = 0.75,
                    path = shot_rel,
                    show_gui = false,
                    daytime = 0.5
                }
            end)
        end
        local line = string.format('{"type":"rocket_launched","important":true,"tick":%d,"screenshot":"%s"}',
            tick, shot_name)
        write_chronicle_line(line)
    end)
end)

-- 4. Постройка знаковых объектов
local function handle_built(event)
    pcall(function()
        local entity = event.entity or event.created_entity
        if not (entity and entity.valid) then return end
        if not MILESTONE_ENTITIES[entity.name] then return end
        local tick = event.tick or game.tick
        local player_name = "robot"
        if event.player_index then
            local p = game.get_player(event.player_index)
            if p then player_name = p.name end
        end
        local shot_name = string.format("shot_milestone_%s_%d.jpg", entity.name, tick)
        local shot_rel = "coop_chronicle/" .. shot_name
        pcall(function()
            game.take_screenshot{
                surface = entity.surface,
                position = entity.position,
                resolution = {x = 1280, y = 720},
                zoom = 0.75,
                path = shot_rel,
                show_gui = false,
                daytime = 0.5
            }
        end)
        local line = string.format('{"type":"milestone_built","important":true,"tick":%d,"player":"%s","entity":"%s","screenshot":"%s"}',
            tick, escape_json(player_name), escape_json(entity.name), shot_name)
        write_chronicle_line(line)
    end)
end

script.on_event(defines.events.on_built_entity, handle_built)
if defines.events.on_robot_built_entity then
    script.on_event(defines.events.on_robot_built_entity, handle_built)
end

-- 5. Ежечасный обзорный снимок базы (60 минут = 216000 тиков)
script.on_nth_tick(60 * 60 * 60, function(event)
    pcall(function()
        local tick = event.tick
        local p = game.connected_players and game.connected_players[1]
        local shot_name = string.format("shot_hourly_%d.jpg", tick)
        local shot_rel = "coop_chronicle/" .. shot_name
        if p and p.valid then
            pcall(function()
                game.take_screenshot{
                    player = p,
                    position = p.position,
                    resolution = {x = 1280, y = 720},
                    zoom = 0.5,
                    path = shot_rel,
                    show_gui = false,
                    daytime = 0.5
                }
            end)
        end
        local evo = 0.0
        if game.forces and game.forces["enemy"] then
            evo = game.forces["enemy"].evolution_factor or 0.0
        end
        local players_cnt = game.connected_players and #game.connected_players or 0
        local line = string.format('{"type":"hourly_snapshot","important":false,"tick":%d,"evolution_factor":%.3f,"players_online":%d,"screenshot":"%s"}',
            tick, evo, players_cnt, shot_name)
        write_chronicle_line(line)
    end)
end)

-- 6. Промежуточная 15-минутная статистика (15 минут = 54000 тиков)
script.on_nth_tick(60 * 60 * 15, function(event)
    pcall(function()
        local tick = event.tick
        local evo = 0.0
        if game.forces and game.forces["enemy"] then
            evo = game.forces["enemy"].evolution_factor or 0.0
        end
        local players_cnt = game.connected_players and #game.connected_players or 0
        local line = string.format('{"type":"stats_snapshot","important":false,"tick":%d,"evolution_factor":%.3f,"players_online":%d}',
            tick, evo, players_cnt)
        write_chronicle_line(line)
    end)
end)
"""


def build_mod_info(factorio_version: str = "2.0") -> dict[str, Any]:
    """Генерирует метаданные info.json для микро-мода хроники."""
    norm_version = "2.0" if factorio_version.startswith("2.") else "1.1"
    return {
        "name": MOD_NAME,
        "version": MOD_VERSION,
        "title": "Factorio Coop Chronicle",
        "author": "Factorio P2P Cloud Sync",
        "factorio_version": norm_version,
        "dependencies": ["? base >= 1.1.0"],
        "description": "Autonomously records player deaths, research achievements, milestone buildings, and hourly base snapshots for Telegram AI reporter.",
    }


def enable_mod_in_mod_list(mod_list_path: Path) -> bool:
    """Обеспечивает включенное состояние мода в mod-list.json."""
    try:
        mod_list_data: dict[str, Any] = {"mods": []}
        if mod_list_path.is_file():
            try:
                mod_list_data = json.loads(mod_list_path.read_text(encoding="utf-8"))
            except Exception as read_err:
                logger.warning(f"Не удалось прочитать {mod_list_path.name}: {read_err}. Создаем новый список модов.")
                mod_list_data = {"mods": []}

        mods = mod_list_data.get("mods")
        if not isinstance(mods, list):
            mods = []
            mod_list_data["mods"] = mods

        found = False
        for item in mods:
            if isinstance(item, dict) and item.get("name") == MOD_NAME:
                item["enabled"] = True
                found = True
                break

        if not found:
            mods.append({"name": MOD_NAME, "enabled": True})

        tmp_path = mod_list_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(mod_list_data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(mod_list_path)
        logger.debug(f"Мод {MOD_NAME} успешно активирован в {mod_list_path}")
        return True
    except Exception as err:
        logger.warning(f"Ошибка при обновлении {mod_list_path}: {err}")
        return False


def ensure_chronicle_mod_installed(
    mods_dir: Path | str,
    factorio_version: str = "2.0",
) -> Path:
    """Устанавливает или обновляет микро-мод factorio-coop-chronicle в папку mods.

    Создает структуру:
        mods/factorio-coop-chronicle_1.0.0/
            ├── info.json
            └── control.lua

    Также активирует мод в mods/mod-list.json.

    Args:
        mods_dir: Путь к папке mods Factorio.
        factorio_version: Версия игры Factorio ("2.0" или "1.1").

    Returns:
        Путь к директории установленного мода.
    """
    target_mods_dir = Path(mods_dir).resolve()
    target_mods_dir.mkdir(parents=True, exist_ok=True)

    mod_dir = target_mods_dir / MOD_FOLDER_NAME
    mod_dir.mkdir(parents=True, exist_ok=True)

    # 1. Запись info.json
    info_json_path = mod_dir / "info.json"
    info_content = build_mod_info(factorio_version)
    tmp_info = info_json_path.with_suffix(".tmp")
    tmp_info.write_text(json.dumps(info_content, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_info.replace(info_json_path)

    # 2. Запись control.lua
    control_lua_path = mod_dir / "control.lua"
    tmp_control = control_lua_path.with_suffix(".tmp")
    tmp_control.write_text(CONTROL_LUA_CONTENT, encoding="utf-8")
    tmp_control.replace(control_lua_path)

    # 3. Активация в mod-list.json
    mod_list_path = target_mods_dir / "mod-list.json"
    enable_mod_in_mod_list(mod_list_path)

    logger.info(f"Микро-мод хроники {MOD_NAME} успешно установлен в {mod_dir}")
    return mod_dir
