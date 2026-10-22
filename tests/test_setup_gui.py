"""Тесты для графического меню первого запуска Factorio Co-op (src/client/setup_gui.py).

Покрывает чек-лист обязательного тест-плана Задачи 015:
1. Инициализация окна FirstRunSetupWindow и динамическое отображение ника из player-data.json.
2. Функционал кнопки «Вставить» из буфера обмена.
3. Переключатель видимости пароля («Показать» / «Скрыть»).
4. Валидация входных данных (пустые поля, префикс FCT1-).
5. Успешная сетевая активация билета и сохранение конфигурации.
6. Обработка краевых ошибок сети и аутентификации (401, 403, 404, 410, обрыв сети).
7. Graceful fallback в headless-среде (TclError -> None).
8. Headless-эмуляция ввода через переменную окружения FACTORIO_HEADLESS_MOCK_INVITE.
9. Режим установщика (installer_mode=True): выбор папки игры и деинсталляция.
10. Интеграция в SessionOrchestrator: вызов GUI при первом запуске (not is_configured).
11. Однократность вызова: при последующих запусках (is_configured == True) GUI НЕ вызывается.
12. Реактивация при 403 Forbidden (токен отозван): сброс токена и повторный запуск GUI.
13. Проверка флага --noconsole (--windowed) в скрипте сборки build/build_client.py.
14. Проверка строгого запрета на эмодзи и смайлики во всем модуле GUI.
"""

from __future__ import annotations

import os
import re
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.hub_client import HubClient
from src.client.session_orchestrator import RoleType, SessionOrchestrator
from src.client.setup_gui import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    FirstRunSetupWindow,
    run_first_time_setup_gui,
)


