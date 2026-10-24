"""Графическое окно первой настройки Factorio Co-op (FirstRunSetupWindow).

Обеспечивает ввод одноразового Кода Дружбы (FCT1-...) и секретного слова,
автоматическое считывание никнейма из player-data.json, обмен инвайта
на постоянный device_token и сохранение factorio_sync_config.json.
При повторных запусках окно больше не вызывается.
"""

from __future__ import annotations

import logging
import os
import shutil
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
            backup_save = env.saves_dir / f"{canonical_save.stem}_prev_local.zip"
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

    try:
        upload_res = hub_client.upload_save(
            file_path=canonical_save,
            session_token=session_token,
            release_on_success=True,
            mod_list=env.get_active_mods_manifest(),
        )
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

        self.step: str = "manage_world" if self.is_already_configured else "enter_invite"
        self.server_has_save: Optional[bool] = None
        self.inputs_card: Optional[Any] = None
        self.world_card: Optional[Any] = None
        self.world_title_lbl: Optional[Any] = None

        if self.is_already_configured and self.existing_config:
            try:
                hub_client = HubClient(config=self.existing_config, http_client=self.http_client)
                st = hub_client.get_status()
                self.server_has_save = bool(st.get("save_exists") or st.get("save", {}).get("exists"))
            except Exception as e:
                logger.debug(f"Не удалось получить статус сервера при открытии окна: {e}")
                self.server_has_save = True

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

        self._configure_dark_theme()
        self._init_window()
        self._build_ui()

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
        except Exception as e:
            logger.warning(f"Ошибка настройки темного стиля ttk: {e}")

    def _init_window(self) -> None:
        """Настраивает геометрические параметры и стили окна."""
        self.root.title("Factorio Co-op - Подключение к облачному миру")
        self.root.configure(bg=COLOR_BG)
        self.root.resizable(False, False)
        self.root.bind("<<Wakeup>>", lambda e: None)

    def _schedule_ui(self, callback: Any) -> None:
        """Безопасно ставит вызов в очередь UI-потока и будит Cocoa event loop на macOS."""
        self.root.after(0, callback)
        try:
            self.root.event_generate("<<Wakeup>>", when="tail")
        except Exception:
            pass

        width = 540
        height = 640 if self.installer_mode else 540
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

        # 2. Информационный блок игрока
        player_card = tk.Frame(
            main_container,
            bg=COLOR_CARD,
            relief=tk.SOLID,
            bd=1,
            padx=12,
            pady=8,
        )
        player_card.pack(fill=tk.X, pady=(0, 10))

        player_title_lbl = tk.Label(
            player_card,
            text=f"Игрок: {self.player_name}",
            font=("Helvetica", 10, "bold"),
            fg=COLOR_ACCENT,
            bg=COLOR_CARD,
        )
        player_title_lbl.pack(anchor="w")

        # 3. Секция выбора активного мира сервера
        self.world_card = tk.Frame(
            main_container,
            bg=COLOR_CARD,
            relief=tk.SOLID,
            bd=1,
            padx=12,
            pady=8,
        )

        self.world_title_lbl = tk.Label(
            self.world_card,
            text="Активный мир на сервере:",
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

        self.code_var = tk.StringVar()
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

        # 6. Строка состояния (Status Bar)
        initial_status = (
            "Сервер уже подключен. Выберите мир и нажмите «Применить и запустить»."
            if self.is_already_configured
            else "Введите полученный Код Дружбы и секретное слово"
        )
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
        self.status_lbl.pack(fill=tk.X, pady=(4, 10))

        # 7. Кнопки действий
        btn_frame = tk.Frame(main_container, bg=COLOR_BG)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

        if self.installer_mode:
            action_text = "Установить и подключить"
        elif self.is_already_configured:
            action_text = "Применить и запустить игру"
        else:
            action_text = "Подключить и продолжить"

        self.action_btn = ttk.Button(
            btn_frame,
            text=action_text,
            style="Accent.TButton",
            command=self._on_activate_click,
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

    def _on_cancel_click(self) -> None:
        """Закрывает окно без сохранения настроек (автономный запуск)."""
        logger.info("Пользователь закрыл окно первичной настройки (автономный режим).")
        self.result_config = None
        self.root.destroy()

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
            self.root.destroy()
        except Exception as e:
            logger.error(f"Ошибка при удалении перехватчика: {e}")
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
                self.root.after(300, self.root.destroy)
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
                self.root.after(300, self.root.destroy)
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
            hub_client = HubClient(config=cfg, http_client=self.http_client)
            save_path = self.env.saves_dir / save_name
            publish_selected_world_to_server(
                env=self.env,
                hub_client=hub_client,
                source_save_path=save_path,
                player_name=self.player_name,
            )
            self.result_config = cfg
            self._schedule_ui(self._on_publish_success)
        except Exception as exc:
            logger.warning(f"Ошибка при публикации выбранного мира: {exc}")
            self._schedule_ui(lambda: self._on_activation_error(exc))

    def _on_publish_success(self) -> None:
        """Обрабатывает успешную публикацию мира."""
        self.set_status("Мир успешно опубликован на сервер! Запуск игры...", color=COLOR_SUCCESS)
        self.root.after(600, self.root.destroy)

    def _transition_to_empty_server_world_selection(
        self, cfg: ClientConfig, local_saves: list[Path]
    ) -> None:
        """Переключает окно на Шаг 2: выбор локального мира для загрузки на пустой сервер."""
        self.step = "select_initial_world"
        self.is_activating = False
        self.existing_config = cfg
        self.result_config = cfg
        self.is_already_configured = True

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
            cfg = HubClient.activate_with_invite_code(
                invite_code=code,
                passphrase=passphrase,
                player_name=self.player_name,
                config_path=self.config_path,
                http_client=self.http_client,
            )

            # Если передан callback установщика
            if self.installer_mode and self.on_install_callback and self.game_path:
                self.on_install_callback(self.game_path, self.player_name, cfg)

            # Проверяем наличие сохранения на сервере
            server_has_save = False
            try:
                hub_client = HubClient(config=cfg, http_client=self.http_client)
                st = hub_client.get_status()
                server_has_save = bool(st.get("save_exists") or st.get("save", {}).get("exists"))
            except Exception as st_err:
                logger.warning(f"Не удалось проверить статус сохранения на сервере: {st_err}")

            self.server_has_save = server_has_save
            self.result_config = cfg
            self.existing_config = cfg
            self.is_already_configured = True

            # Случай А1: на сервере уже есть мир -> мгновенный запуск
            if server_has_save:
                logger.info("На сервере уже есть активный мир. Селектор пропускается.")
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
            logger.warning(f"Ошибка при активации инвайт-кода: {exc}")
            self._schedule_ui(lambda: self._on_activation_error(exc))

    def _on_activation_success(self, cfg: Optional[ClientConfig] = None) -> None:
        """Обрабатывает успешную активацию."""
        if cfg is not None:
            self.result_config = cfg
        self.set_status("Готово! Сервер подключен. Запуск игры...", color=COLOR_SUCCESS)
        self.root.after(600, self.root.destroy)

    def _on_activation_error(self, exc: Exception) -> None:
        """Отображает понятную ошибку пользователю."""
        self.is_activating = False
        self.action_btn.config(state="normal")

        err_str = str(exc)
        if "403" in err_str or "Forbidden" in err_str:
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

    Returns:
        Экземпляр сконфигурированного ClientConfig, либо None если окно закрыто
        пользователем или графический дисплей недоступен.
    """
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
        )
        root.mainloop()
        if getattr(app, "was_uninstalled", False):
            return ClientConfig(was_uninstalled=True)
        return app.result_config
    except Exception as fatal_gui_err:
        logger.error(f"Критическая ошибка при выполнении GUI первого запуска: {fatal_gui_err}")
        try:
            root.destroy()
        except Exception:
            pass
        return existing_config if (existing_config and existing_config.is_configured()) else None
