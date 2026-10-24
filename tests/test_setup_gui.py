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

import json
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
    DEFAULT_SERVER_WORLD_OPTION,
    KEEP_CURRENT_SERVER_WORLD_OPTION,
    SKIP_WORLD_UPLOAD_OPTION,
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
        self.env.set_player_username("GreyPikachu")

        # Создаем tk.Tk в скрытом режиме без вывода на экран
        self.root = tk.Tk()
        self.root.deiconify()

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
        self.assertEqual(app.player_name, "GreyPikachu")
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

    def test_02b_keyboard_shortcuts_select_all_and_clear(self):
        """Проверка сочетаний клавиш Cmd+A (латиница и кириллица) и Cmd+Backspace."""
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=False,
        )
        self.root.update()

        # Помещаем тестовый код в поле ввода
        app.code_entry.focus_force()
        app.code_var.set("FCT1-TEST-SHORTCUTS")
        self.root.update()

        # 1. Проверяем Select All с латинской раскладкой (Cmd+A: keycode 0, keysym 'a')
        evt_latin = tk.Event()
        evt_latin.widget = app.code_entry
        evt_latin.keycode = 0
        evt_latin.keysym = "a"
        evt_latin.state = 8
        res = app._on_entry_key(evt_latin)
        self.assertEqual(res, "break")
        self.assertTrue(app.code_entry.selection_present())
        self.assertEqual(app.code_entry.selection_get(), "FCT1-TEST-SHORTCUTS")

        # 2. Снимаем выделение и проверяем русскую раскладку (Cmd+Ф: keycode 0, keysym 'Cyrillic_ef')
        app.code_entry.select_clear()
        self.assertFalse(app.code_entry.selection_present())

        evt_cyrillic = tk.Event()
        evt_cyrillic.widget = app.code_entry
        evt_cyrillic.keycode = 0
        evt_cyrillic.keysym = "Cyrillic_ef"
        evt_cyrillic.state = 8
        res = app._on_entry_key(evt_cyrillic)
        self.assertEqual(res, "break")
        self.assertTrue(app.code_entry.selection_present())
        self.assertEqual(app.code_entry.selection_get(), "FCT1-TEST-SHORTCUTS")

        # 3. Проверяем Cmd+Backspace (быстрая очистка поля)
        evt_bs = tk.Event()
        evt_bs.widget = app.code_entry
        evt_bs.keycode = 51
        evt_bs.keysym = "BackSpace"
        evt_bs.state = 8
        res = app._on_entry_key(evt_bs)
        self.assertEqual(res, "break")
        self.assertEqual(app.code_entry.get(), "")

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
            player_name="GreyPikachu",
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
            player_name="GreyPikachu",
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
        self.assertEqual(str(app.action_btn.cget("state")), "normal")

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