class TestFirstRunSetupWindow(unittest.TestCase):
    """Модульные тесты интерфейса и логики FirstRunSetupWindow."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_gui_")).resolve()
        self.user_data = (self.temp_dir / "userdata").resolve()
        self.game_root = (self.temp_dir / "gameroot").resolve()
        self.user_data.mkdir(parents=True)
        self.game_root.mkdir(parents=True)

        self.env = FactorioEnvironment(
            custom_game_root=self.game_root,
            custom_user_data_dir=self.user_data,
        )
        self.env.set_player_username("PikachuEngineer")

        # Создаем tk.Tk в скрытом режиме без вывода на экран
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_initialization_and_player_detection(self):
        """Проверка инициализации интерфейса и автоматического считывания ника из player-data.json."""
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=False,
        )

        # Никнейм должен быть автоматически прочитан из player-data.json
        self.assertEqual(app.player_name, "PikachuEngineer")
        self.assertEqual(self.root.title(), "Factorio Co-op - Подключение к облачному миру")

        # Поля ввода
        self.assertEqual(app.code_var.get(), "")
        self.assertEqual(app.pass_var.get(), "")
        self.assertEqual(app.pass_entry.cget("show"), "*")
        self.assertFalse(app.installer_mode)
        self.assertIsNone(app.result_config)

    def test_02_paste_from_clipboard(self):
        """Проверка работы кнопки «Вставить» из системного буфера обмена."""
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=False,
        )

        # Эмулируем содержимое буфера обмена
        self.root.clipboard_clear()
        self.root.clipboard_append("  FCT1-SAMPLE-INVITE-TOKEN  ")

        app._on_paste_code()

        # Текст должен быть очищен от пробелов и помещен в code_var
        self.assertEqual(app.code_var.get(), "FCT1-SAMPLE-INVITE-TOKEN")

    def test_03_toggle_password_visibility(self):
        """Проверка переключателя «Показать» / «Скрыть» в поле секретного слова."""
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=False,
        )

        self.assertFalse(app.show_pass)
        self.assertEqual(app.pass_entry.cget("show"), "*")

        # Нажатие «Показать»
        app._on_toggle_pass()
        self.assertTrue(app.show_pass)
        self.assertEqual(app.pass_entry.cget("show"), "")
        self.assertEqual(app.toggle_pass_btn.cget("text"), "Скрыть")

        # Повторное нажатие возвращает «*»
        app._on_toggle_pass()
        self.assertFalse(app.show_pass)
        self.assertEqual(app.pass_entry.cget("show"), "*")
        self.assertEqual(app.toggle_pass_btn.cget("text"), "Показать")

    def test_04_validation_empty_and_invalid_fields(self):
        """Проверка валидации: пустые поля и отсутствие обязательного префикса FCT1-."""
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=False,
        )

        # 1. Пустой код
        app.code_var.set("")
        app.pass_var.set("secret")
        app._on_activate_click()
        self.assertIn("Пожалуйста, введите Код Дружбы", app.status_var.get())
        self.assertFalse(app.is_activating)

        # 2. Неверный префикс
        app.code_var.set("INVALID-PREFIX-123")
        app._on_activate_click()
        self.assertIn("FCT1-", app.status_var.get())
        self.assertFalse(app.is_activating)

        # 3. Пустой пароль
        app.code_var.set("FCT1-VALID-PREFIX")
        app.pass_var.set("")
        app._on_activate_click()
        self.assertIn("секретное слово", app.status_var.get())
        self.assertFalse(app.is_activating)

    @patch("src.client.setup_gui.HubClient.activate_with_invite_code")
    def test_05_activation_success(self, mock_activate):
        """Проверка успешной активации билета, сохранения конфига и закрытия окна."""
        mock_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            hub_stun_host="127.0.0.1",
            hub_stun_port=14197,
            player_name="PikachuEngineer",
            device_token="dev_tok_success_999",
            device_id="dev_id_abc",
        )
        mock_activate.return_value = mock_cfg

        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=False,
        )

        app.code_var.set("FCT1-TOKEN-123")
        app.pass_var.set("mypassword")

        # Прямой вызов воркера
        app._activate_worker("FCT1-TOKEN-123", "mypassword")

        mock_activate.assert_called_once_with(
            invite_code="FCT1-TOKEN-123",
            passphrase="mypassword",
            player_name="PikachuEngineer",
            config_path=app.config_path,
            http_client=None,
        )
        self.assertEqual(app.result_config, mock_cfg)

        # Вызов успеха
        app._on_activation_success()
        self.assertIn("Готово! Сервер подключен", app.status_var.get())

    def test_06_activation_error_handling(self):
        """Проверка корректной локализации и вывода различных ошибок сервера."""
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=False,
        )

        # 1. 403 Forbidden
        app._on_activation_error(Exception("HTTPStatusError 403 Forbidden"))
        self.assertIn("Доступ запрещен сервером", app.status_var.get())
        self.assertEqual(app.action_btn.cget("state"), "normal")

        # 2. 404 Not Found / Истек
        app._on_activation_error(Exception("Билет приглашения уже использован"))
        self.assertIn("истек или уже был использован", app.status_var.get())

        # 3. Неверное секретное слово
        app._on_activation_error(Exception("Decryption error: invalid padding or MAC"))
        self.assertIn("Неверное секретное слово", app.status_var.get())

        # 4. Обрыв связи
        app._on_activation_error(Exception("ConnectError: connection refused"))
        self.assertIn("Сервер-сейф недоступен", app.status_var.get())

    def test_07_installer_mode_ui_and_callbacks(self):
        """Проверка работы FirstRunSetupWindow в режиме установщика (installer_mode=True)."""
        mock_install = MagicMock()
        mock_uninstall = MagicMock(return_value={"message": "Успешно удалено"})

        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=True,
            on_install_callback=mock_install,
            on_uninstall_callback=mock_uninstall,
        )

        self.assertTrue(app.installer_mode)
        self.assertEqual(app.action_btn.cget("text"), "Установить и подключить")
        self.assertEqual(app.game_path.resolve(), self.game_root.resolve())

        # Вызов деинсталляции с подтверждением
        with patch("tkinter.messagebox.askyesno", return_value=True), \
             patch("tkinter.messagebox.showinfo"):
            app._on_uninstall_click()
            mock_uninstall.assert_called_once_with(self.game_root)


class TestSetupGUIRunner(unittest.TestCase):
    """Тестирование функции run_first_time_setup_gui и граничных условий окружения."""

    def test_08_run_first_time_setup_gui_tcl_error_fallback(self):
        """В headless-среде без графического сервера TclError безопасно перехватывается с возвратом None."""
        with patch("tkinter.Tk", side_effect=Exception("no display name and no $DISPLAY environment variable")):
            result = run_first_time_setup_gui()
            self.assertIsNone(result)

    def test_09_headless_mock_env_variable(self):
        """При наличии переменной FACTORIO_HEADLESS_MOCK_INVITE выполняется headless-активация без GUI."""
        mock_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="headless_mock_token_123",
        )

        with patch.dict(os.environ, {
            "FACTORIO_HEADLESS_MOCK_INVITE": "FCT1-MOCK-CODE",
            "FACTORIO_HEADLESS_MOCK_PASSPHRASE": "mockpass",
        }), patch("src.client.setup_gui.HubClient.activate_with_invite_code", return_value=mock_cfg) as mock_act:
            res = run_first_time_setup_gui()
            self.assertEqual(res, mock_cfg)
            mock_act.assert_called_once()


class TestSessionOrchestratorGUIIntegration(unittest.TestCase):
    """Интеграционные тесты вызова GUI первого запуска из SessionOrchestrator."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_orch_gui_"))
        self.user_data = self.temp_dir / "userdata"
        self.game_root = self.temp_dir / "gameroot"
        self.user_data.mkdir(parents=True)
        self.game_root.mkdir(parents=True)

        self.env = FactorioEnvironment(
            custom_game_root=self.game_root,
            custom_user_data_dir=self.user_data,
        )
        self.env.set_player_username("TesterPikachu")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_10_first_run_invokes_gui_when_unconfigured(self):
        """При первом запуске (device_token пуст) SessionOrchestrator вызывает setup_gui_provider."""
        unconfigured_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="",
        )
        self.assertFalse(unconfigured_cfg.is_configured())

        activated_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="dev_tok_activated_123",
            player_name="TesterPikachu",
        )

        mock_gui_provider = MagicMock(return_value=activated_cfg)
        mock_hub = MagicMock(spec=HubClient)
        mock_hub.get_status.return_value = {"state": "hosting_active", "current_host": "Alex"}

        orch = SessionOrchestrator(
            config=unconfigured_cfg,
            env=self.env,
            hub_client=mock_hub,
            setup_gui_provider=mock_gui_provider,
        )

        with patch("src.client.session_orchestrator.show_role_banner"), \
             patch("src.client.session_orchestrator.copy_to_clipboard"), \
             patch("src.client.session_orchestrator.GuestP2PProxy"):
            report = orch.prepare_session()

        mock_gui_provider.assert_called_once()
        self.assertEqual(orch.role, RoleType.GUEST)
        self.assertEqual(orch.config.device_token, "dev_tok_activated_123")

    def test_11_first_run_cancelled_leads_to_offline(self):
        """Если пользователь закрыл окно GUI без ввода кода, игра запускается в режиме OFFLINE."""
        unconfigured_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="",
        )

        # Пользователь закрыл окно -> возвращается None
        mock_gui_provider = MagicMock(return_value=None)
        mock_hub = MagicMock(spec=HubClient)

        orch = SessionOrchestrator(
            config=unconfigured_cfg,
            env=self.env,
            hub_client=mock_hub,
            setup_gui_provider=mock_gui_provider,
        )

        report = orch.prepare_session()
        self.assertEqual(orch.role, RoleType.OFFLINE)
        self.assertEqual(report["reason"], "setup_cancelled_by_user")

    def test_12_subsequent_runs_never_call_gui(self):
        """Во 2-й и последующие разы (is_configured == True) GUI-окно настройки НЕ вызывается."""
        configured_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="already_configured_token_555",
            player_name="TesterPikachu",
        )
        self.assertTrue(configured_cfg.is_configured())

        mock_gui_provider = MagicMock()
        mock_hub = MagicMock(spec=HubClient)
        mock_hub.get_status.return_value = {"state": "hosting_active", "current_host": "Alex"}

        orch = SessionOrchestrator(
            config=configured_cfg,
            env=self.env,
            hub_client=mock_hub,
            setup_gui_provider=mock_gui_provider,
        )

        with patch("src.client.session_orchestrator.show_role_banner"), \
             patch("src.client.session_orchestrator.copy_to_clipboard"), \
             patch("src.client.session_orchestrator.GuestP2PProxy"):
            report = orch.prepare_session()

        # GUI ни разу не вызывался!
        mock_gui_provider.assert_not_called()
        self.assertEqual(orch.role, RoleType.GUEST)

    def test_13_403_forbidden_triggers_reauth_gui(self):
        """При ответе 403 Forbidden (токен отозван) перехватчик сбрасывает токен и вызывает окно повторной авторизации."""
        stale_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="revoked_token_403",
            player_name="TesterPikachu",
        )

        new_valid_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="newly_issued_token_777",
            player_name="TesterPikachu",
        )

        mock_gui_provider = MagicMock(return_value=new_valid_cfg)

        mock_hub = MagicMock(spec=HubClient)
        # Первый вызов get_status выбрасывает 403 Forbidden, второй вызов успешен
        mock_hub.get_status.side_effect = [
            Exception("Client error '403 Forbidden' for url 'http://127.0.0.1:8088/status'"),
            {"state": "hosting_active", "current_host": "Alex"},
        ]

        orch = SessionOrchestrator(
            config=stale_cfg,
            env=self.env,
            hub_client=mock_hub,
            setup_gui_provider=mock_gui_provider,
        )

        with patch("src.client.session_orchestrator.show_role_banner"), \
             patch("src.client.session_orchestrator.copy_to_clipboard"), \
             patch("src.client.session_orchestrator.GuestP2PProxy"):
            report = orch.prepare_session()

        mock_gui_provider.assert_called_once()
        # Проверяем, что в GUI было передано сообщение о необходимости повторной авторизации
        _, kwargs = mock_gui_provider.call_args
        self.assertIn("Доступ устройства был обновлен", kwargs.get("prompt_message", ""))
        self.assertEqual(orch.config.device_token, "newly_issued_token_777")
        self.assertEqual(orch.role, RoleType.GUEST)


