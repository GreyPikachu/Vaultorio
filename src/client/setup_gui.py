"""Графическое окно первой настройки Factorio Co-op (FirstRunSetupWindow).

Обеспечивает ввод одноразового Кода Дружбы (FCT1-...) и секретного слова,
автоматическое считывание никнейма из player-data.json, обмен инвайта
на постоянный device_token и сохранение factorio_sync_config.json.
При повторных запусках окно больше не вызывается.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.hub_client import HubClient

logger = logging.getLogger("factorio_client.setup_gui")

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

        self._init_window()
        self._build_ui()

    def _init_window(self) -> None:
        """Настраивает геометрические параметры и стили окна."""
        self.root.title("Factorio Co-op - Подключение к облачному миру")
        self.root.configure(bg=COLOR_BG)
        self.root.resizable(False, False)

        width = 540
        height = 580 if self.installer_mode else 480
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

        main_container = tk.Frame(self.root, bg=COLOR_BG, padx=20, pady=16)
        main_container.pack(fill=tk.BOTH, expand=True)

        # 1. Шапка (Заголовок и подзаголовок)
        header_frame = tk.Frame(main_container, bg=COLOR_BG)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        title_lbl = tk.Label(
            header_frame,
            text="Factorio Co-op - Подключение к облачному миру",
            font=("Helvetica", 13, "bold"),
            fg=COLOR_TEXT,
            bg=COLOR_BG,
        )
        title_lbl.pack(anchor="w")

        subtitle_lbl = tk.Label(
            header_frame,
            text="Синхронизация сохранений и P2P-кооператив без белых IP",
            font=("Helvetica", 9),
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_BG,
        )
        subtitle_lbl.pack(anchor="w", pady=(2, 0))

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
            text=f"Никнейм из Factorio (player-data.json): {self.player_name}",
            font=("Helvetica", 10, "bold"),
            fg=COLOR_ACCENT,
            bg=COLOR_CARD,
        )
        player_title_lbl.pack(anchor="w")

        player_sub_lbl = tk.Label(
            player_card,
            text="Никнейм подтягивается автоматически из настроек вашей игры Factorio",
            font=("Helvetica", 8),
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_CARD,
        )
        player_sub_lbl.pack(anchor="w", pady=(2, 0))

        # 3. Режим установщика: путь к игре
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

            browse_btn = tk.Button(
                path_row,
                text="Обзор...",
                font=("Helvetica", 9),
                bg=COLOR_SECONDARY,
                fg=COLOR_TEXT,
                activebackground=COLOR_SECONDARY_HOVER,
                activeforeground=COLOR_TEXT,
                relief=tk.FLAT,
                command=self._on_browse_game_dir,
                padx=8,
                pady=2,
            )
            browse_btn.pack(side=tk.RIGHT)

        # 4. Блок ввода Кода Дружбы и секретного слова
        inputs_card = tk.Frame(
            main_container,
            bg=COLOR_CARD,
            relief=tk.SOLID,
            bd=1,
            padx=12,
            pady=10,
        )
        inputs_card.pack(fill=tk.X, pady=(0, 10))

        # Поле 1: Одноразовый Код Дружбы
        code_lbl = tk.Label(
            inputs_card,
            text="Одноразовый Код Дружбы (FCT1-...):",
            font=("Helvetica", 9, "bold"),
            fg=COLOR_TEXT,
            bg=COLOR_CARD,
        )
        code_lbl.pack(anchor="w")

        code_row = tk.Frame(inputs_card, bg=COLOR_CARD)
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

        paste_btn = tk.Button(
            code_row,
            text="Вставить",
            font=("Helvetica", 9),
            bg=COLOR_SECONDARY,
            fg=COLOR_TEXT,
            activebackground=COLOR_SECONDARY_HOVER,
            activeforeground=COLOR_TEXT,
            relief=tk.FLAT,
            command=self._on_paste_code,
            padx=8,
            pady=2,
        )
        paste_btn.pack(side=tk.RIGHT)

        # Поле 2: Секретное слово
        pass_lbl = tk.Label(
            inputs_card,
            text="Секретное слово (пароль инвайта):",
            font=("Helvetica", 9, "bold"),
            fg=COLOR_TEXT,
            bg=COLOR_CARD,
        )
        pass_lbl.pack(anchor="w")

        pass_row = tk.Frame(inputs_card, bg=COLOR_CARD)
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
        self.toggle_pass_btn = tk.Button(
            pass_row,
            text="Показать",
            font=("Helvetica", 9),
            bg=COLOR_SECONDARY,
            fg=COLOR_TEXT,
            activebackground=COLOR_SECONDARY_HOVER,
            activeforeground=COLOR_TEXT,
            relief=tk.FLAT,
            command=self._on_toggle_pass,
            padx=8,
            pady=2,
        )
        self.toggle_pass_btn.pack(side=tk.RIGHT)

        # 5. Строка состояния (Status Bar)
        self.status_var = tk.StringVar(value="Введите полученный Код Дружбы и секретное слово")
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

        # 6. Кнопки действий
        btn_frame = tk.Frame(main_container, bg=COLOR_BG)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

        action_text = "Установить и подключить" if self.installer_mode else "Подключить и продолжить"
        self.action_btn = tk.Button(
            btn_frame,
            text=action_text,
            font=("Helvetica", 10, "bold"),
            bg=COLOR_ACCENT,
            fg="#ffffff",
            activebackground=COLOR_ACCENT_HOVER,
            activeforeground="#ffffff",
            relief=tk.FLAT,
            command=self._on_activate_click,
            pady=7,
        )
        self.action_btn.pack(fill=tk.X, pady=(0, 6))

        # Дополнительные кнопки
        if self.installer_mode:
            uninstall_btn = tk.Button(
                btn_frame,
                text="Удалить перехватчик (Вернуть чистую игру)",
                font=("Helvetica", 9),
                bg=COLOR_SECONDARY,
                fg=COLOR_TEXT_MUTED,
                activebackground=COLOR_SECONDARY_HOVER,
                activeforeground=COLOR_TEXT,
                relief=tk.FLAT,
                command=self._on_uninstall_click,
                pady=4,
            )
            uninstall_btn.pack(fill=tk.X)
        else:
            cancel_btn = tk.Button(
                btn_frame,
                text="Запустить игру автономно (без сервера)",
                font=("Helvetica", 9),
                bg=COLOR_SECONDARY,
                fg=COLOR_TEXT_MUTED,
                activebackground=COLOR_SECONDARY_HOVER,
                activeforeground=COLOR_TEXT,
                relief=tk.FLAT,
                command=self._on_cancel_click,
                pady=4,
            )
            cancel_btn.pack(fill=tk.X)

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
        if not self.game_path:
            self.set_status("Укажите папку с игрой для удаления перехватчика", color=COLOR_ERROR)
            return

        from tkinter import messagebox

        confirm = messagebox.askyesno(
            "Подтверждение удаления",
            "Вы действительно хотите удалить перехватчик и вернуть оригинальную чистую игру Factorio?",
        )
        if not confirm:
            return

        if self.on_uninstall_callback:
            try:
                res = self.on_uninstall_callback(self.game_path)
                msg = res.get("message", "Перехватчик успешно удален") if isinstance(res, dict) else "Успешно удалено"
                messagebox.showinfo("Успешно", msg)
                self.root.destroy()
            except Exception as e:
                logger.error(f"Ошибка при удалении перехватчика: {e}")
                self.set_status(f"Ошибка удаления: {e}", color=COLOR_ERROR)

    def _on_activate_click(self) -> None:
        """Валидирует ввод и запускает фоновую активацию билета."""
        if self.is_activating:
            return

        code = self.code_var.get().strip()
        passphrase = self.pass_var.get().strip()

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
            args=(code, passphrase),
            daemon=True,
            name="FirstRunActivationThread",
        )
        worker.start()

    def _activate_worker(self, code: str, passphrase: str) -> None:
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

            self.result_config = cfg
            self.root.after(0, self._on_activation_success)
        except Exception as exc:
            logger.warning(f"Ошибка при активации инвайт-кода: {exc}")
            self.root.after(0, lambda: self._on_activation_error(exc))

    def _on_activation_success(self) -> None:
        """Обрабатывает успешную активацию."""
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
        return None

    try:
        root = tk.Tk()
    except Exception as tcl_err:
        logger.warning(
            f"Графический дисплей недоступен ({tcl_err}). "
            f"Пропуск GUI-окна первого запуска."
        )
        return None

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
        )
        root.mainloop()
        return app.result_config
    except Exception as fatal_gui_err:
        logger.error(f"Критическая ошибка при выполнении GUI первого запуска: {fatal_gui_err}")
        try:
            root.destroy()
        except Exception:
            pass
        return None
