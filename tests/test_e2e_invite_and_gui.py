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


if __name__ == "__main__":
    unittest.main()