class TestBuildFlagsAndNoEmojis(unittest.TestCase):
    """Тестирование скрипта сборки PyInstaller и отсутствия эмодзи."""

    def test_14_build_client_has_noconsole_flags(self):
        """Проверка наличия флага --noconsole для исключения черного окна терминала."""
        build_script = Path(__file__).resolve().parent.parent / "build" / "build_client.py"
        self.assertTrue(build_script.is_file())

        content = build_script.read_text(encoding="utf-8")
        self.assertIn("--noconsole", content)
        self.assertIn("src.client.setup_gui", content)

    def test_15_zero_emojis_in_setup_gui(self):
        """Проверка абсолютного отсутствия эмодзи и смайликов в файле src/client/setup_gui.py."""
        gui_file = Path(__file__).resolve().parent.parent / "src" / "client" / "setup_gui.py"
        self.assertTrue(gui_file.is_file())

        content = gui_file.read_text(encoding="utf-8")

        emoji_pattern = re.compile(
            r"[\U0001F600-\U0001F64F"  # Emoticons
            r"\U0001F300-\U0001F5FF"  # Symbols & Pictographs
            r"\U0001F680-\U0001F6FF"  # Transport & Map
            r"\U0001F1E0-\U0001F1FF"  # Regional Indicator Symbols / Flags
            r"\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
            r"\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
            r"\U00002700-\U000027BF]"  # Dingbats
        )
        matches = emoji_pattern.findall(content)
        self.assertEqual(len(matches), 0, f"Обнаружены запрещенные эмодзи в setup_gui.py: {matches}")
