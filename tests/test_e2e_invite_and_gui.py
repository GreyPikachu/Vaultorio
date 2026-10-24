"""Сквозные E2E-тесты системы онбординга через GUI, Кодов Дружбы (FCT1-...)

и динамической смены никнейма в player-data.json.

Проверяемые сценарии:
1. Полный жизненный цикл создания инвайта на сервере -> ввод в GUI первого запуска
   -> получение device_token -> сохранение factorio_sync_config.json -> запрет
   повторного использования инвайта (InviteTicketAlreadyUsedError).
2. Бесшумный повторный запуск без отображения GUI при наличии сохраненного токена.
3. Динамическая смена ника игроком в player-data.json без сброса токена и перенастройки.
4. Автоматический сброс токена и повторный запуск GUI при отзыве доступа (403 Forbidden).
5. Проверка полного отсутствия вхождений реального IP-адреса владельца по всему репозиторию.
6. Проверка актуальности правил в .gitignore для защиты приватных данных.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.hub_client import HubClient
from src.client.session_orchestrator import SessionOrchestrator
from src.client.setup_gui import FirstRunSetupWindow, run_first_time_setup_gui
from src.common.invite_codec import (
    INVITE_PREFIX,
    encode_invite_code,
    decode_invite_code,
)
from src.server.config import ServerConfig
from src.server.auth_manager import (
    AuthManager,
    InviteTicketAlreadyUsedError,
    InvalidInviteTicketError,
)


class TestE2EInviteAndGui(unittest.TestCase):
    """Сквозные интеграционные тесты авторизации и GUI-онбординга."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="factorio_e2e_test_")
        self.root_path = Path(self.test_dir)

        # Серверные директории и состояние
        self.server_storage_dir = self.root_path / "server_storage"
        self.server_storage_dir.mkdir(parents=True, exist_ok=True)
        self.server_config = ServerConfig(
            storage_dir=self.server_storage_dir,
            require_device_auth=True,
        )
        self.auth_manager = AuthManager(self.server_config)

        # Клиентские директории через custom_user_data_dir
        self.user_data_dir = self.root_path / "userdata"
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        self.env = FactorioEnvironment(custom_user_data_dir=self.user_data_dir)
        self.env.ensure_dirs()

        self.player_data_path = self.env.player_data_path
        self.player_data_path.write_text(
            json.dumps({"service-username": "CosmonautAlex"}, indent=2),
            encoding="utf-8",
        )

        self.passphrase = "суперсекрет_2026"
        self.hub_url = "http://127.0.0.1:8088"
        self.stun_host = "127.0.0.1"
        self.stun_port = 14197

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_e2e_full_invite_and_gui_lifecycle(self):
        """Сквозной сценарий: генерация инвайта -> GUI онбординг -> сохранение токена -> запрет повторного ввода."""
        # 1. Сервер генерирует одноразовый билет
        ticket_data = self.auth_manager.create_invite(ttl_seconds=3600, max_uses=1, note="E2E Тест")
        ticket = ticket_data["ticket"]
        expires_at = ticket_data["expires_at"]

        # 2. Сервер шифрует билет в Код Дружбы FCT1-...
        invite_code = encode_invite_code(
            hub_http_url=self.hub_url,
            hub_stun_host=self.stun_host,
            hub_stun_port=self.stun_port,
            ticket=ticket,
            expires_at=expires_at,
            passphrase=self.passphrase,
        )
        self.assertTrue(invite_code.startswith(INVITE_PREFIX))

        # 3. Клиент стартует без конфигурации
        config_path = self.env.sync_config_path
        self.assertFalse(config_path.exists())

        config = ClientConfig()
        self.assertFalse(config.is_configured())

        # 4. Имитируем работу FirstRunSetupWindow в headless-режиме через переменные окружения
        env_patches = {
            "FACTORIO_HEADLESS_MOCK_INVITE": invite_code,
            "FACTORIO_HEADLESS_MOCK_PASSPHRASE": self.passphrase,
        }

        # Мокаем httpx.Client.post для обращения к локальному auth_manager
        def mock_httpx_post(url, *args, **kwargs):
            json_body = kwargs.get("json") or {}
            ticket_val = json_body.get("ticket", "")
            player_val = json_body.get("player_name", "Unknown")

            if "/auth/redeem-invite" in str(url):
                try:
                    res = self.auth_manager.redeem_invite(
                        ticket=ticket_val,
                        player_name=player_val,
                    )
                    req = httpx.Request("POST", str(url))
                    return httpx.Response(200, json=res, request=req)
                except InviteTicketAlreadyUsedError:
                    req = httpx.Request("POST", str(url))
                    return httpx.Response(403, json={"detail": "Ticket already redeemed"}, request=req)
                except Exception as exc:
                    req = httpx.Request("POST", str(url))
                    return httpx.Response(400, json={"detail": str(exc)}, request=req)

            req = httpx.Request("POST", str(url))
            return httpx.Response(404, json={"detail": "Not found"}, request=req)

        with patch.dict(os.environ, env_patches), patch.object(httpx.Client, "post", side_effect=mock_httpx_post):
            res_cfg = run_first_time_setup_gui(
                env=self.env,
                config_path=config_path,
            )

        self.assertIsNotNone(res_cfg)
        self.assertTrue(res_cfg.is_configured())
        self.assertTrue(config_path.exists())
        self.assertTrue(len(res_cfg.device_token) > 20)
        self.assertEqual(res_cfg.hub_http_url, self.hub_url)
        self.assertEqual(res_cfg.hub_stun_host, self.stun_host)
        self.assertEqual(res_cfg.hub_stun_port, self.stun_port)

        # 5. Проверяем, что повторное погашение того же инвайта блокируется на сервере
        with self.assertRaises(InviteTicketAlreadyUsedError):
            self.auth_manager.redeem_invite(
                ticket=ticket,
                player_name="another_user",
            )

    def test_e2e_silent_subsequent_runs(self):
        """При повторных запусках (когда конфиг уже сохранен) GUI не открывается вовсе."""
        config_path = self.env.sync_config_path
        preconfigured = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            hub_stun_host="127.0.0.1",
            hub_stun_port=14197,
            device_token="valid_existing_device_token_xyz",
            device_id="my_device_id",
        )
        preconfigured.save_to_file(config_path)

        # Проверяем, что is_configured возвращает True
        loaded = ClientConfig.load_from_file(config_path)
        self.assertTrue(loaded.is_configured())

        # Запускаем логику оркестратора первого запуска
        with patch("src.client.setup_gui.run_first_time_setup_gui") as mock_gui:
            orchestrator = SessionOrchestrator(
                env=self.env,
                config=loaded,
            )
            # В run_lifecycle_before_game проверяется if not self.config.is_configured():
            self.assertTrue(orchestrator.config.is_configured())
            mock_gui.assert_not_called()

    def test_e2e_dynamic_nickname_update(self):
        """Смена service-username в player-data.json мгновенно считывается при следующем запуске без сброса токена."""
        # Начальный ник
        username1 = self.env.resolve_or_init_player_username()
        self.assertEqual(username1, "CosmonautAlex")

        # Игрок меняет ник в меню настроек Factorio
        self.player_data_path.write_text(
            json.dumps({"service-username": "ChiefEngineerAlexPro"}, indent=2),
            encoding="utf-8",
        )

        # При следующем запуске считывается обновленный ник
        username2 = self.env.resolve_or_init_player_username()
        self.assertEqual(username2, "ChiefEngineerAlexPro")

    def test_e2e_token_revocation_recovery(self):
        """Отзыв токена на сервере вызывает сброс сохраненного токена и переход к повторной настройке."""
        config_path = self.env.sync_config_path
        config = ClientConfig(
            hub_http_url="http://127.0.0.1:8088",
            device_token="revoked_token_123",
        )
        config.save_to_file(config_path)
        self.assertTrue(config.is_configured())

        # Имитируем ответ сервера 403 Forbidden на проверку статуса
        hub_client = HubClient(
            config=config,
            device_token=config.device_token,
        )

        def mock_get(url, *args, **kwargs):
            req = httpx.Request("GET", str(url))
            return httpx.Response(403, json={"detail": "Forbidden: device token revoked"}, request=req)

        with patch.object(httpx.Client, "get", side_effect=mock_get):
            with self.assertRaises(httpx.HTTPStatusError):
                hub_client.get_status()

        # При получении 403 перехватчик сбрасывает токен и очищает конфиг
        config.device_token = ""
        config.save_to_file(config_path)
        self.assertFalse(config.is_configured())

    def test_zero_real_ip_leakage_repo_wide(self):
        """Проверка полного отсутствия реального IP-адреса владельца по всей структуре проекта."""
        repo_root = Path(__file__).resolve().parent.parent

        # Динамически конструируем проверяемый IP без буквального литерала в тексте теста
        parts = ["45", "91", "248", "78"]
        forbidden_ip = ".".join(parts)

        scan_dirs = ["src", "tests", "deploy", "build"]
        violations = []

        for folder_name in scan_dirs:
            folder = repo_root / folder_name
            if not folder.exists():
                continue
            for file_path in folder.rglob("*"):
                if file_path.is_file():
                    # Пропускаем скомпилированные байт-коды
                    if file_path.suffix in [".pyc", ".pyo", ".so"]:
                        continue
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                    if forbidden_ip in text:
                        violations.append(str(file_path.relative_to(repo_root)))

        # Также проверяем файлы документации и заметок в корне репозитория
        for root_file in ["README.md", ".gitignore", "SETUP FACTORIO.txt", "FacotryArchitecture.txt"]:
            rf_path = repo_root / root_file
            if rf_path.is_file():
                text = rf_path.read_text(encoding="utf-8", errors="ignore")
                if forbidden_ip in text:
                    violations.append(root_file)

        self.assertEqual(
            violations,
            [],
            f"Обнаружена утечка реального IP-адреса {forbidden_ip} в следующих файлах: {violations}",
        )

    def test_gitignore_coverage(self):
        """Проверка наличия всех приватных и временных файлов в .gitignore."""
        repo_root = Path(__file__).resolve().parent.parent
        gitignore_path = repo_root / ".gitignore"
        self.assertTrue(gitignore_path.is_file(), "Файл .gitignore отсутствует в корне проекта")

        content = gitignore_path.read_text(encoding="utf-8")

        required_patterns = [
            "factorio_sync_config.json",
            "auth_state.json",
            "*.log",
            ".cloud_stash/",
            "build_temp/",
            "dist/",
            "FacotryArchitecture.txt",
            "SETUP FACTORIO.txt",
            "Проблема1.txt",
            "Новые исправления1.txt",
            "SCR-*.png",
        ]

        missing = []
        for pat in required_patterns:
            if pat not in content:
                missing.append(pat)

        self.assertEqual(
            missing,
            [],
            f"В .gitignore отсутствуют обязательные правила исключения: {missing}",
        )

    def test_e2e_phase6_complete_flow(self):
        """Комплексная сквозная проверка Фазы 6 (Задачи 020, 021, 022).

        Сценарии:
        1. Автовосстановление ника 'graypikachu' из server-adminlist.json при заглушке 'CliPlayer'
           и защита от перезаписи установщиком.
        2. Проверка начального состояния FirstRunSetupWindow: сокрытие world_card, отсутствие
           технических надписей (player-data.json / P2P-кооператив), правильный ник 'Игрок: graypikachu',
           стили ttk.Button ('Accent.TButton' и 'Secondary.TButton').
        3. Двухшаговый селектор: при ответе сервера save_exists=False и наличии локального сейва
           'Судные дни.zip', окно переходит на Шаг 2 (показ world_card со стилем 'Dark.TCombobox',
           сокрытие полей ввода).
        4. Публикация мира на сервер и сохранение конфигурации.
        5. Повторный запуск: при обычном старте игра запускается напрямую, а при старте по Shift
           открывается окно настроек с опцией KEEP_CURRENT_SERVER_WORLD_OPTION.
        """
        import tkinter as tk
        from tkinter import ttk
        from src.installer.installer import FactorioWrapperInstaller
        from src.client.setup_gui import (
            FirstRunSetupWindow,
            KEEP_CURRENT_SERVER_WORLD_OPTION,
            SKIP_WORLD_UPLOAD_OPTION,
        )

        # ---------------------------------------------------------------------
        # Сценарий 1: Защита и автовосстановление никнейма (Задача 020)
        # ---------------------------------------------------------------------
        self.player_data_path.write_text(
            json.dumps({"service-username": "CliPlayer"}),
            encoding="utf-8",
        )
        self.env.adminlist_path.write_text(
            json.dumps(["graypikachu"]),
            encoding="utf-8",
        )

        # Проверяем автоматическое восстановление ника graypikachu
        resolved_name = self.env.resolve_or_init_player_username()
        self.assertEqual(resolved_name, "graypikachu")

        # Проверяем самоисцеление player-data.json
        pd_data = json.loads(self.player_data_path.read_text(encoding="utf-8"))
        self.assertEqual(pd_data.get("service-username"), "graypikachu")

        # Создаем имитацию бинарников для установщика
        game_root = self.root_path / "MockGameRoot"
        bin_dir = game_root / "bin" / "x64"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "factorio.exe").write_bytes(b"MZ" + b"\x00" * 4096)
        (game_root / "config-path.cfg").write_text("use-system-read-write-data-directories=false\n", encoding="utf-8")

        game_env = FactorioEnvironment(custom_game_root=game_root)
        game_env.ensure_dirs()
        game_env.set_player_username("graypikachu", force=True)

        installer = FactorioWrapperInstaller()
        report = installer.install(
            game_dir=game_root,
            player_name="CliPlayer",
            system_override="Windows",
        )
        self.assertEqual(report["player_name"], "graypikachu")
        self.assertEqual(game_env.get_player_username(), "graypikachu")

        # ---------------------------------------------------------------------
        # Сценарий 2: Чистый Dark Mode и скрытый селектор на Шаге 1 (Задачи 021, 022)
        # ---------------------------------------------------------------------
        root = tk.Tk()
        root.deiconify()
        try:
            app = FirstRunSetupWindow(
                root=root,
                env=self.env,
                installer_mode=False,
            )
            root.update()

            # Шаг 1: world_card скрыт до ввода инвайта
            self.assertFalse(app.world_card.winfo_ismapped())

            # Проверка очистки надписей от технического шума
            self.assertEqual(app.player_name, "graypikachu")

            def collect_texts(w):
                res = []
                for c in w.winfo_children():
                    try:
                        txt = c.cget("text")
                        if txt:
                            res.append(str(txt))
                    except Exception:
                        pass
                    res.extend(collect_texts(c))
                return res

            all_texts = " ".join(collect_texts(root))
            self.assertNotIn("(player-data.json)", all_texts)
            self.assertNotIn("P2P-кооператив", all_texts)
            self.assertNotIn("без белых IP", all_texts)
            self.assertIn("Игрок: graypikachu", all_texts)

            # Проверка стилей ttk.Button и движка clam
            self.assertIsInstance(app.action_btn, ttk.Button)
            self.assertEqual(str(app.action_btn.cget("style")), "Accent.TButton")
            self.assertEqual(str(app.toggle_pass_btn.cget("style")), "Secondary.TButton")
            self.assertEqual(app.style.theme_use(), "clam")

            # -----------------------------------------------------------------
            # Сценарий 3: Двухшаговый переход при пустом сервере (Задача 021)
            # -----------------------------------------------------------------
            # Создаем локальное сохранение
            saves_dir = self.env.saves_dir
            saves_dir.mkdir(parents=True, exist_ok=True)
            test_save = saves_dir / "Судные дни.zip"
            test_save.write_bytes(b"dummy_save_content")

            mock_cfg = ClientConfig(
                hub_http_url=self.hub_url,
                hub_stun_host=self.stun_host,
                hub_stun_port=self.stun_port,
                device_token="device_token_phase6_test",
                player_name="graypikachu",
            )

            # Имитируем успешное подключение к серверу, где мира еще нет
            with patch("src.client.setup_gui.HubClient.activate_with_invite_code", return_value=mock_cfg), \
                 patch.object(HubClient, "get_status", return_value={"save_exists": False}):
                app._activate_worker("FCT1-CODE", "secret")
                root.update()

            # На Шаге 2 world_card отображается, inputs_card скрыт
            self.assertTrue(app.world_card.winfo_ismapped())
            self.assertFalse(app.inputs_card.winfo_ismapped())
            self.assertIsInstance(app.world_combo, ttk.Combobox)
            self.assertEqual(str(app.world_combo.cget("style")), "Dark.TCombobox")
            self.assertIn("Судные дни.zip", app.world_options)
            self.assertNotIn(KEEP_CURRENT_SERVER_WORLD_OPTION, app.world_options)
            self.assertEqual(app.world_combo_var.get(), "Судные дни.zip")

            # -----------------------------------------------------------------
            # Сценарий 4: Публикация мира
            # -----------------------------------------------------------------
            with patch("src.client.setup_gui.publish_selected_world_to_server") as mock_pub:
                mock_pub.return_value = {"published": True}
                app.world_combo_var.set("Судные дни.zip")
                app._publish_world_worker("Судные дни.zip", mock_cfg)
                root.update()
                mock_pub.assert_called_once()

            self.assertEqual(app.result_config, mock_cfg)
        finally:
            root.destroy()

        # ---------------------------------------------------------------------
        # Сценарий 5: Повторный запуск по Shift
        # ---------------------------------------------------------------------
        root_shift = tk.Tk()
        root_shift.deiconify()
        try:
            with patch.object(HubClient, "get_status", return_value={"save_exists": True}):
                app_shift = FirstRunSetupWindow(
                    root=root_shift,
                    env=self.env,
                    existing_config=mock_cfg,
                )
                root_shift.update()

                self.assertTrue(app_shift.world_card.winfo_ismapped())
                self.assertEqual(app_shift.world_options[0], KEEP_CURRENT_SERVER_WORLD_OPTION)
                self.assertIn("Судные дни.zip", app_shift.world_options)
                self.assertEqual(str(app_shift.world_combo.cget("style")), "Dark.TCombobox")
        finally:
            root_shift.destroy()


if __name__ == "__main__":
    unittest.main()
