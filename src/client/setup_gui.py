"""Графическое окно первой настройки Factorio Co-op (FirstRunSetupWindow).

Обеспечивает ввод одноразового Кода Дружбы (FCT1-...) и секретного слова,
автоматическое считывание никнейма из player-data.json, обмен инвайта
на постоянный device_token и сохранение factorio_sync_config.json.
При повторных запусках окно больше не вызывается.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.hub_client import HubClient

logger = logging.getLogger("factorio_client.setup_gui")

DEFAULT_SERVER_WORLD_OPTION = "Текущий мир на сервере (не менять)"
KEEP_CURRENT_SERVER_WORLD_OPTION = "Текущий мир на сервере (не менять)"
SKIP_WORLD_UPLOAD_OPTION = "Не загружать (создать новый мир в игре)"


def publish_selected_world_to_server(
    env: FactorioEnvironment,
    hub_client: HubClient,
    source_save_path: Optional[Path | str] = None,
    player_name: str = "Player",
    save_path: Optional[Path | str] = None,
    on_progress: Optional[Callable[[int, int, float], None]] = None,
    world_name: Optional[str] = None,
) -> dict[str, Any]:
    """Публикует выбранный локальный мир на сервер в качестве нового cloud_world.zip.

    1. Валидирует source_save_path (существование, валидный ZIP).
    2. Проверяет статус сервера (GET /status): если сервер занят (не idle), выбрасывает RuntimeError.
    3. Создает резервную копию текущего локального cloud_world.zip в cloud_world_prev_local.zip (если есть).
    4. Атомарно копирует source_save_path в saves/cloud_world.zip.
    5. Захватывает роль Хоста (claim_host).
    6. Выгружает новый cloud_world.zip на VPS с release_on_success=True.

    Returns:
        Словарь с отчетом о публикации мира.
    """
    target_src = source_save_path if source_save_path is not None else save_path
    if target_src is None:
        raise ValueError("Не указан путь к файлу сохранения для публикации (source_save_path).")

    path = Path(target_src).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Файл сохранения '{path.name}' не найден.")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"Файл '{path.name}' поврежден или не является ZIP-архивом.")

    status = hub_client.get_status()
    state = status.get("state") or status.get("session", {}).get("state", "idle")
    if state != "idle":
        current_host = (
            status.get("current_host")
            or status.get("session", {}).get("host_name")
            or "другим игроком"
        )
        raise RuntimeError(
            f"Сейчас на сервере играет {current_host}. Сменить мир можно только когда сервер свободен."
        )

    canonical_save = env.saves_dir / env.cloud_save_name
    env.saves_dir.mkdir(parents=True, exist_ok=True)

    # Если исходный файл не является самим cloud_world.zip
    if path.resolve() != canonical_save.resolve():
        if canonical_save.exists():
            env.cloud_stash_dir.mkdir(parents=True, exist_ok=True)
            backup_save = env.cloud_stash_dir / f"{canonical_save.stem}_prev_local.zip"
            try:
                shutil.copy2(canonical_save, backup_save)
                logger.info(f"Создана резервная копия старого мира: {backup_save.name}")
            except Exception as b_err:
                logger.warning(f"Не удалось создать резервную копию {canonical_save.name}: {b_err}")

        # Атомарно обновляем saves/cloud_world.zip
        temp_target = env.saves_dir / f"{canonical_save.stem}_new_{int(time.time())}.tmp"
        try:
            shutil.copy2(path, temp_target)
            os.replace(temp_target, canonical_save)
            logger.info(f"Файл {path.name} скопирован в {canonical_save.name}")
        except Exception as copy_err:
            if temp_target.exists():
                try:
                    temp_target.unlink()
                except OSError:
                    pass
            raise RuntimeError(f"Ошибка копирования мира в {canonical_save.name}: {copy_err}")

    # Захватываем роль Хоста для выгрузки
    claim_res = hub_client.claim_host(player_name=player_name, wait_for_upload=True)
    if not claim_res.get("claimed"):
        reason = claim_res.get("reason", "сервер отклонил запрос")
        raise RuntimeError(f"Не удалось занять слот Хоста для публикации мира ({reason}).")

    session_token = str(claim_res.get("session_token", ""))
    if not session_token:
        raise RuntimeError("Сервер не вернул session_token при захвате слота.")

    upload_kwargs: dict[str, Any] = {
        "file_path": canonical_save,
        "session_token": session_token,
        "release_on_success": True,
        "mod_list": env.get_active_mods_manifest(),
    }
    if world_name is not None:
        upload_kwargs["world_name"] = str(world_name).strip()
    if on_progress is not None:
        upload_kwargs["progress_callback"] = on_progress

    try:
        upload_res = hub_client.upload_save(**upload_kwargs)
        logger.info(f"Мир {path.name} успешно выгружен на сервер как {env.cloud_save_name}: {upload_res}")
        return {
            "published": True,
            "source_save": str(path),
            "cloud_save": str(canonical_save),
            "upload_result": upload_res,
        }
    except Exception as up_err:
        logger.error(f"Ошибка выгрузки нового мира на сервер: {up_err}")
        try:
            hub_client.release_host(session_token, reason="publish_failed")
        except Exception:
            pass
        raise

# Цветовая палитра Factorio Dark Industrial
COLOR_BG = "#1f2124"
COLOR_CARD = "#2b2e33"
COLOR_BORDER = "#3a3f47"
COLOR_TEXT = "#f0f0f0"
COLOR_TEXT_MUTED = "#95a5a6"
COLOR_INPUT_BG = "#16171a"
COLOR_INPUT_FG = "#ffffff"
COLOR_ACCENT = "#e67e22"
COLOR_ACCENT_HOVER = "#d35400"
COLOR_SECONDARY = "#3d424a"
COLOR_SECONDARY_HOVER = "#4e545e"
COLOR_SUCCESS = "#2ecc71"
COLOR_ERROR = "#e74c3c"
COLOR_INFO = "#3498db"
COLOR_DANGER = "#c0392b"
COLOR_DANGER_HOVER = "#962d22"


class FirstRunSetupWindow:
    """Графическое окно первичной настройки и ввода инвайт-кода."""

    def __init__(
        self,
        root: Any,
        env: Optional[FactorioEnvironment] = None,
        config_path: Optional[Path | str] = None,
        installer_mode: bool = False,
        http_client: Optional[Any] = None,
        on_install_callback: Optional[Callable[[Path, str, ClientConfig], None]] = None,
        on_uninstall_callback: Optional[Callable[[Path], Any]] = None,
        prompt_message: Optional[str] = None,
        existing_config: Optional[ClientConfig] = None,
        auto_connect: bool = False,
    ) -> None:
        """Инициализирует окно настройки.

        Args:
            root: Корневой объект Tkinter (tk.Tk).
            env: Окружение путей Factorio (или дефолтное).
            config_path: Путь для сохранения factorio_sync_config.json.
            installer_mode: Режим установщика (дополнительный выбор папки игры и деинсталляция).
            http_client: Опциональный клиент httpx (для тестов).
            on_install_callback: Callback установки перехватчика в installer_mode.
            on_uninstall_callback: Callback удаления перехватчика в installer_mode.
            prompt_message: Дополнительное пояснительное сообщение в шапке окна.
            existing_config: Уже существующая конфигурация клиента (при открытии по Shift).
            auto_connect: Режим автоматического фонового подключения со спиннером.
        """
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.tk = tk
        self.ttk = ttk

        self.env = env or FactorioEnvironment()
        self.config_path = Path(config_path).resolve() if config_path else self.env.sync_config_path
        self.installer_mode = bool(installer_mode)
        self.http_client = http_client
        self.on_install_callback = on_install_callback
        self.on_uninstall_callback = on_uninstall_callback
        self.prompt_message = prompt_message
        self.existing_config = existing_config
        self.is_already_configured = bool(self.existing_config and self.existing_config.is_configured())
        self.auto_connect = bool(auto_connect and self.is_already_configured)

        self.server_has_save: Optional[bool] = None
        self.server_state: str = "idle"
        self.server_host_name: Optional[str] = None
        self.server_guests_count: int = 0
        self.server_world_name: Optional[str] = None
        self.server_world_display: Optional[str] = None
        self.server_world_version: Optional[int] = None
        self.server_world_size_mb: Optional[float] = None
        self.server_status_lbl: Optional[Any] = None
        self.inputs_card: Optional[Any] = None
        self.world_card: Optional[Any] = None
        self.world_title_lbl: Optional[Any] = None

        if self.auto_connect:
            self.step = "auto_connect"
        elif self.is_already_configured and self.existing_config:
            try:
                hub_client = HubClient(config=self.existing_config, http_client=self.http_client)
                st = hub_client.get_status(timeout=5.0)
                self._apply_server_status_dict(st)
            except Exception as e:
                logger.debug(f"Не удалось получить статус сервера при открытии окна: {e}")
                self.server_has_save = True

            if self.server_has_save is False:
                self.step = "select_initial_world"
            else:
                self.step = "manage_world"
        else:
            self.step = "enter_invite"

        self.result_config: Optional[ClientConfig] = None
        self.is_activating = False

        # Определение никнейма игрока
        try:
            self.player_name = self.env.resolve_or_init_player_username()
        except Exception as e:
            logger.warning(f"Не удалось определить никнейм из player-data.json: {e}")
            self.player_name = "Engineer"

        # Определение папки игры
        self.game_path: Optional[Path] = self.env.game_root
        self.was_uninstalled: bool = False

        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._poll_ui_queue()

        self._configure_dark_theme()
        self._init_window()
        self._build_ui()

    def _apply_server_status_dict(self, st: dict[str, Any]) -> None:
        """Обновляет внутренние поля статуса сервера и активного сохранения из ответа /status."""
        session = st.get("session") or {}
        save = st.get("save") or st.get("save_metadata") or {}

        self.server_has_save = bool(st.get("save_exists") or save.get("exists", False))
        self.server_state = str(st.get("state") or session.get("state") or "idle")
        self.server_host_name = st.get("current_host") or session.get("host_name")
        guests = st.get("active_guests") or session.get("active_guests") or []
        self.server_guests_count = len(guests) if isinstance(guests, list) else 0

        w_name = st.get("world_name") or save.get("world_name")
        disp_name = st.get("display_world_name") or save.get("display_world_name")
        if w_name:
            self.server_world_name = str(w_name)
            self.server_world_display = str(disp_name) if disp_name else f"{self.server_world_name} [Cloud]"
        elif disp_name:
            self.server_world_name = "cloud_world" if self.server_has_save else None
            self.server_world_display = str(disp_name)
        else:
            self.server_world_name = "cloud_world" if self.server_has_save else None
            self.server_world_display = None
        self.server_world_version = save.get("version_counter") or st.get("version_counter")
        size_b = save.get("size_bytes") or 0
        self.server_world_size_mb = round(size_b / (1024 * 1024), 1) if size_b else None

    def _format_server_status_banner(self) -> tuple[str, str]:
        """Возвращает кортеж (текст, цвет) для отображения статуса сервера в player_card."""
        if not self.is_already_configured:
            return ("Статус сервера: ожидание подключения по Коду Дружбы", COLOR_TEXT_MUTED)

        state = self.server_state
        host = self.server_host_name or "Игрок"
        guests = self.server_guests_count

        if state == "idle":
            return ("Статус сервера: [Свободен] — можно запускать игру (вы станете Хостом)", COLOR_SUCCESS)
        elif state == "claiming":
            return (f"Статус сервера: [Подготовка лобби...] Хост: {host}", COLOR_ACCENT)
        elif state == "hosting_active":
            return (f"Статус сервера: [В игре!] Хост: {host} (гостей: {guests}) — вы войдете как Гость", COLOR_INFO)
        elif state == "uploading_final":
            return (f"Статус сервера: [Сохранение мира в сейф...] Хост: {host}", COLOR_ACCENT)
        else:
            return (f"Статус сервера: [{state}]", COLOR_TEXT_MUTED)

    def _format_world_title(self) -> str:
        """Формирует текст заголовка карточки мира в зависимости от статуса сервера."""
        if self.server_has_save is False:
            if self.step == "select_initial_world":
                return "На сервере еще нет сохраненного мира. Выберите ваш мир для загрузки:"
            return "Активный мир на сервере: Не выбран (слот пуст — выберите мир для загрузки):"

        if self.server_world_display:
            if self.server_world_version and self.server_world_size_mb is not None:
                return f"Активный мир на сервере: {self.server_world_display} (v{self.server_world_version}, {self.server_world_size_mb} МБ)"
            return f"Активный мир на сервере: {self.server_world_display}"
        return "Активный мир на сервере:"

    def _update_server_status_widgets(self) -> None:
        """Обновляет виджеты статуса сервера и названия мира в UI."""
        if hasattr(self, "server_status_lbl") and self.server_status_lbl is not None:
            text, color = self._format_server_status_banner()
            try:
                self.server_status_lbl.config(text=text, fg=color)
            except Exception:
                pass

        if hasattr(self, "world_title_lbl") and self.world_title_lbl is not None:
            try:
                self.world_title_lbl.config(text=self._format_world_title())
            except Exception:
                pass

        if hasattr(self, "archive_world_btn") and self.archive_world_btn is not None:
            try:
                if self.is_already_configured and self.server_has_save:
                    self.archive_world_btn.pack(side=self.tk.LEFT)
                    if self.server_state != "idle":
                        self.archive_world_btn.config(state="disabled")
                    else:
                        self.archive_world_btn.config(state="normal")
                else:
                    self.archive_world_btn.pack_forget()
            except Exception:
                pass

    def _on_refresh_status_click(self) -> None:
        """Обрабатывает нажатие кнопки обновления статуса сервера."""
        if self.is_activating:
            return
        if not self.is_already_configured or not self.existing_config:
            return

        self.set_status("Опрос сервера...", color=COLOR_INFO)

        def _worker():
            try:
                hub_client = HubClient(config=self.existing_config, http_client=self.http_client)
                st = hub_client.get_status(timeout=5.0)
                self._apply_server_status_dict(st)
                self._schedule_ui(lambda: (
                    self._update_server_status_widgets(),
                    self.set_status("Статус сервера обновлен.", color=COLOR_SUCCESS),
                ))
            except Exception as e:
                logger.warning(f"Ошибка обновления статуса сервера: {e}")
                self._schedule_ui(lambda err=e: self.set_status(f"Ошибка связи: {err}", color=COLOR_ERROR))

        threading.Thread(target=_worker, daemon=True, name="RefreshStatusThread").start()

    def _configure_dark_theme(self) -> None:
        """Настраивает кроссплатформенный темный стиль виджетов через тему 'clam'."""
        ttk = self.ttk
        try:
            self.style = ttk.Style(self.root)
            if "clam" in self.style.theme_names():
                self.style.theme_use("clam")

            # Стиль основной акцентной кнопки (оранжевая)
            self.style.configure(
                "Accent.TButton",
                background=COLOR_ACCENT,
                foreground="#ffffff",
                bordercolor=COLOR_ACCENT,
                darkcolor=COLOR_ACCENT,
                lightcolor=COLOR_ACCENT,
                focuscolor="none",
                font=("Helvetica", 10, "bold"),
                padding=(10, 7),
            )
            self.style.map(
                "Accent.TButton",
                background=[
                    ("disabled", "#5a4531"),
                    ("pressed", COLOR_ACCENT_HOVER),
                    ("active", COLOR_ACCENT_HOVER),
                ],
                foreground=[
                    ("disabled", "#aaaaaa"),
                    ("active", "#ffffff"),
                ],
            )

            # Стиль второстепенных кнопок (темно-серые)
            self.style.configure(
                "Secondary.TButton",
                background=COLOR_SECONDARY,
                foreground=COLOR_TEXT,
                bordercolor=COLOR_BORDER,
                darkcolor=COLOR_SECONDARY,
                lightcolor=COLOR_SECONDARY,
                focuscolor="none",
                font=("Helvetica", 9),
                padding=(8, 4),
            )
            self.style.map(
                "Secondary.TButton",
                background=[
                    ("disabled", "#2b2e33"),
                    ("pressed", COLOR_SECONDARY_HOVER),
                    ("active", COLOR_SECONDARY_HOVER),
                ],
                foreground=[
                    ("disabled", "#777777"),
                    ("active", "#ffffff"),
                ],
            )

            # Стиль темного выпадающего списка
            self.style.configure(
                "Dark.TCombobox",
                fieldbackground=COLOR_INPUT_BG,
                background=COLOR_SECONDARY,
                foreground=COLOR_INPUT_FG,
                arrowcolor=COLOR_TEXT,
                bordercolor=COLOR_BORDER,
                lightcolor=COLOR_BORDER,
                darkcolor=COLOR_BORDER,
                selectbackground=COLOR_INPUT_BG,
                selectforeground=COLOR_INPUT_FG,
            )
            self.style.map(
                "Dark.TCombobox",
                fieldbackground=[
                    ("readonly", COLOR_INPUT_BG),
                    ("disabled", COLOR_CARD),
                ],
                foreground=[
                    ("readonly", COLOR_INPUT_FG),
                    ("disabled", COLOR_TEXT_MUTED),
                ],
                selectbackground=[("readonly", COLOR_INPUT_BG)],
                selectforeground=[("readonly", COLOR_INPUT_FG)],
                background=[
                    ("active", COLOR_SECONDARY_HOVER),
                    ("readonly", COLOR_SECONDARY),
                ],
            )

            # Настройка цветов выпадающего меню Listbox
            self.root.option_add("*TCombobox*Listbox.background", COLOR_INPUT_BG)
            self.root.option_add("*TCombobox*Listbox.foreground", COLOR_INPUT_FG)
            self.root.option_add("*TCombobox*Listbox.selectBackground", COLOR_ACCENT)
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

            # Стиль полосы прогресса Factorio Industrial (оранжевая на темном фоне)
            self.style.configure(
                "Dark.Horizontal.TProgressbar",
                thickness=16,
                troughcolor=COLOR_INPUT_BG,
                background=COLOR_ACCENT,
                bordercolor=COLOR_BORDER,
                lightcolor=COLOR_ACCENT,
                darkcolor=COLOR_ACCENT,
            )
        except Exception as e:
            logger.warning(f"Ошибка настройки темного стиля ttk: {e}")

    def _init_window(self) -> None:
        """Настраивает геометрические параметры и стили окна."""
        self.root.title("Factorio Co-op - Подключение к облачному миру")
        self.root.configure(bg=COLOR_BG)
        self.root.resizable(False, False)
        self.root.bind("<<Wakeup>>", lambda e: None)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.root.bind("<Command-w>", lambda e: self._on_window_close())
        self.root.bind("<Control-w>", lambda e: self._on_window_close())
        self._setup_menus_and_shortcuts()

        width = 540
        height = 640 if self.installer_mode else 560
        if self.prompt_message:
            height += 40

        try:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            pos_x = max(0, (screen_w - width) // 2)
            pos_y = max(0, (screen_h - height) // 2)
            self.root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        except Exception as err:
            logger.debug(f"Не удалось центрировать окно: {err}")
            self.root.geometry(f"{width}x{height}")

    def _setup_menus_and_shortcuts(self) -> None:
        """Настраивает системное меню Edit и универсальные сочетания клавиш (Cmd/Ctrl+A, Cmd/Ctrl+V и др.)."""
        tk = self.tk

        # Создаем нативное системное меню Edit (Правка) для macOS Cocoa responder chain
        try:
            menubar = tk.Menu(self.root)
            self.root.config(menu=menubar)
            edit_menu = tk.Menu(menubar, tearoff=False)
            menubar.add_cascade(label="Edit", menu=edit_menu)

            def _menu_select_all() -> None:
                widget = self.root.focus_get()
                if widget and hasattr(widget, "select_range"):
                    try:
                        widget.select_range(0, "end")
                        widget.icursor("end")
                    except Exception:
                        pass

            def _menu_delete_all() -> None:
                widget = self.root.focus_get()
                if widget and hasattr(widget, "delete"):
                    try:
                        widget.delete(0, "end")
                    except Exception:
                        pass

            edit_menu.add_command(
                label="Cut",
                accelerator="Cmd+X",
                command=lambda: self.root.focus_get() and self.root.focus_get().event_generate("<<Cut>>"),
            )
            edit_menu.add_command(
                label="Copy",
                accelerator="Cmd+C",
                command=lambda: self.root.focus_get() and self.root.focus_get().event_generate("<<Copy>>"),
            )
            edit_menu.add_command(
                label="Paste",
                accelerator="Cmd+V",
                command=lambda: self.root.focus_get() and self.root.focus_get().event_generate("<<Paste>>"),
            )
            edit_menu.add_command(
                label="Select All",
                accelerator="Cmd+A",
                command=_menu_select_all,
            )
            edit_menu.add_command(
                label="Clear",
                accelerator="Cmd+Backspace",
                command=_menu_delete_all,
            )
        except Exception as err:
            logger.debug(f"Не удалось настроить меню Edit: {err}")

        # Универсальный обработчик для полей ввода (Entry и TEntry) с поддержкой русской раскладки
        def _handle_entry_keypress(event: Any) -> Optional[str]:
            widget = getattr(event, "widget", None)
            if not widget:
                return None

            state = getattr(event, "state", 0)
            is_modifier = bool(state & 8) or bool(state & 4)
            if not is_modifier:
                return None

            keysym = str(getattr(event, "keysym", "")).lower()
            keycode = getattr(event, "keycode", -1)

            # Выделить всё: клавиша 'A' (на Mac keycode == 0, keysym 'a' или русская 'ф'/'cyrillic_ef')
            if keycode == 0 or keysym in ("a", "cyrillic_ef", "ф"):
                if hasattr(widget, "select_range"):
                    try:
                        widget.select_range(0, "end")
                        widget.icursor("end")
                    except Exception:
                        pass
                    return "break"
                if hasattr(widget, "selection_range"):
                    try:
                        widget.selection_range(0, "end")
                        widget.icursor("end")
                    except Exception:
                        pass
                    return "break"

            # Очистить всё поле ввода (Cmd+Backspace / Cmd+Delete):
            # На Mac keycode 51 (Backspace), 117 (Delete)
            if keycode in (51, 117) or keysym in ("backspace", "delete"):
                if hasattr(widget, "delete"):
                    try:
                        widget.delete(0, "end")
                    except Exception:
                        pass
                    return "break"

            # Копировать (Cmd+C / Ctrl+C):
            # На Mac keycode 8, keysym 'c' или русская 'с'/'cyrillic_es'
            if keycode == 8 or keysym in ("c", "cyrillic_es", "с"):
                if hasattr(widget, "selection_present") and widget.selection_present():
                    try:
                        sel_text = widget.selection_get()
                        self.root.clipboard_clear()
                        self.root.clipboard_append(sel_text)
                    except Exception:
                        pass
                    return "break"

            # Вставить (Cmd+V / Ctrl+V):
            # На Mac keycode 9, keysym 'v' или русская 'м'/'cyrillic_em'
            if keycode == 9 or keysym in ("v", "cyrillic_em", "м"):
                try:
                    clip_text = self.root.clipboard_get()
                    if hasattr(widget, "selection_present") and widget.selection_present():
                        widget.delete("sel.first", "sel.last")
                    widget.insert("insert", clip_text)
                except Exception:
                    pass
                return "break"

            # Вырезать (Cmd+X / Ctrl+X):
            # На Mac keycode 7, keysym 'x' или русская 'ч'/'cyrillic_che'
            if keycode == 7 or keysym in ("x", "cyrillic_che", "ч"):
                if hasattr(widget, "selection_present") and widget.selection_present():
                    try:
                        sel_text = widget.selection_get()
                        self.root.clipboard_clear()
                        self.root.clipboard_append(sel_text)
                        widget.delete("sel.first", "sel.last")
                    except Exception:
                        pass
                    return "break"

            return None

        self._on_entry_key = _handle_entry_keypress

    def _drain_ui_queue(self) -> None:
        """Выполняет все накопленные задачи из очереди в UI-потоке."""
        try:
            while True:
                task = self._ui_queue.get_nowait()
                try:
                    task()
                except Exception as err:
                    logger.error(f"Ошибка выполнения UI-задачи из очереди: {err}")
        except queue.Empty:
            pass

    def _poll_ui_queue(self) -> None:
        """Регулярно извлекает задачи из очереди и выполняет их в главном UI-потоке Tkinter."""
        if getattr(self, "_closing", False):
            return
        self._drain_ui_queue()
        try:
            self.root.after(40, self._poll_ui_queue)
        except Exception:
            pass

    def _schedule_ui(self, callback: Any) -> None:
        """Потокобезопасно отправляет задачу в UI-очередь главного потока."""
        self._ui_queue.put(callback)
        if threading.current_thread() is threading.main_thread():
            self._drain_ui_queue()

    def _build_ui(self) -> None:
        """Создает и размещает элементы пользовательского интерфейса."""
        tk = self.tk
        ttk = self.ttk

        main_container = tk.Frame(self.root, bg=COLOR_BG, padx=20, pady=16)
        main_container.pack(fill=tk.BOTH, expand=True)

        # 1. Шапка (Заголовок)
        header_frame = tk.Frame(main_container, bg=COLOR_BG)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        title_lbl = tk.Label(
            header_frame,
            text="Factorio Co-op",
            font=("Helvetica", 13, "bold"),
            fg=COLOR_TEXT,
            bg=COLOR_BG,
        )
        title_lbl.pack(anchor="w")

        # Опциональное информационное уведомление
        if self.prompt_message:
            prompt_frame = tk.Frame(
                main_container,
                bg="#342818",
                relief=tk.SOLID,
                bd=1,
                padx=10,
                pady=6,
            )
            prompt_frame.pack(fill=tk.X, pady=(0, 10))
            prompt_lbl = tk.Label(
                prompt_frame,
                text=self.prompt_message,
                font=("Helvetica", 9),
                fg="#f39c12",
                bg="#342818",
                wraplength=480,
                justify="left",
            )
            prompt_lbl.pack(anchor="w")

        # 2. Информационный блок игрока и статуса сервера
        player_card = tk.Frame(
            main_container,
            bg=COLOR_CARD,
            relief=tk.SOLID,
            bd=1,
            padx=12,
            pady=8,
        )
        player_card.pack(fill=tk.X, pady=(0, 10))

        player_row = tk.Frame(player_card, bg=COLOR_CARD)
        player_row.pack(fill=tk.X)

        player_title_lbl = tk.Label(
            player_row,
            text=f"Игрок: {self.player_name}",
            font=("Helvetica", 10, "bold"),
            fg=COLOR_ACCENT,
            bg=COLOR_CARD,
        )
        player_title_lbl.pack(side=tk.LEFT)

        if self.is_already_configured:
            self.refresh_btn = ttk.Button(
                player_row,
                text="Обновить статус",
                style="Secondary.TButton",
                command=self._on_refresh_status_click,
            )
            self.refresh_btn.pack(side=tk.RIGHT)

        status_text, status_color = self._format_server_status_banner()
        self.server_status_lbl = tk.Label(
            player_card,
            text=status_text,
            font=("Helvetica", 9),
            fg=status_color,
            bg=COLOR_CARD,
            wraplength=480,
            justify="left",
        )
        self.server_status_lbl.pack(anchor="w", pady=(3, 0))

        # 3. Секция выбора активного мира сервера
        self.world_card = tk.Frame(
            main_container,
            bg=COLOR_CARD,
            relief=tk.SOLID,
            bd=1,
            padx=12,
            pady=8,
        )

        world_title = self._format_world_title()
        self.world_title_lbl = tk.Label(
            self.world_card,
            text=world_title,
            font=("Helvetica", 9, "bold"),
            fg=COLOR_TEXT,
            bg=COLOR_CARD,
        )
        self.world_title_lbl.pack(anchor="w")

        local_saves = []
        try:
            local_saves = self.env.list_local_saves(include_cloud_world=False)
        except Exception as err:
            logger.warning(f"Ошибка получения списка сохранений: {err}")

        if self.is_already_configured:
            if self.server_has_save is False:
                self.world_options = [s.name for s in local_saves] + [SKIP_WORLD_UPLOAD_OPTION]
                default_val = self.world_options[0] if self.world_options else SKIP_WORLD_UPLOAD_OPTION
            else:
                self.world_options = [KEEP_CURRENT_SERVER_WORLD_OPTION] + [s.name for s in local_saves]
                default_val = KEEP_CURRENT_SERVER_WORLD_OPTION
        else:
            self.world_options = [s.name for s in local_saves] + [SKIP_WORLD_UPLOAD_OPTION]
            default_val = self.world_options[0] if self.world_options else SKIP_WORLD_UPLOAD_OPTION

        self.world_combo_var = tk.StringVar(value=default_val)

        self.world_combo = ttk.Combobox(
            self.world_card,
            textvariable=self.world_combo_var,
            values=self.world_options,
            state="readonly",
            style="Dark.TCombobox",
            font=("Helvetica", 9),
        )
        self.world_combo.pack(fill=tk.X, pady=(4, 4))

        self.world_actions_frame = tk.Frame(self.world_card, bg=COLOR_CARD)
        self.world_actions_frame.pack(fill=tk.X, pady=(2, 4))

        self.archive_world_btn = ttk.Button(
            self.world_actions_frame,
            text="Снять мир с сервера / В архив...",
            style="Secondary.TButton",
            command=self._on_archive_world_click,
        )
        if self.is_already_configured and self.server_has_save:
            self.archive_world_btn.pack(side=tk.LEFT)
            if self.server_state != "idle":
                self.archive_world_btn.config(state="disabled")

        self.world_hint_lbl = tk.Label(
            self.world_card,
            text="Одиночные игры и другие карты не затрагивают сервер",
            font=("Helvetica", 8),
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_CARD,
            wraplength=480,
            justify="left",
        )
        self.world_hint_lbl.pack(anchor="w")

        # Карточка выбора мира отображается сразу ТОЛЬКО если клиент уже настроен (запуск по Shift)
        if self.is_already_configured:
            self.world_card.pack(fill=tk.X, pady=(0, 10))

        # 4. Режим установщика: путь к игре
        if self.installer_mode:
            game_card = tk.Frame(
                main_container,
                bg=COLOR_CARD,
                relief=tk.SOLID,
                bd=1,
                padx=12,
                pady=8,
            )
            game_card.pack(fill=tk.X, pady=(0, 10))

            game_lbl = tk.Label(
                game_card,
                text="Папка с игрой Factorio:",
                font=("Helvetica", 9, "bold"),
                fg=COLOR_TEXT,
                bg=COLOR_CARD,
            )
            game_lbl.pack(anchor="w")

            path_row = tk.Frame(game_card, bg=COLOR_CARD)
            path_row.pack(fill=tk.X, pady=(4, 0))

            game_dir_str = str(self.game_path) if self.game_path else "Не определена автоматически"
            self.game_path_var = tk.StringVar(value=game_dir_str)
            self.game_path_entry = tk.Entry(
                path_row,
                textvariable=self.game_path_var,
                font=("Helvetica", 9),
                bg=COLOR_INPUT_BG,
                fg=COLOR_INPUT_FG,
                relief=tk.SOLID,
                bd=1,
                insertbackground=COLOR_INPUT_FG,
            )
            self.game_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=3)

            browse_btn = ttk.Button(
                path_row,
                text="Обзор...",
                style="Secondary.TButton",
                command=self._on_browse_game_dir,
            )
            browse_btn.pack(side=tk.RIGHT)

        # 5. Блок ввода Кода Дружбы и секретного слова
        self.inputs_card = tk.Frame(
            main_container,
            bg=COLOR_CARD,
            relief=tk.SOLID,
            bd=1,
            padx=12,
            pady=10,
        )
        if self.step != "auto_connect" and not (self.is_already_configured and self.server_has_save is False):
            self.inputs_card.pack(fill=tk.X, pady=(0, 10))

        # Поле 1: Код Дружбы
        code_title = (
            "Новый Код Дружбы (необязательно):"
            if self.is_already_configured
            else "Код Дружбы:"
        )
        code_lbl = tk.Label(
            self.inputs_card,
            text=code_title,
            font=("Helvetica", 9, "bold"),
            fg=COLOR_TEXT,
            bg=COLOR_CARD,
        )
        code_lbl.pack(anchor="w")

        code_row = tk.Frame(self.inputs_card, bg=COLOR_CARD)
        code_row.pack(fill=tk.X, pady=(4, 10))

        self.code_var = tk.StringVar(value="")
        self.code_entry = tk.Entry(
            code_row,
            textvariable=self.code_var,
            font=("Helvetica", 10),
            bg=COLOR_INPUT_BG,
            fg=COLOR_INPUT_FG,
            relief=tk.SOLID,
            bd=1,
            insertbackground=COLOR_INPUT_FG,
        )
        self.code_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=4)

        paste_btn = ttk.Button(
            code_row,
            text="Вставить",
            style="Secondary.TButton",
            command=self._on_paste_code,
        )
        paste_btn.pack(side=tk.RIGHT)

        # Поле 2: Секретное слово
        pass_title = (
            "Секретное слово (необязательно):"
            if self.is_already_configured
            else "Секретное слово:"
        )
        pass_lbl = tk.Label(
            self.inputs_card,
            text=pass_title,
            font=("Helvetica", 9, "bold"),
            fg=COLOR_TEXT,
            bg=COLOR_CARD,
        )
        pass_lbl.pack(anchor="w")

        pass_row = tk.Frame(self.inputs_card, bg=COLOR_CARD)
        pass_row.pack(fill=tk.X, pady=(4, 4))

        self.pass_var = tk.StringVar()
        self.pass_entry = tk.Entry(
            pass_row,
            textvariable=self.pass_var,
            font=("Helvetica", 10),
            bg=COLOR_INPUT_BG,
            fg=COLOR_INPUT_FG,
            show="*",
            relief=tk.SOLID,
            bd=1,
            insertbackground=COLOR_INPUT_FG,
        )
        self.pass_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=4)

        self.show_pass = False
        self.toggle_pass_btn = ttk.Button(
            pass_row,
            text="Показать",
            style="Secondary.TButton",
            command=self._on_toggle_pass,
        )
        self.toggle_pass_btn.pack(side=tk.RIGHT)

        if hasattr(self, "_on_entry_key"):
            self.code_entry.bind("<KeyPress>", self._on_entry_key)
            self.pass_entry.bind("<KeyPress>", self._on_entry_key)
            if self.installer_mode and hasattr(self, "game_path_entry"):
                self.game_path_entry.bind("<KeyPress>", self._on_entry_key)

        # 6. Строка состояния (Status Bar)
        if self.step == "auto_connect":
            initial_status = "Пожалуйста, подождите. Идет подключение к облачной игре..."
        elif self.is_already_configured:
            if self.server_has_save is False:
                initial_status = "Сервер подключен, но пуст. Выберите мир для загрузки."
            else:
                initial_status = "Сервер уже подключен. Выберите мир и нажмите «Применить и запустить»."
        else:
            initial_status = "Введите полученный Код Дружбы и секретное слово"
        self.status_var = tk.StringVar(value=initial_status)
        self.status_lbl = tk.Label(
            main_container,
            textvariable=self.status_var,
            font=("Helvetica", 9),
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_BG,
            wraplength=490,
            justify="center",
        )
        self.status_lbl.pack(fill=tk.X, pady=(4, 6))

        # 6.1 Карточка шкалы прогресса загрузки/выгрузки мира (изначально не упакована)
        self.progress_card = tk.Frame(
            main_container,
            bg=COLOR_CARD,
            relief=tk.SOLID,
            bd=1,
            padx=12,
            pady=8,
        )
        self.progress_title_lbl = tk.Label(
            self.progress_card,
            text="Загрузка мира на сервер...",
            font=("Helvetica", 9, "bold"),
            fg=COLOR_TEXT,
            bg=COLOR_CARD,
        )
        self.progress_title_lbl.pack(anchor="w", pady=(0, 4))

        self.progress_bar = ttk.Progressbar(
            self.progress_card,
            style="Dark.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.progress_bar.pack(fill=tk.X, pady=(0, 6))

        self.progress_info_frame = tk.Frame(self.progress_card, bg=COLOR_CARD)
        self.progress_info_frame.pack(fill=tk.X)

        self.progress_bytes_lbl = tk.Label(
            self.progress_info_frame,
            text="0.0 / 0.0 МБ (0%)",
            font=("Helvetica", 8),
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_CARD,
        )
        self.progress_bytes_lbl.pack(side=tk.LEFT)

        self.progress_speed_lbl = tk.Label(
            self.progress_info_frame,
            text="0.0 МБ/с",
            font=("Helvetica", 8),
            fg=COLOR_ACCENT,
            bg=COLOR_CARD,
        )
        self.progress_speed_lbl.pack(side=tk.RIGHT)

        # 7. Кнопки действий
        self.btn_frame = tk.Frame(main_container, bg=COLOR_BG)
        self.btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        btn_frame = self.btn_frame

        if self.step == "auto_connect":
            action_text = "Сменить мир / Настройки (Shift)"
            action_style = "Secondary.TButton"
            action_cmd = self._transition_to_manage_world
        elif self.installer_mode:
            action_text = "Установить и подключить"
            action_style = "Accent.TButton"
            action_cmd = self._on_activate_click
        elif self.is_already_configured:
            action_text = (
                "Загрузить мир на сервер и играть"
                if self.server_has_save is False
                else "Применить и запустить игру"
            )
            action_style = "Accent.TButton"
            action_cmd = self._on_activate_click
        else:
            action_text = "Подключить и продолжить"
            action_style = "Accent.TButton"
            action_cmd = self._on_activate_click

        self.action_btn = ttk.Button(
            btn_frame,
            text=action_text,
            style=action_style,
            command=action_cmd,
        )
        self.action_btn.pack(fill=tk.X, pady=(0, 6))

        # Дополнительные кнопки
        if self.installer_mode:
            self.uninstall_btn = ttk.Button(
                btn_frame,
                text="Удалить перехватчик (Вернуть чистую игру)",
                style="Secondary.TButton",
                command=self._on_uninstall_click,
            )
            self.uninstall_btn.pack(fill=tk.X)
        else:
            self.cancel_btn = ttk.Button(
                btn_frame,
                text="Запустить игру автономно (без сервера)",
                style="Secondary.TButton",
                command=self._on_cancel_click,
            )
            self.cancel_btn.pack(fill=tk.X, pady=(0, 6))

            self.uninstall_btn = ttk.Button(
                btn_frame,
                text="Удалить перехватчик (Вернуть чистую игру)",
                style="Secondary.TButton",
                command=self._on_uninstall_click,
            )
            self.uninstall_btn.pack(fill=tk.X)

        if self.step == "auto_connect":
            self.progress_card.pack(fill=tk.X, pady=(0, 10))
            self.progress_title_lbl.config(text="Подключение к облачному миру...")
            self.progress_bar.config(mode="indeterminate")
            try:
                self.progress_bar.start(15)
            except Exception:
                pass
            self.progress_bytes_lbl.config(text="Связь с сервером Vaultorio...")
            self.progress_speed_lbl.config(text="")

            worker = threading.Thread(
                target=self._auto_connect_worker,
                daemon=True,
                name="AutoConnectWorker",
            )
            worker.start()

        # Устанавливаем фокус ввода при открытии окна на первое поле
        try:
            self.code_entry.focus_set()
        except Exception:
            pass

    def _transition_to_manage_world(self) -> None:
        """Переключает окно из режима автоподключения в режим управления миром."""
        self.step = "manage_world"
        try:
            self.progress_bar.stop()
        except Exception:
            pass
        self.progress_card.pack_forget()
        if self.inputs_card:
            self.inputs_card.pack(fill=self.tk.X, pady=(0, 10))
        if self.world_card:
            self.world_card.pack(fill=self.tk.X, pady=(0, 10))
        self._update_server_status_widgets()
        self.action_btn.config(
            text="Применить и запустить игру",
            style="Accent.TButton",
            command=self._on_activate_click,
        )
        self.set_status("Сервер подключен. Выберите мир и нажмите «Применить и запустить».", color=COLOR_INFO)

    def _auto_connect_worker(self) -> None:
        """Фоновый рабочий поток для автоматического подключения к серверу при обычном запуске."""
        try:
            hub_client = HubClient(config=self.existing_config, http_client=self.http_client)
            # Щедрый таймаут 20 секунд для нестабильного Wi-Fi
            st = hub_client.get_status(timeout=20.0)
            self._apply_server_status_dict(st)
            self._schedule_ui(self._update_server_status_widgets)

            if self.step != "auto_connect":
                return

            if self.server_has_save is False:
                local_saves = []
                try:
                    local_saves = self.env.list_local_saves(include_cloud_world=False)
                except Exception:
                    pass
                if local_saves:
                    self._schedule_ui(lambda: self._transition_to_empty_server_world_selection(self.existing_config, local_saves))
                    return

            # Проверяем, свободен ли сервер (роль Хоста) и нужно ли обновить локальный сейв
            server_state = st.get("state") or st.get("session", {}).get("state", "idle")
            server_sha = st.get("sha256") or st.get("save", {}).get("sha256")
            local_save = self.env.saves_dir / self.env.cloud_save_name

            if server_state == "idle" and server_sha and self.server_has_save:
                needs_download = True
                if local_save.is_file():
                    try:
                        from src.client.hub_client import compute_file_sha256
                        if compute_file_sha256(local_save) == server_sha:
                            needs_download = False
                    except Exception:
                        needs_download = True

                if needs_download:
                    logger.info("Обнаружена новая версия сейва на сервере. Скачивание через лаунчер...")
                    self._schedule_ui(lambda: self._show_progress_card("Синхронизация мира с сервером..."))
                    self._schedule_ui(lambda: self.set_status("Загрузка актуального мира из облака...", color=COLOR_INFO))

                    def _progress_cb(downloaded: int, total: int, speed: float) -> None:
                        self._schedule_ui(lambda: self._update_progress(downloaded, total, speed))

                    hub_client.download_save(
                        target_path=local_save,
                        expected_sha=server_sha,
                        progress_callback=_progress_cb,
                    )

            self._schedule_ui(self._on_auto_connect_success)
        except Exception as exc:
            logger.warning(f"Ошибка при автоподключении к серверу: {exc}")
            if self.step == "auto_connect":
                self._schedule_ui(lambda e=exc: self._on_auto_connect_failure(e))

    def _on_auto_connect_success(self) -> None:
        """Обрабатывает успешное автоподключение."""
        if self.step != "auto_connect":
            return
        try:
            self.progress_bar.stop()
            self.progress_bar.config(mode="determinate", value=100)
        except Exception:
            pass
        self.progress_title_lbl.config(text="Сервер готов!")
        self.progress_bytes_lbl.config(text="Облачный мир готов к запуску")
        self.set_status("Синхронизация завершена! Запуск игры Factorio...", color=COLOR_SUCCESS)
        self.result_config = self.existing_config
        self.root.after(400, self._close_window_cleanly)

    def _on_auto_connect_failure(self, exc: Exception) -> None:
        """Обрабатывает сбой автоподключения (таймаут / отсутствие сети)."""
        if self.step != "auto_connect":
            return
        try:
            self.progress_bar.stop()
        except Exception:
            pass
        self.progress_card.pack_forget()
        err_str = str(exc).lower()
        err_msg = "Таймаут соединения" if "timed out" in err_str else "Сервер недоступен"
        self.set_status(f"{err_msg}. Запуск игры в автономном режиме (OFFLINE)...", color=COLOR_WARNING)
        self.result_config = ClientConfig(was_uninstalled=False, offline_mode=True)
        self.root.after(1200, self._close_window_cleanly)

    # =========================================================================
    # Обработчики событий пользовательского интерфейса
    # =========================================================================

    def _on_paste_code(self) -> None:
        """Вставляет содержимое буфера обмена в поле кода."""
        try:
            clipboard_text = self.root.clipboard_get()
            if clipboard_text:
                clean_text = clipboard_text.strip()
                self.code_var.set(clean_text)
                self.set_status("Код вставлен из буфера обмена", color=COLOR_INFO)
        except Exception as e:
            logger.debug(f"Не удалось получить текст из буфера обмена: {e}")
            self.set_status("Буфер обмена пуст или недоступен", color=COLOR_TEXT_MUTED)

    def _on_toggle_pass(self) -> None:
        """Переключает видимость символов в поле секретного слова."""
        self.show_pass = not self.show_pass
        if self.show_pass:
            self.pass_entry.config(show="")
            self.toggle_pass_btn.config(text="Скрыть")
        else:
            self.pass_entry.config(show="*")
            self.toggle_pass_btn.config(text="Показать")

    def _on_browse_game_dir(self) -> None:
        """Открывает диалог выбора папки игры Factorio."""
        from tkinter import filedialog

        initial_dir = str(self.game_path) if self.game_path and self.game_path.is_dir() else str(Path.home())
        selected = filedialog.askdirectory(
            initialdir=initial_dir,
            title="Выберите папку с игрой Factorio",
            mustexist=True,
        )
        if selected:
            p = Path(selected).resolve()
            self.game_path = p
            self.game_path_var.set(str(p))
            self.set_status(f"Выбрана папка: {p.name}", color=COLOR_INFO)

    def set_status(self, message: str, color: str = COLOR_TEXT_MUTED) -> None:
        """Безопасно обновляет строку состояния."""
        self.status_var.set(message)
        self.status_lbl.config(fg=color)

    def show_progress(self, title: str = "Загрузка мира на сервер...") -> None:
        """Отображает шкалу прогресса и сбрасывает индикаторы."""
        if hasattr(self, "progress_title_lbl"):
            self.progress_title_lbl.config(text=title)
        if hasattr(self, "progress_bar"):
            self.progress_bar["value"] = 0
        if hasattr(self, "progress_bytes_lbl"):
            self.progress_bytes_lbl.config(text="0.0 / 0.0 МБ (0%)")
        if hasattr(self, "progress_speed_lbl"):
            self.progress_speed_lbl.config(text="Подготовка...")
        if hasattr(self, "progress_card"):
            try:
                if not self.progress_card.winfo_ismapped():
                    self.progress_card.pack(fill=self.tk.X, pady=(0, 8))
            except Exception:
                pass

    def hide_progress(self) -> None:
        """Скрывает шкалу прогресса."""
        if hasattr(self, "progress_card"):
            try:
                if self.progress_card.winfo_ismapped():
                    self.progress_card.pack_forget()
            except Exception:
                pass

    def update_progress(
        self, uploaded_bytes: int, total_bytes: int, speed_bytes_per_sec: float
    ) -> None:
        """Обновляет шкалу прогресса, объем переданных данных, скорость и ETA."""
        if not hasattr(self, "progress_card"):
            return

        if not self.progress_card.winfo_ismapped():
            self.show_progress()

        total = max(1, total_bytes)
        uploaded = min(total, max(0, uploaded_bytes))
        percent = (uploaded / total) * 100.0

        if hasattr(self, "progress_bar"):
            self.progress_bar["value"] = percent

        uploaded_mb = uploaded / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        if hasattr(self, "progress_bytes_lbl"):
            self.progress_bytes_lbl.config(
                text=f"{uploaded_mb:.1f} / {total_mb:.1f} МБ ({percent:.0f}%)"
            )

        # Форматирование скорости передачи
        if speed_bytes_per_sec >= 1024 * 1024:
            speed_str = f"{speed_bytes_per_sec / (1024 * 1024):.1f} МБ/с"
        elif speed_bytes_per_sec >= 1024:
            speed_str = f"{speed_bytes_per_sec / 1024:.0f} КБ/с"
        else:
            speed_str = f"{speed_bytes_per_sec:.0f} Б/с"

        # Форматирование расчетного времени завершения (ETA)
        if speed_bytes_per_sec > 1024 and total > uploaded:
            remaining_bytes = total - uploaded
            remaining_sec = max(1, int(remaining_bytes / speed_bytes_per_sec))
            if remaining_sec < 60:
                eta_str = f"осталось ~{remaining_sec} сек"
            else:
                eta_str = f"осталось ~{remaining_sec // 60} мин {remaining_sec % 60} сек"
            speed_detail = f"{speed_str} • {eta_str}"
        elif uploaded >= total:
            speed_detail = "Завершено! Проверка..."
        else:
            speed_detail = speed_str

        if hasattr(self, "progress_speed_lbl"):
            self.progress_speed_lbl.config(text=speed_detail)

    def _on_upload_progress_from_worker(
        self, uploaded: int, total: int, speed: float
    ) -> None:
        """Потокобезопасно передает событие прогресса из фонового потока в UI-очередь."""
        self._schedule_ui(lambda u=uploaded, t=total, s=speed: self.update_progress(u, t, s))

    def _close_window_cleanly(self) -> None:
        """Принудительно скрывает и безопасно уничтожает окно GUI.

        На macOS вызов destroy() без предварительного withdraw() может оставлять
        окно видимым на экране, если последующий код перехватчика блокирует поток
        в ожидании процесса игры, вызывая зависание (Not Responding).
        Скрытие окна через withdraw() и прокачка очереди событий Cocoa гарантируют,
        что WindowServer уберёт окно до завершения mainloop.
        """
        self._closing = True
        try:
            self.root.withdraw()
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        try:
            self.root.update()
        except Exception:
            pass

    def _on_window_close(self) -> None:
        """Закрывает окно при нажатии на крестик (полный выход без запуска игры)."""
        logger.info("Пользователь нажал крестик закрытия окна (отмена запуска игры).")
        self.result_config = ClientConfig(user_aborted=True)
        self._close_window_cleanly()

    def _on_cancel_click(self) -> None:
        """Закрывает окно без сохранения настроек (автономный запуск)."""
        logger.info("Пользователь выбрал автономный запуск игры (без сервера).")
        self.result_config = ClientConfig(was_uninstalled=False, offline_mode=True)
        self._close_window_cleanly()

    def _on_uninstall_click(self) -> None:
        """Запускает процесс деинсталляции перехватчика."""
        game_dir = self.game_path or (self.env.game_root if self.env else None)
        if not game_dir:
            try:
                from src.installer.installer import FactorioWrapperInstaller
                game_dir = FactorioWrapperInstaller().find_game_installation()
            except Exception:
                game_dir = None

        if not game_dir:
            self.set_status("Не удалось найти папку с игрой Factorio для удаления перехватчика", color=COLOR_ERROR)
            return

        from tkinter import messagebox

        confirm = messagebox.askyesno(
            "Удаление перехватчика",
            "Вы действительно хотите удалить перехватчик и вернуть оригинальную чистую игру Factorio?\n\n"
            "- Оригинальный исполняемый файл игры будет восстановлен на свое место.\n"
            "- Файлы конфигурации синхронизатора будут удалены.\n"
            "- Ваши игровые сохранения (миры) останутся в полной сохранности.\n\n"
            "Продолжить удаление?",
        )
        if not confirm:
            return

        try:
            if self.on_uninstall_callback:
                res = self.on_uninstall_callback(game_dir)
            else:
                from src.installer.installer import FactorioWrapperInstaller
                installer = FactorioWrapperInstaller()
                res = installer.uninstall(game_dir=game_dir)

            self.was_uninstalled = True
            self.result_config = ClientConfig(was_uninstalled=True)

            msg = res.get("message", "Перехватчик успешно удален") if isinstance(res, dict) else "Успешно удалено"
            messagebox.showinfo(
                "Удаление завершено",
                f"{msg}\n\nОригинальная игра Factorio восстановлена в первоначальное состояние. "
                f"Теперь игра будет запускаться стандартным образом без синхронизатора.",
            )
            self._close_window_cleanly()
        except Exception as e:
            logger.error(f"Ошибка при удалении перехватчика: {e}")
            err_str = str(e)
            if sys.platform == "win32" and ("WinError 5" in err_str or isinstance(e, PermissionError) or "Отказано в доступе" in err_str):
                try:
                    import ctypes
                    cmd_line = "-NoProfile -ExecutionPolicy Bypass -Command \"irm https://raw.githubusercontent.com/GreyPikachu/Vaultorio/main/deploy/uninstall.ps1 | iex\""
                    ret = ctypes.windll.shell32.ShellExecuteW(
                        None,
                        "runas",
                        "powershell.exe",
                        cmd_line,
                        None,
                        1,
                    )
                    if ret > 32:
                        self.set_status("Запрошены права администратора. Подтвердите окно UAC...", color=COLOR_INFO)
                        self.was_uninstalled = True
                        self.result_config = ClientConfig(was_uninstalled=True)
                        self.root.after(1500, self._close_window_cleanly)
                        return
                except Exception as uac_err:
                    logger.warning(f"Ошибка запуска UAC деинсталлятора: {uac_err}")
            self.set_status(f"Ошибка удаления: {e}", color=COLOR_ERROR)

    def _on_activate_click(self) -> None:
        """Валидирует ввод и запускает фоновую активацию или смену мира."""
        if self.is_activating:
            return

        # Шаг 2: выбор мира для загрузки на пустой сервер
        if self.step == "select_initial_world":
            selected_world = self.world_combo_var.get().strip()
            if not selected_world or selected_world == SKIP_WORLD_UPLOAD_OPTION:
                logger.info("Пользователь выбрал не загружать мир на сервер.")
                self.result_config = self.existing_config
                self.set_status("Запуск игры...", color=COLOR_SUCCESS)
                self.root.after(300, self._close_window_cleanly)
                return

            self.is_activating = True
            self.action_btn.config(state="disabled")
            self.set_status(f"Загрузка мира '{selected_world}' на сервер...", color=COLOR_INFO)

            worker = threading.Thread(
                target=self._publish_world_worker,
                args=(selected_world, self.existing_config),
                daemon=True,
                name="WorldPublishThread",
            )
            worker.start()
            return

        # Шаг manage_world (открытие по Shift с уже настроенным клиентом)
        code = self.code_var.get().strip()
        passphrase = self.pass_var.get().strip()
        selected_world = self.world_combo_var.get().strip()

        if self.is_already_configured and not code and not passphrase:
            if selected_world in (
                DEFAULT_SERVER_WORLD_OPTION,
                KEEP_CURRENT_SERVER_WORLD_OPTION,
                SKIP_WORLD_UPLOAD_OPTION,
                "Оставить текущий мир сервера (cloud_world.zip)",
            ):
                self.result_config = self.existing_config
                self.set_status("Настройки сохранены. Запуск игры...", color=COLOR_SUCCESS)
                self.root.after(300, self._close_window_cleanly)
                return

            self.is_activating = True
            self.action_btn.config(state="disabled")
            self.set_status(f"Публикация мира '{selected_world}' на сервер...", color=COLOR_INFO)

            worker = threading.Thread(
                target=self._publish_world_worker,
                args=(selected_world, self.existing_config),
                daemon=True,
                name="WorldPublishThread",
            )
            worker.start()
            return

        if not code:
            self.set_status("Пожалуйста, введите Код Дружбы", color=COLOR_ERROR)
            self.code_entry.focus_set()
            return

        if not code.startswith("FCT1-"):
            self.set_status("Код Дружбы должен начинаться с 'FCT1-'", color=COLOR_ERROR)
            self.code_entry.focus_set()
            return

        if not passphrase:
            self.set_status("Пожалуйста, введите секретное слово (пароль)", color=COLOR_ERROR)
            self.pass_entry.focus_set()
            return

        if self.installer_mode and (not self.game_path or not self.game_path.exists()):
            self.set_status("Укажите существующую папку с игрой Factorio", color=COLOR_ERROR)
            return

        self.is_activating = True
        self.action_btn.config(state="disabled")
        self.set_status("Проверка кода и подключение к серверу...", color=COLOR_INFO)

        # Фоновый рабочий поток
        worker = threading.Thread(
            target=self._activate_worker,
            args=(code, passphrase, selected_world),
            daemon=True,
            name="FirstRunActivationThread",
        )
        worker.start()

    def _publish_world_worker(self, save_name: str, cfg: Optional[ClientConfig]) -> None:
        """Публикует выбранный мир на сервер в фоновом потоке."""
        try:
            if not cfg:
                raise ValueError("Конфигурация клиента отсутствует.")
            self._schedule_ui(
                lambda s=save_name: self.show_progress(f"Выгрузка мира '{s}' на сервер...")
            )
            hub_client = HubClient(config=cfg, http_client=self.http_client)
            save_path = self.env.saves_dir / save_name
            publish_selected_world_to_server(
                env=self.env,
                hub_client=hub_client,
                source_save_path=save_path,
                player_name=self.player_name,
                on_progress=self._on_upload_progress_from_worker,
                world_name=save_path.stem,
            )
            try:
                st = hub_client.get_status(timeout=5.0)
                self._apply_server_status_dict(st)
                self._schedule_ui(self._update_server_status_widgets)
            except Exception:
                pass
            self.result_config = cfg
            self._schedule_ui(self._on_publish_success)
        except Exception as exc:
            err = exc
            logger.warning(f"Ошибка при публикации выбранного мира: {err}")
            self._schedule_ui(lambda e=err: self._on_activation_error(e))

    def _on_archive_world_click(self) -> None:
        """Открывает диалог выбора: скачать мир локально перед удалением или сразу архивировать на VPS."""
        if self.is_activating:
            return

        try:
            dialog = tk.Toplevel(self.root)
            dialog.title("Управление сервером")
            dialog.configure(bg=COLOR_BG)
            dialog.resizable(False, False)
            dialog.transient(self.root)
            dialog.grab_set()

            # Центрирование относительно главного окна
            w, h = 500, 260
            try:
                rx = self.root.winfo_x()
                ry = self.root.winfo_y()
                rw = self.root.winfo_width()
                rh = self.root.winfo_height()
                x = max(0, rx + (rw - w) // 2)
                y = max(0, ry + (rh - h) // 2)
                dialog.geometry(f"{w}x{h}+{x}+{y}")
            except Exception:
                dialog.geometry(f"{w}x{h}")

            frame = tk.Frame(dialog, bg=COLOR_CARD, padx=16, pady=16, relief=tk.SOLID, bd=1)
            frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

            title_lbl = tk.Label(
                frame,
                text="Архивация и снятие мира с сервера",
                font=("Helvetica", 11, "bold"),
                fg=COLOR_TEXT,
                bg=COLOR_CARD,
            )
            title_lbl.pack(anchor="w", pady=(0, 6))

            desc_lbl = tk.Label(
                frame,
                text=(
                    "Текущий общий мир будет снят с сервера и перемещен в архив на VPS.\n"
                    "После этого сервер станет пустым, и вы сможете выбрать другой мир.\n\n"
                    "Вы хотите сохранить копию мира в свои локальные сохранения Factorio?"
                ),
                font=("Helvetica", 9),
                fg=COLOR_TEXT_MUTED,
                bg=COLOR_CARD,
                wraplength=460,
                justify="left",
            )
            desc_lbl.pack(anchor="w", pady=(0, 12))

            def _do_action(download_local: bool) -> None:
                try:
                    dialog.destroy()
                except Exception:
                    pass
                self._execute_world_archive(download_local=download_local)

            btn_row = tk.Frame(frame, bg=COLOR_CARD)
            btn_row.pack(fill=tk.X, pady=(4, 0))

            btn_dl = ttk.Button(
                btn_row,
                text="Скачать в сохранения и снять с сервера",
                style="Accent.TButton",
                command=lambda: _do_action(download_local=True),
            )
            btn_dl.pack(fill=tk.X, pady=(0, 6))

            btn_vps = ttk.Button(
                btn_row,
                text="Снять с сервера (сохранить только на VPS)",
                style="Secondary.TButton",
                command=lambda: _do_action(download_local=False),
            )
            btn_vps.pack(fill=tk.X, pady=(0, 6))

            btn_cancel = ttk.Button(
                btn_row,
                text="Отмена",
                style="Secondary.TButton",
                command=dialog.destroy,
            )
            btn_cancel.pack(fill=tk.X)
        except Exception as e:
            logger.warning(f"Ошибка открытия окна диалога архивации: {e}")
            from tkinter import messagebox
            ans = messagebox.askyesno(
                "Снятие мира с сервера",
                "Снять текущий мир с сервера и сохранить его в архив VPS?\n"
                "(Сервер станет пустым для выбора нового мира)",
            )
            if ans:
                self._execute_world_archive(download_local=False)

    def _execute_world_archive(self, download_local: bool) -> None:
        """Запускает процесс архивации и сброса мира на сервере в фоновом потоке."""
        if self.is_activating:
            return
        self.is_activating = True
        self.action_btn.config(state="disabled")
        if hasattr(self, "archive_world_btn"):
            self.archive_world_btn.config(state="disabled")

        self.set_status("Связь с сервером...", color=COLOR_INFO)

        worker = threading.Thread(
            target=self._archive_world_worker,
            args=(download_local,),
            daemon=True,
            name="WorldArchiveThread",
        )
        worker.start()

    def _archive_world_worker(self, download_local: bool) -> None:
        """Фоновый рабочий поток архивации мира."""
        try:
            cfg = self.existing_config
            if not cfg:
                raise ValueError("Конфигурация клиента отсутствует.")

            hub_client = HubClient(config=cfg, http_client=self.http_client)
            # 1. Проверяем статус сервера
            st = hub_client.get_status(timeout=10.0)
            self._apply_server_status_dict(st)
            self._schedule_ui(self._update_server_status_widgets)
            state = self.server_state
            if state != "idle":
                current_host = self.server_host_name or "другой игрок"
                raise RuntimeError(
                    f"Сейчас на сервере играет {current_host}. Снять мир можно только когда сервер свободен."
                )

            saved_local_name: Optional[str] = None
            if download_local:
                self._schedule_ui(
                    lambda: self.set_status("Скачивание копии мира в локальные сохранения...", color=COLOR_INFO)
                )
                ts_str = datetime.now().strftime("%Y-%m-%d_%H-%M")
                if self.server_world_name and self.server_world_name != "cloud_world":
                    saved_local_name = f"{self.server_world_name}_архив_сервера_{ts_str}.zip"
                else:
                    saved_local_name = f"cloud_world_предыдущий_глобальный_сервер_{ts_str}.zip"
                dest_path = self.env.saves_dir / saved_local_name
                self.env.saves_dir.mkdir(parents=True, exist_ok=True)
                hub_client.download_save(dest_path)
                logger.info(f"Локальная копия мира сохранена в {dest_path}")

            self._schedule_ui(lambda: self.set_status("Перемещение мира в архив сервера...", color=COLOR_INFO))
            player_name = cfg.player_name or self.player_name or "Player"
            res = hub_client.reset_server_world(player_name=player_name)
            logger.info(f"Серверный мир архивирован: {res}")

            self.server_has_save = False
            self.server_world_name = None
            self.server_world_display = None

            # Обновляем локальный список сохранений
            local_saves = []
            try:
                local_saves = self.env.list_local_saves(include_cloud_world=False)
            except Exception:
                pass

            def _on_success():
                self.is_activating = False
                if hasattr(self, "archive_world_btn"):
                    self.archive_world_btn.pack_forget()
                self._transition_to_empty_server_world_selection(cfg, local_saves)
                if saved_local_name:
                    msg = (
                        f"Копия сохранена как '{saved_local_name}'. "
                        "Мир снят с сервера в архив. Выберите новый мир для загрузки."
                    )
                else:
                    msg = "Мир успешно перемещен в архив сервера. Выберите локальный мир для загрузки или начните игру."
                self.set_status(msg, color=COLOR_SUCCESS)

            self._schedule_ui(_on_success)

        except Exception as e:
            logger.error(f"Ошибка архивации мира: {e}")

            def _on_error(err=e):
                self.is_activating = False
                self.action_btn.config(state="normal")
                if hasattr(self, "archive_world_btn"):
                    self.archive_world_btn.config(state="normal")
                self.set_status(f"Ошибка: {err}", color=COLOR_ERROR)

            self._schedule_ui(_on_error)

    def _on_publish_success(self) -> None:
        """Обрабатывает успешную публикацию мира."""
        if hasattr(self, "progress_bar"):
            self.progress_bar["value"] = 100
        if hasattr(self, "progress_speed_lbl"):
            self.progress_speed_lbl.config(text="Завершено! Запуск игры...")
        self.set_status("Мир успешно опубликован на сервер! Запуск игры...", color=COLOR_SUCCESS)
        self.root.after(800, self._close_window_cleanly)

    def _transition_to_empty_server_world_selection(
        self, cfg: ClientConfig, local_saves: list[Path]
    ) -> None:
        """Переключает окно на Шаг 2: выбор локального мира для загрузки на пустой сервер."""
        self.step = "select_initial_world"
        self.is_activating = False
        self.existing_config = cfg
        self.result_config = cfg
        self.is_already_configured = True

        # Скрываем кнопку архивации, так как сервер уже пуст
        if hasattr(self, "archive_world_btn"):
            self.archive_world_btn.pack_forget()

        # Скрываем карточку ввода кода
        if self.inputs_card:
            self.inputs_card.pack_forget()

        # Формируем список сохранений без заглушки сервера
        options = [s.name for s in local_saves] + [SKIP_WORLD_UPLOAD_OPTION]
        self.world_options = options
        self.world_combo["values"] = options
        self.world_combo_var.set(options[0])

        if self.world_title_lbl:
            self.world_title_lbl.config(
                text="На сервере еще нет сохраненного мира. Выберите ваш мир для загрузки:"
            )

        # Показываем карточку выбора мира
        if self.world_card and self.world_card.winfo_manager() != "pack":
            self.world_card.pack(fill=self.tk.X, pady=(0, 10))

        # Обновляем кнопку действия и статус
        self.action_btn.config(
            text="Загрузить мир на сервер и играть",
            state="normal",
        )
        self.set_status(
            "Сервер подключен! Выберите мир, который нужно загрузить на сервер.",
            color=COLOR_SUCCESS,
        )

    def _activate_worker(
        self,
        code: str,
        passphrase: str,
        selected_world: Optional[str] = None,
    ) -> None:
        """Выполняет сетевой запрос активации в фоновом потоке."""
        try:
            # Проверяем http_client: если его base_url не совпадает с адресом сервера из инвайта,
            # используем прямое подключение к серверу инвайта (None)
            target_http_client = self.http_client
            if target_http_client is not None:
                try:
                    from src.common.invite_codec import decode_invite_code
                    dec_preview = decode_invite_code(code, passphrase)
                    inv_url = str(dec_preview.get("hub_http_url", "")).rstrip("/")
                    client_base = str(getattr(target_http_client, "base_url", "")).rstrip("/")
                    if client_base and inv_url and client_base != inv_url:
                        logger.info(
                            f"Базовый адрес клиента ({client_base}) не совпадает с адресом инвайта ({inv_url}). "
                            f"Переключение на прямое подключение."
                        )
                        target_http_client = None
                except Exception:
                    pass

            cfg = HubClient.activate_with_invite_code(
                invite_code=code,
                passphrase=passphrase,
                player_name=self.player_name,
                config_path=self.config_path,
                http_client=target_http_client,
            )

            # Если передан callback установщика
            if self.installer_mode and self.on_install_callback and self.game_path:
                self.on_install_callback(self.game_path, self.player_name, cfg)

            # Проверяем наличие сохранения на сервере
            server_has_save = False
            try:
                hub_client = HubClient(config=cfg, http_client=self.http_client)
                st = hub_client.get_status()
                self._apply_server_status_dict(st)
                self._schedule_ui(self._update_server_status_widgets)
                server_has_save = self.server_has_save
            except Exception as st_err:
                logger.warning(f"Не удалось проверить статус сохранения на сервере: {st_err}")

            self.server_has_save = server_has_save
            self.result_config = cfg
            self.existing_config = cfg
            self.is_already_configured = True

            # Случай А1: на сервере уже есть мир -> скачиваем актуальный сейв со шкалой прогресса перед запуском
            if server_has_save:
                logger.info("На сервере уже есть активный мир. Проверяем необходимость загрузки локальной копии...")
                server_sha = st.get("sha256") or st.get("save", {}).get("sha256")
                local_save = self.env.saves_dir / self.env.cloud_save_name
                needs_download = True
                if local_save.is_file() and server_sha:
                    try:
                        from src.client.hub_client import compute_file_sha256
                        if compute_file_sha256(local_save) == server_sha:
                            needs_download = False
                    except Exception:
                        needs_download = True

                if needs_download:
                    try:
                        logger.info("Скачивание актуального сохранения мира через окно активации...")
                        self._schedule_ui(lambda: self.show_progress("Скачивание мира с сервера..."))
                        self._schedule_ui(lambda: self.set_status("Загрузка актуального сохранения из облака...", color=COLOR_INFO))

                        def _progress_cb(downloaded: int, total: int, speed: float) -> None:
                            self._schedule_ui(lambda: self.update_progress(downloaded, total, speed))

                        self.env.saves_dir.mkdir(parents=True, exist_ok=True)
                        hub_client.download_save(
                            target_path=local_save,
                            expected_sha=server_sha,
                            progress_callback=_progress_cb,
                        )
                        logger.info(f"Мир успешно сохранен в {local_save}")
                    except Exception as dl_err:
                        logger.warning(f"Предварительная загрузка сохранения в GUI не удалась ({dl_err}), делегирование оркестратору.")

                self._schedule_ui(self._on_activation_success)
                return

            # Случай А2: сервер пустой -> проверяем локальные сохранения
            local_saves = []
            try:
                local_saves = self.env.list_local_saves(include_cloud_world=False)
            except Exception as l_err:
                logger.warning(f"Ошибка получения списка сохранений: {l_err}")

            if not local_saves:
                logger.info("Локальных сохранений нет. Запуск игры для создания нового мира.")
                self._schedule_ui(self._on_activation_success)
                return

            # Есть локальные сохранения -> переход на Шаг 2 в UI-потоке
            logger.info(f"Сервер пуст, найдено {len(local_saves)} локальных сохранений. Переход на Шаг 2.")
            self._schedule_ui(lambda: self._transition_to_empty_server_world_selection(cfg, local_saves))

        except Exception as exc:
            err = exc
            logger.warning(f"Ошибка при активации инвайт-кода: {err}")
            self._schedule_ui(lambda e=err: self._on_activation_error(e))

    def _on_activation_success(self, cfg: Optional[ClientConfig] = None) -> None:
        """Обрабатывает успешную активацию."""
        if cfg is not None:
            self.result_config = cfg
        self.set_status("Готово! Сервер подключен. Запуск игры...", color=COLOR_SUCCESS)
        self.root.after(600, self._close_window_cleanly)

    def _on_activation_error(self, exc: Exception) -> None:
        """Отображает понятную ошибку пользователю."""
        self.is_activating = False
        self.hide_progress()
        self.action_btn.config(state="normal")

        err_str = str(exc)
        if "not_active_host" in err_str or "сессия Хоста истекла" in err_str:
            msg = "Ошибка: Время ожидания загрузки на сервере истекло. Попробуйте нажать кнопку еще раз."
        elif "403" in err_str or "Forbidden" in err_str:
            msg = "Ошибка: Доступ запрещен сервером (возможно, билет аннулирован)."
        elif "404" in err_str or "410" in err_str or "истек" in err_str or "использован" in err_str:
            msg = "Ошибка: Код приглашения не найден, истек или уже был использован."
        elif "Decryption" in err_str or "MAC" in err_str or "padding" in err_str or "Неверное секретное слово" in err_str:
            msg = "Ошибка: Неверное секретное слово или поврежденный Код Дружбы."
        elif "ConnectError" in err_str or "ConnectionRefused" in err_str or "timeout" in err_str:
            msg = "Ошибка сети: Сервер-сейф недоступен. Проверьте интернет-соединение."
        elif "Сейчас на сервере играет" in err_str:
            msg = err_str
        else:
            msg = f"Ошибка подключения: {err_str}"

        self.set_status(msg, color=COLOR_ERROR)


def run_first_time_setup_gui(
    env: Optional[FactorioEnvironment] = None,
    config_path: Optional[Path | str] = None,
    installer_mode: bool = False,
    http_client: Optional[Any] = None,
    on_install_callback: Optional[Callable[[Path, str, ClientConfig], None]] = None,
    on_uninstall_callback: Optional[Callable[[Path], Any]] = None,
    prompt_message: Optional[str] = None,
    existing_config: Optional[ClientConfig] = None,
    use_subprocess: bool = False,
    auto_connect: bool = False,
) -> Optional[ClientConfig]:
    """Безопасно запускает графическое окно первичной настройки.

    При отсутствии графического дисплея (headless CI / сервер без X11/Wayland)
    перехватывает TclError и возвращает None с предупреждением в лог,
    предотвращая аварийное завершение процесса.

    Args:
        env: Окружение Factorio.
        config_path: Путь к сохраняемому factorio_sync_config.json.
        installer_mode: Флаг режима установщика.
        http_client: Опциональный клиент httpx.
        on_install_callback: Callback для установки перехватчика.
        on_uninstall_callback: Callback для деинсталляции.
        prompt_message: Текст информационного баннера в окне.
        existing_config: Уже существующая конфигурация (при повторном открытии по Shift).
        use_subprocess: Если True, окно запускается в отдельном дочернем процессе.
        auto_connect: Режим автоматического фонового подключения со спиннером.

    Returns:
        Экземпляр сконфигурированного ClientConfig, либо None если окно закрыто
        пользователем или графический дисплей недоступен.
    """
    # Если запрошен запуск в изолированном дочернем процессе (macOS window hang prevention)
    if (
        use_subprocess
        and http_client is None
        and on_install_callback is None
        and on_uninstall_callback is None
        and not os.getenv("FACTORIO_HEADLESS_MOCK_INVITE")
    ):
        return run_setup_gui_in_subprocess(
            env=env,
            config_path=config_path,
            installer_mode=installer_mode,
            prompt_message=prompt_message,
            existing_config=existing_config,
            auto_connect=auto_connect,
        )

    # Проверка переменной окружения для headless-тестов с мок-активацией
    mock_invite = os.getenv("FACTORIO_HEADLESS_MOCK_INVITE")
    mock_pass = os.getenv("FACTORIO_HEADLESS_MOCK_PASSPHRASE")
    if mock_invite and mock_pass:
        logger.info("Обнаружена переменная FACTORIO_HEADLESS_MOCK_INVITE: запуск эмуляции ввода GUI.")
        target_env = env or FactorioEnvironment()
        pname = target_env.resolve_or_init_player_username()
        dest_cfg = config_path or target_env.sync_config_path
        return HubClient.activate_with_invite_code(
            invite_code=mock_invite,
            passphrase=mock_pass,
            player_name=pname,
            config_path=dest_cfg,
            http_client=http_client,
        )

    try:
        import tkinter as tk
    except ImportError as imp_err:
        logger.warning(f"Пакет tkinter недоступен в текущем Python: {imp_err}. Пропуск GUI.")
        return existing_config if (existing_config and existing_config.is_configured()) else None

    try:
        root = tk.Tk()
    except Exception as tcl_err:
        logger.warning(
            f"Графический дисплей недоступен ({tcl_err}). "
            f"Пропуск GUI-окна первого запуска."
        )
        return existing_config if (existing_config and existing_config.is_configured()) else None

    try:
        app = FirstRunSetupWindow(
            root=root,
            env=env,
            config_path=config_path,
            installer_mode=installer_mode,
            http_client=http_client,
            on_install_callback=on_install_callback,
            on_uninstall_callback=on_uninstall_callback,
            prompt_message=prompt_message,
            existing_config=existing_config,
            auto_connect=auto_connect,
        )
        root.mainloop()
        if getattr(app, "was_uninstalled", False):
            return ClientConfig(was_uninstalled=True)
        if app.result_config is not None:
            return app.result_config
        return ClientConfig(user_aborted=True)
    except Exception as fatal_gui_err:
        logger.error(f"Критическая ошибка при выполнении GUI первого запуска: {fatal_gui_err}")
        try:
            root.withdraw()
            root.destroy()
        except Exception:
            pass
        return ClientConfig(user_aborted=True)


def run_setup_gui_subprocess_entry(args_file: Path | str, result_file: Path | str) -> int:
    """Точка входа для выполнения FirstRunSetupWindow в изолированном дочернем процессе.

    Args:
        args_file: JSON-файл с аргументами запуска.
        result_file: JSON-файл для записи результата работы.

    Returns:
        0 при успешном завершении, 1 при ошибке аргументов.
    """
    args_p = Path(args_file).resolve()
    res_p = Path(result_file).resolve()

    res_data: dict[str, Any] = {
        "status": "cancelled",
        "config": None,
        "was_uninstalled": False,
    }

    try:
        if not args_p.is_file():
            logger.error(f"Файл аргументов подпроцесса GUI не найден: {args_p}")
            res_p.write_text(json.dumps(res_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1

        args_raw = json.loads(args_p.read_text(encoding="utf-8"))
        custom_game_root = args_raw.get("custom_game_root")
        custom_user_data_dir = args_raw.get("custom_user_data_dir")
        config_path = args_raw.get("config_path")
        installer_mode = bool(args_raw.get("installer_mode", False))
        prompt_message = args_raw.get("prompt_message")
        existing_config_dict = args_raw.get("existing_config_dict")
        auto_connect = bool(args_raw.get("auto_connect", False))

        env = FactorioEnvironment(
            custom_game_root=Path(custom_game_root) if custom_game_root else None,
            custom_user_data_dir=Path(custom_user_data_dir) if custom_user_data_dir else None,
        )

        existing_config: Optional[ClientConfig] = None
        if existing_config_dict and isinstance(existing_config_dict, dict):
            try:
                import dataclasses
                valid_fields = {f.name for f in dataclasses.fields(ClientConfig)}
                filtered = {k: v for k, v in existing_config_dict.items() if k in valid_fields}
                existing_config = ClientConfig(**filtered)
            except Exception as e:
                logger.warning(f"Не удалось десериализовать existing_config в подпроцессе: {e}")

        cfg = run_first_time_setup_gui(
            env=env,
            config_path=config_path,
            installer_mode=installer_mode,
            http_client=None,
            on_install_callback=None,
            on_uninstall_callback=None,
            prompt_message=prompt_message,
            existing_config=existing_config,
            use_subprocess=False,
            auto_connect=auto_connect,
        )

        if cfg is not None:
            if getattr(cfg, "was_uninstalled", False):
                res_data["status"] = "uninstalled"
                res_data["was_uninstalled"] = True
                res_data["config"] = cfg.to_dict()
            elif getattr(cfg, "user_aborted", False):
                res_data["status"] = "aborted"
                res_data["user_aborted"] = True
                res_data["config"] = cfg.to_dict()
            else:
                res_data["status"] = "ok"
                res_data["config"] = cfg.to_dict()
        else:
            res_data["status"] = "aborted"
            res_data["user_aborted"] = True
            res_data["config"] = ClientConfig(user_aborted=True).to_dict()

    except Exception as exc:
        logger.exception(f"Критическая ошибка в дочернем процессе GUI: {exc}")
        res_data["status"] = "error"
        res_data["error"] = str(exc)
    finally:
        try:
            res_p.write_text(json.dumps(res_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as write_err:
            logger.error(f"Не удалось записать результат подпроцесса GUI в {res_p}: {write_err}")

    return 0


def run_setup_gui_in_subprocess(
    env: Optional[FactorioEnvironment] = None,
    config_path: Optional[Path | str] = None,
    installer_mode: bool = False,
    prompt_message: Optional[str] = None,
    existing_config: Optional[ClientConfig] = None,
    auto_connect: bool = False,
) -> Optional[ClientConfig]:
    """Запускает GUI первого запуска в отдельном изолированном дочернем процессе.

    Это устраняет зависание окна и статус 'Not Responding' на macOS:
    когда подпроцесс завершается, ядро операционной системы полностью закрывает окно
    и освобождает связь с WindowServer, а родительский процесс перехватчика
    остается чистым фоновым демоном без Cocoa/NSApplication.
    """
    target_env = env or FactorioEnvironment()
    target_config_path = str(config_path or target_env.sync_config_path)

    args_data: dict[str, Any] = {
        "custom_game_root": str(target_env.game_root) if target_env.game_root else None,
        "custom_user_data_dir": str(target_env.user_data_dir) if target_env.user_data_dir else None,
        "config_path": target_config_path,
        "installer_mode": bool(installer_mode),
        "prompt_message": prompt_message,
        "existing_config_dict": existing_config.to_dict() if existing_config else None,
        "auto_connect": bool(auto_connect),
    }

    with tempfile.TemporaryDirectory(prefix="factorio_gui_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        args_file = tmp_dir / "setup_args.json"
        result_file = tmp_dir / "setup_result.json"

        args_file.write_text(json.dumps(args_data, ensure_ascii=False, indent=2), encoding="utf-8")

        is_frozen = getattr(sys, "frozen", False)
        if is_frozen:
            cmd = [
                sys.executable,
                "--wrapper-internal-setup-gui",
                str(args_file),
                str(result_file),
            ]
        else:
            cmd = [
                sys.executable,
                "-m",
                "src.client.setup_gui",
                "--subprocess-args",
                str(args_file),
                "--subprocess-result",
                str(result_file),
            ]

        logger.info(f"Запуск изолированного подпроцесса GUI: {' '.join(cmd)}")
        try:
            sub_env = os.environ.copy()
            res = subprocess.run(cmd, env=sub_env, check=False)
            logger.info(f"Изолированный подпроцесс GUI завершился с кодом {res.returncode}")
        except Exception as proc_err:
            logger.error(f"Не удалось запустить подпроцесс GUI: {proc_err}")
            return existing_config if (existing_config and existing_config.is_configured()) else None

        if not result_file.is_file():
            logger.warning(f"Файл результата GUI не найден после завершения подпроцесса: {result_file}")
            return existing_config if (existing_config and existing_config.is_configured()) else None

        try:
            result_raw = json.loads(result_file.read_text(encoding="utf-8"))
            status = result_raw.get("status")
            cfg_dict = result_raw.get("config")
            was_uninstalled = bool(result_raw.get("was_uninstalled", False))
            user_aborted = bool(result_raw.get("user_aborted", False)) or (cfg_dict and bool(cfg_dict.get("user_aborted", False)))

            if was_uninstalled or status == "uninstalled":
                logger.info("Подпроцесс сообщил об успешной деинсталляции перехватчика.")
                return ClientConfig(was_uninstalled=True)

            if user_aborted or status == "aborted":
                logger.info("Подпроцесс сообщил об отмене запуска пользователем (нажат крестик окна).")
                return ClientConfig(user_aborted=True)

            if status == "ok" and cfg_dict:
                import dataclasses
                valid_fields = {f.name for f in dataclasses.fields(ClientConfig)}
                filtered = {k: v for k, v in cfg_dict.items() if k in valid_fields}
                return ClientConfig(**filtered)

            logger.info(f"Подпроцесс GUI завершился со статусом '{status}'.")
            return ClientConfig(user_aborted=True)
        except Exception as parse_err:
            logger.error(f"Ошибка при чтении результата подпроцесса GUI: {parse_err}")
            return existing_config if (existing_config and existing_config.is_configured()) else None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Factorio Setup GUI Subprocess")
    parser.add_argument("--subprocess-args", type=str, default=None)
    parser.add_argument("--subprocess-result", type=str, default=None)
    parsed_args, _ = parser.parse_known_args()

    if parsed_args.subprocess_args and parsed_args.subprocess_result:
        ret_code = run_setup_gui_subprocess_entry(
            Path(parsed_args.subprocess_args),
            Path(parsed_args.subprocess_result),
        )
        sys.exit(ret_code)
    else:
        res = run_first_time_setup_gui(use_subprocess=False)
        sys.exit(0 if res else 1)