class TestFirstRunConditionalWorldSelector(unittest.TestCase):
    """Тесты для Задачи 021: условный двухшаговый выбор мира и скрытие заглушки."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_gui_step2_")).resolve()
        self.user_data = (self.temp_dir / "userdata").resolve()
        self.game_root = (self.temp_dir / "gameroot").resolve()
        self.saves_dir = (self.user_data / "saves").resolve()
        self.user_data.mkdir(parents=True)
        self.game_root.mkdir(parents=True)
        self.saves_dir.mkdir(parents=True)

        self.env = FactorioEnvironment(
            custom_game_root=self.game_root,
            custom_user_data_dir=self.user_data,
        )
        self.env.set_player_username("PikachuTester")

        self.root = tk.Tk()
        self.root.deiconify()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_save(self, name: str) -> Path:
        import zipfile
        p = self.saves_dir / name
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("level.dat", b"dummy_content")
        return p

    def test_first_run_world_card_is_hidden_initially(self):
        """Карточка выбора мира скрыта при первом открытии (до подключения)."""
        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        self.assertFalse(app.world_card.winfo_ismapped())
        self.assertTrue(app.inputs_card.winfo_ismapped())
        self.assertEqual(app.step, "enter_invite")
        self.assertNotIn("Оставить текущий мир сервера (cloud_world.zip)", app.world_options)
        self.assertEqual(app.action_btn.cget("text"), "Подключить и продолжить")

    @patch("src.client.setup_gui.HubClient.activate_with_invite_code")
    def test_activation_server_has_save_skips_step2(self, mock_activate):
        """Если на сервере уже есть мир (save_exists=True), селектор не отображается."""
        mock_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="dev_tok_123",
            player_name="PikachuTester",
        )
        mock_activate.return_value = mock_cfg
        self._create_save("MyBase.zip")

        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        with patch.object(HubClient, "get_status", return_value={"save_exists": True}):
            app._activate_worker("FCT1-CODE", "secret")
            self.root.update()

        self.assertTrue(app.server_has_save)
        self.assertEqual(app.step, "enter_invite")
        self.assertFalse(app.world_card.winfo_ismapped())
        self.assertIn("Готово! Сервер подключен", app.status_var.get())
        self.assertEqual(app.result_config, mock_cfg)

    @patch("src.client.setup_gui.HubClient.activate_with_invite_code")
    def test_activation_server_empty_no_local_saves_starts_game_directly(self, mock_activate):
        """Если сервер пустой и локальных сохранений нет, игра запускается сразу."""
        mock_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="dev_tok_123",
            player_name="PikachuTester",
        )
        mock_activate.return_value = mock_cfg

        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        with patch.object(HubClient, "get_status", return_value={"save_exists": False}):
            app._activate_worker("FCT1-CODE", "secret")
            self.root.update()

        self.assertFalse(app.server_has_save)
        self.assertEqual(app.step, "enter_invite")
        self.assertFalse(app.world_card.winfo_ismapped())
        self.assertIn("Готово! Сервер подключен", app.status_var.get())

    @patch("src.client.setup_gui.HubClient.activate_with_invite_code")
    def test_activation_server_empty_with_local_saves_transitions_to_step2(self, mock_activate):
        """Если сервер пустой и есть локальные сейвы, окно переходит на Шаг 2."""
        mock_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="dev_tok_123",
            player_name="PikachuTester",
        )
        mock_activate.return_value = mock_cfg
        self._create_save("Судные дни.zip")

        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        with patch.object(HubClient, "get_status", return_value={"save_exists": False}):
            app._activate_worker("FCT1-CODE", "secret")
            self.root.update()

        self.assertEqual(app.step, "select_initial_world")
        self.assertFalse(app.inputs_card.winfo_ismapped())
        self.assertTrue(app.world_card.winfo_ismapped())
        self.assertEqual(
            app.world_title_lbl.cget("text"),
            "На сервере еще нет сохраненного мира. Выберите ваш мир для загрузки:",
        )
        self.assertEqual(app.world_options, ["Судные дни.zip", SKIP_WORLD_UPLOAD_OPTION])
        self.assertEqual(app.world_combo_var.get(), "Судные дни.zip")
        self.assertEqual(app.action_btn.cget("text"), "Загрузить мир на сервер и играть")
        self.assertIn("Сервер подключен! Выберите мир", app.status_var.get())

    def test_step2_skip_upload_action(self):
        """На Шаге 2 выбор «Не загружать» сохраняет конфиг и запускает игру без выгрузки."""
        self._create_save("Судные дни.zip")
        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        mock_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="tok",
            player_name="PikachuTester",
        )
        app._transition_to_empty_server_world_selection(mock_cfg, [self.saves_dir / "Судные дни.zip"])
        self.root.update()

        app.world_combo_var.set(SKIP_WORLD_UPLOAD_OPTION)

        with patch("src.client.setup_gui.publish_selected_world_to_server") as mock_pub:
            app._on_activate_click()
            mock_pub.assert_not_called()

        self.assertEqual(app.result_config, mock_cfg)
        self.assertIn("Запуск игры...", app.status_var.get())

    def test_step2_publish_selected_save_action(self):
        """На Шаге 2 выбор сейва запускает фоновую выгрузку на сервер."""
        save_path = self._create_save("Судные дни.zip")
        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        mock_cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="tok",
            player_name="PikachuTester",
        )
        app._transition_to_empty_server_world_selection(mock_cfg, [save_path])
        self.root.update()

        app.world_combo_var.set("Судные дни.zip")

        with patch("src.client.setup_gui.publish_selected_world_to_server") as mock_pub:
            mock_pub.return_value = {"published": True}
            app._publish_world_worker("Судные дни.zip", mock_cfg)
            mock_pub.assert_called_once()
            self.root.update()

        self.assertEqual(app.result_config, mock_cfg)
        self.assertIn("Мир успешно опубликован на сервер", app.status_var.get())

    def test_shift_mode_with_server_empty_vs_has_save(self):
        """При запуске по Shift селектор отражает статус наличия мира на сервере."""
        self._create_save("Base1.zip")
        cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="tok",
            player_name="PikachuTester",
        )

        with patch.object(HubClient, "get_status", return_value={"save_exists": False}):
            app_empty = FirstRunSetupWindow(root=self.root, env=self.env, existing_config=cfg)
            self.root.update()
            self.assertTrue(app_empty.world_card.winfo_ismapped())
            self.assertNotIn(KEEP_CURRENT_SERVER_WORLD_OPTION, app_empty.world_options)
            self.assertEqual(app_empty.world_options[0], "Base1.zip")
            self.assertEqual(app_empty.world_options[-1], SKIP_WORLD_UPLOAD_OPTION)

        with patch.object(HubClient, "get_status", return_value={"save_exists": True}):
            root_has = tk.Tk()
            try:
                app_has = FirstRunSetupWindow(root=root_has, env=self.env, existing_config=cfg)
                root_has.update()
                self.assertTrue(app_has.world_card.winfo_ismapped())
                self.assertEqual(app_has.world_options[0], KEEP_CURRENT_SERVER_WORLD_OPTION)
                self.assertIn("Base1.zip", app_has.world_options)
            finally:
                root_has.destroy()


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


class TestFirstRunDarkModeAndCleanLabels(unittest.TestCase):
    """Тестирование темной темы ttk и очистки меток интерфейса от технического шума (Задача 022)."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_gui_dark_")).resolve()
        self.user_data = (self.temp_dir / "userdata").resolve()
        self.game_root = (self.temp_dir / "gameroot").resolve()
        self.user_data.mkdir(parents=True)
        self.game_root.mkdir(parents=True)

        self.env = FactorioEnvironment(
            custom_game_root=self.game_root,
            custom_user_data_dir=self.user_data,
        )
        self.env.set_player_username("PikachuTester")
        self.root = tk.Tk()
        self.root.deiconify()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _collect_all_widget_texts(self, widget) -> list[str]:
        """Рекурсивно собирает текст со всех виджетов окна."""
        texts = []
        for child in widget.winfo_children():
            try:
                txt = child.cget("text")
                if txt:
                    texts.append(str(txt))
            except Exception:
                pass
            texts.extend(self._collect_all_widget_texts(child))
        return texts

    def test_clean_labels_no_technical_noise(self):
        """Проверка отсутствия технического шума: (player-data.json), P2P-кооператив и т.д."""
        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        all_texts = self._collect_all_widget_texts(self.root)
        full_text = " ".join(all_texts)

        # 1. Запрещенные технические фразы
        self.assertNotIn("(player-data.json)", full_text)
        self.assertNotIn("P2P-кооператив", full_text)
        self.assertNotIn("без белых IP", full_text)
        self.assertNotIn("Никнейм подтягивается автоматически", full_text)

        # 2. Карточка игрока строго 'Игрок: ...'
        self.assertIn(f"Игрок: {app.player_name}", all_texts)

        # 3. Чистые подписи полей ввода в начальном режиме
        self.assertIn("Код Дружбы:", all_texts)
        self.assertIn("Секретное слово:", all_texts)

    def test_clean_labels_in_shift_reconfig_mode(self):
        """Проверка чистых подписей полей в режиме повторного конфигурирования (Shift-режим)."""
        cfg = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="tok",
            player_name="PikachuTester",
        )
        with patch.object(HubClient, "get_status", return_value={"save_exists": True}):
            app = FirstRunSetupWindow(root=self.root, env=self.env, existing_config=cfg)
            self.root.update()

            all_texts = self._collect_all_widget_texts(self.root)
            self.assertIn("Новый Код Дружбы (необязательно):", all_texts)
            self.assertIn("Секретное слово (необязательно):", all_texts)
            self.assertIn("Активный мир на сервере:", all_texts)

    def test_ttk_buttons_and_dark_styles(self):
        """Проверка использования ttk.Button и стилей Accent.TButton, Secondary.TButton, Dark.TCombobox."""
        from tkinter import ttk

        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        # 1. Кнопки должны быть экземплярами ttk.Button (не обычными tk.Button)
        self.assertIsInstance(app.action_btn, ttk.Button)
        self.assertIsInstance(app.toggle_pass_btn, ttk.Button)

        def find_buttons(w):
            res = []
            for c in w.winfo_children():
                if isinstance(c, ttk.Button):
                    res.append(c)
                res.extend(find_buttons(c))
            return res

        all_buttons = find_buttons(self.root)
        self.assertGreaterEqual(len(all_buttons), 4)

        # 2. Стили кнопок
        self.assertEqual(str(app.action_btn.cget("style")), "Accent.TButton")
        self.assertEqual(str(app.toggle_pass_btn.cget("style")), "Secondary.TButton")

        # Все второстепенные кнопки (Вставить, Показать, Автономно) имеют Secondary.TButton
        for btn in all_buttons:
            if btn != app.action_btn:
                self.assertEqual(str(btn.cget("style")), "Secondary.TButton")

        # 3. Стиль селектора мира
        self.assertIsInstance(app.world_combo, ttk.Combobox)
        self.assertEqual(str(app.world_combo.cget("style")), "Dark.TCombobox")

        # 4. Тема оформления ttk
        self.assertEqual(app.style.theme_use(), "clam")

    def test_installer_mode_buttons_are_ttk(self):
        """В режиме installer_mode кнопка деинсталляции и выбора папки также ttk.Button со стилями."""
        from tkinter import ttk

        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=True)
        self.root.update()

        self.assertIsInstance(app.action_btn, ttk.Button)
        self.assertEqual(str(app.action_btn.cget("style")), "Accent.TButton")

        def find_buttons(w):
            res = []
            for c in w.winfo_children():
                if isinstance(c, ttk.Button):
                    res.append(c)
                res.extend(find_buttons(c))
            return res

        all_buttons = find_buttons(self.root)
        self.assertGreaterEqual(len(all_buttons), 4)
        for btn in all_buttons:
            style = str(btn.cget("style"))
            self.assertIn(style, ["Accent.TButton", "Secondary.TButton"])


class TestGUIUninstallerIntegration(unittest.TestCase):
    """Тестирование функционала кнопки деинсталлятора в GUI (Задача 024)."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_gui_uninstall_")).resolve()
        self.user_data = (self.temp_dir / "userdata").resolve()
        self.game_root = (self.temp_dir / "gameroot").resolve()
        self.user_data.mkdir(parents=True)
        self.game_root.mkdir(parents=True)

        self.env = FactorioEnvironment(
            custom_game_root=self.game_root,
            custom_user_data_dir=self.user_data,
        )
        self.env.set_player_username("PikachuUninstallTest")
        self.root = tk.Tk()
        self.root.deiconify()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_uninstaller_button_present_in_normal_mode(self):
        """Кнопка деинсталляции присутствует в обычном режиме (installer_mode=False)."""
        from tkinter import ttk
        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        self.assertTrue(hasattr(app, "uninstall_btn"))
        self.assertIsInstance(app.uninstall_btn, ttk.Button)
        self.assertEqual(app.uninstall_btn.cget("text"), "Удалить перехватчик (Вернуть чистую игру)")
        self.assertEqual(str(app.uninstall_btn.cget("style")), "Secondary.TButton")

    def test_uninstaller_cancelled_by_user(self):
        """При отказе в окне подтверждения (askyesno=False) деинсталляция отменяется."""
        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        with patch("tkinter.messagebox.askyesno", return_value=False) as mock_ask, \
             patch("tkinter.messagebox.showinfo") as mock_info:
            app._on_uninstall_click()
            mock_ask.assert_called_once()
            mock_info.assert_not_called()
            self.assertFalse(app.was_uninstalled)

    def test_uninstaller_confirmed_and_safely_executed(self):
        """При подтверждении вызывается деинсталляция, сохраняются сейвы и закрывается окно."""
        saves_dir = self.env.saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        player_save = saves_dir / "Судные дни.zip"
        player_save.write_bytes(b"valuable_player_data")

        mock_uninstall_cb = MagicMock(return_value={"status": "uninstalled", "message": "Успешно удалено"})
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            installer_mode=False,
            on_uninstall_callback=mock_uninstall_cb,
        )
        self.root.update()

        with patch("tkinter.messagebox.askyesno", return_value=True) as mock_ask, \
             patch("tkinter.messagebox.showinfo") as mock_info:
            app._on_uninstall_click()
            mock_ask.assert_called_once()
            mock_info.assert_called_once()
            mock_uninstall_cb.assert_called_once_with(self.game_root)
            self.assertTrue(app.was_uninstalled)
            self.assertTrue(app.result_config.was_uninstalled)

        # Проверяем, что сейв игрока остался абсолютно нетронутым
        self.assertTrue(player_save.is_file())
        self.assertEqual(player_save.read_bytes(), b"valuable_player_data")

    def test_session_orchestrator_handles_uninstalled_config_cleanly(self):
        """SessionOrchestrator корректно завершает работу с кодом 0 при деинсталляции через GUI."""
        cfg = ClientConfig(hub_http_url="http://127.0.0.1:8088", device_token="tok", player_name="Pikachu")
        orchestrator = SessionOrchestrator(
            config=cfg,
            env=self.env,
            setup_gui_provider=lambda **kwargs: ClientConfig(was_uninstalled=True),
            force_setup_gui=True,
        )

        with patch.object(orchestrator, "launch_game") as mock_launch:
            exit_code = orchestrator.run()
            self.assertEqual(exit_code, 0)
            mock_launch.assert_not_called()

    def test_progress_bar_display_and_updates(self):
        """Проверка отображения и корректного расчета шкалы прогресса, скорости и ETA."""
        app = FirstRunSetupWindow(root=self.root, env=self.env, installer_mode=False)
        self.root.update()

        # Изначально прогресс-карточка скрыта
        self.assertFalse(app.progress_card.winfo_ismapped())

        # Отображение прогресса
        app.show_progress("Выгрузка тестового мира...")
        self.root.update()
        self.assertTrue(app.progress_card.winfo_ismapped())
        self.assertEqual(app.progress_title_lbl.cget("text"), "Выгрузка тестового мира...")
        self.assertEqual(app.progress_bar["value"], 0)

        # Обновление 50% с имитацией скорости 2 МБ/с
        app.update_progress(
            uploaded_bytes=25 * 1024 * 1024,
            total_bytes=50 * 1024 * 1024,
            speed_bytes_per_sec=2 * 1024 * 1024,
        )
        self.root.update()
        self.assertAlmostEqual(app.progress_bar["value"], 50.0, places=1)
        self.assertIn("25.0 / 50.0 МБ (50%)", app.progress_bytes_lbl.cget("text"))
        self.assertIn("2.0 МБ/с", app.progress_speed_lbl.cget("text"))
        self.assertIn("осталось ~12 сек", app.progress_speed_lbl.cget("text"))

        # Завершение на 100%
        app.update_progress(
            uploaded_bytes=50 * 1024 * 1024,
            total_bytes=50 * 1024 * 1024,
            speed_bytes_per_sec=2 * 1024 * 1024,
        )
        self.root.update()
        self.assertEqual(app.progress_bar["value"], 100.0)
        self.assertIn("50.0 / 50.0 МБ (100%)", app.progress_bytes_lbl.cget("text"))

        # Скрытие прогресса
        app.hide_progress()
        self.root.update()
        self.assertFalse(app.progress_card.winfo_ismapped())


class TestGUISubprocessIsolationAndCleanClose(unittest.TestCase):
    """Тесты изоляции GUI в дочернем процессе и чистого закрытия окна (Задача 027)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.env = FactorioEnvironment(custom_user_data_dir=self.tmp_path)
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        self.tmp_dir.cleanup()

    def test_close_window_cleanly_calls_withdraw_and_destroy(self):
        """Проверяет принудительное скрытие (withdraw) и очистку Cocoa-очереди при закрытии окна."""
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            config_path=self.tmp_path / "sync_cfg.json",
        )

        with patch.object(self.root, "withdraw") as mock_withdraw, \
             patch.object(self.root, "update_idletasks") as mock_idletasks, \
             patch.object(self.root, "quit") as mock_quit, \
             patch.object(self.root, "destroy") as mock_destroy:
            app._close_window_cleanly()

            self.assertTrue(app._closing)
            mock_withdraw.assert_called_once()
            mock_idletasks.assert_called_once()
            mock_quit.assert_called_once()
            mock_destroy.assert_called_once()

    def test_poll_ui_queue_bails_out_when_closing(self):
        """Проверяет, что при флаге _closing опрос UI-очереди прекращается."""
        app = FirstRunSetupWindow(
            root=self.root,
            env=self.env,
            config_path=self.tmp_path / "sync_cfg.json",
        )
        app._closing = True

        with patch.object(self.root, "after") as mock_after:
            app._poll_ui_queue()
            mock_after.assert_not_called()

    def test_run_setup_gui_in_subprocess_success(self):
        """Проверяет запуск изолированного подпроцесса GUI и парсинг ClientConfig."""
        from src.client.setup_gui import run_setup_gui_in_subprocess

        def fake_subprocess_run(cmd, env=None, check=False):
            # Извлекаем путь к result.json из аргументов команды
            result_file_path = None
            for idx, arg in enumerate(cmd):
                if arg == "--subprocess-result" and idx + 1 < len(cmd):
                    result_file_path = Path(cmd[idx + 1])
                    break
                if arg == "--wrapper-internal-setup-gui" and idx + 2 < len(cmd):
                    result_file_path = Path(cmd[idx + 2])
                    break

            if result_file_path:
                payload = {
                    "status": "ok",
                    "config": {
                        "hub_http_url": "http://10.20.30.40:8088",
                        "device_token": "subproc_token_123",
                        "player_name": "SubprocPlayer",
                    },
                    "was_uninstalled": False,
                }
                result_file_path.write_text(json.dumps(payload), encoding="utf-8")

            res_mock = MagicMock()
            res_mock.returncode = 0
            return res_mock

        with patch("subprocess.run", side_effect=fake_subprocess_run):
            cfg = run_setup_gui_in_subprocess(
                env=self.env,
                config_path=self.tmp_path / "sync_cfg.json",
            )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.device_token, "subproc_token_123")
        self.assertEqual(cfg.player_name, "SubprocPlayer")
        self.assertFalse(cfg.was_uninstalled)

    def test_run_setup_gui_in_subprocess_uninstalled(self):
        """Проверяет обработку статуса uninstalled из дочернего процесса."""
        from src.client.setup_gui import run_setup_gui_in_subprocess

        def fake_subprocess_uninstall(cmd, env=None, check=False):
            result_file_path = None
            for idx, arg in enumerate(cmd):
                if arg == "--subprocess-result" and idx + 1 < len(cmd):
                    result_file_path = Path(cmd[idx + 1])
                    break
            if result_file_path:
                payload = {
                    "status": "uninstalled",
                    "config": None,
                    "was_uninstalled": True,
                }
                result_file_path.write_text(json.dumps(payload), encoding="utf-8")
            res_mock = MagicMock()
            res_mock.returncode = 0
            return res_mock

        with patch("subprocess.run", side_effect=fake_subprocess_uninstall):
            cfg = run_setup_gui_in_subprocess(
                env=self.env,
                config_path=self.tmp_path / "sync_cfg.json",
            )

        self.assertIsNotNone(cfg)
        self.assertTrue(cfg.was_uninstalled)

    def test_run_setup_gui_subprocess_entry_execution(self):
        """Проверяет точку входа подпроцесса run_setup_gui_subprocess_entry."""
        from src.client.setup_gui import run_setup_gui_subprocess_entry

        args_file = self.tmp_path / "args.json"
        result_file = self.tmp_path / "result.json"

        args_data = {
            "custom_user_data_dir": str(self.tmp_path),
            "config_path": str(self.tmp_path / "sync.json"),
            "installer_mode": False,
        }
        args_file.write_text(json.dumps(args_data), encoding="utf-8")

        mock_cfg = ClientConfig(device_token="entry_tok_456", player_name="EntryTester")

        with patch("src.client.setup_gui.run_first_time_setup_gui", return_value=mock_cfg):
            ret = run_setup_gui_subprocess_entry(args_file, result_file)

        self.assertEqual(ret, 0)
        self.assertTrue(result_file.is_file())
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["config"]["device_token"], "entry_tok_456")

    def test_factorio_wrapper_internal_setup_gui_cli(self):
        """Проверяет CLI-флаг --wrapper-internal-setup-gui в factorio_wrapper.py."""
        from src.client import factorio_wrapper

        args_path = str(self.tmp_path / "args.json")
        res_path = str(self.tmp_path / "res.json")

        with patch("src.client.setup_gui.run_setup_gui_subprocess_entry", return_value=0) as mock_entry:
            code = factorio_wrapper.main([
                "--wrapper-internal-setup-gui",
                args_path,
                res_path,
            ])
            self.assertEqual(code, 0)
            mock_entry.assert_called_once_with(Path(args_path), Path(res_path))

    def test_session_orchestrator_passes_use_subprocess_to_gui_runner(self):
        """Проверяет, что SessionOrchestrator передает use_subprocess=True в gui_runner."""
        mock_runner = MagicMock(return_value=ClientConfig(device_token="orch_tok_789"))
        unconfigured_cfg = ClientConfig(device_token="")

        orch = SessionOrchestrator(
            config=unconfigured_cfg,
            env=self.env,
            setup_gui_provider=mock_runner,
        )

        with patch("src.client.session_orchestrator.show_role_banner"), \
             patch("src.client.session_orchestrator.copy_to_clipboard"), \
             patch("src.client.session_orchestrator.HubClient"):
            orch.prepare_session()

        self.assertTrue(mock_runner.called)
        _, kwargs = mock_runner.call_args
        self.assertTrue(kwargs.get("use_subprocess"))



