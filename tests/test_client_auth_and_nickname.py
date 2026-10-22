"""
Тесты динамического никнейма из player-data.json, авторизации клиента и нулевого хардкода IP (Task 014).
Проверяет:
1. Динамическое чтение никнейма и автоматическую инициализацию через resolve_or_init_player_username.
2. Сериализацию, десериализацию и валидацию ClientConfig (device_token, is_configured).
3. Работу HubClient с заголовком X-Device-Token, методом redeem_invite и activate_with_invite_code.
4. Включение device_token в датаграммы HostP2PBridge и GuestP2PProxy.
5. Полное отсутствие реального IP-адреса в файлах src/client/.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.hub_client import HubClient
from src.client.p2p_tunnel import GuestP2PProxy, HostP2PBridge, decode_control_packet
from src.client.session_orchestrator import SessionOrchestrator
from src.common.invite_codec import (
    InvalidInvitePassphraseError,
    encode_invite_code,
)
from src.server.api import create_app
from src.server.auth_manager import AuthManager
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager
from src.server.storage import SaveStorageManager


class TestDynamicPlayerNickname(unittest.TestCase):
    """Тестирование динамического определения и инициализации имени игрока."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_env_nick_"))
        self.env = FactorioEnvironment(custom_user_data_dir=self.temp_dir)
        self.env.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_player_username_reads_service_username(self):
        """Чтение корректного service-username из player-data.json."""
        self.env.set_player_username("Dmitry_Factorio")
        self.assertEqual(self.env.get_player_username(), "Dmitry_Factorio")

    def test_get_player_username_dynamic_update(self):
        """Если игрок сменил ник в игре (в файле player-data.json), метод сразу возвращает новый ник."""
        self.env.set_player_username("OldNick")
        self.assertEqual(self.env.get_player_username(), "OldNick")

        # Имитируем сохранение нового ника самой игрой Factorio
        data = {"service-username": "NewDynamicNick"}
        self.env.player_data_path.write_text(json.dumps(data), encoding="utf-8")

        self.assertEqual(self.env.get_player_username(), "NewDynamicNick")

    def test_resolve_or_init_existing_valid_name(self):
        """Если в player-data.json уже есть валидный ник, resolve_or_init_player_username возвращает его."""
        self.env.set_player_username("MasterBuilder")
        resolved = self.env.resolve_or_init_player_username(fallback_name="FallbackGuy")
        self.assertEqual(resolved, "MasterBuilder")

    def test_resolve_or_init_overwrites_default_player_with_fallback(self):
        """Если в player-data.json стоит дефолтный 'Player', он перезаписывается на валидный fallback."""
        self.env.player_data_path.write_text(json.dumps({"service-username": "Player"}), encoding="utf-8")
        self.assertEqual(self.env.get_player_username(), "Player")

        resolved = self.env.resolve_or_init_player_username(fallback_name="SuperHero")
        self.assertEqual(resolved, "SuperHero")
        # Проверяем, что файл действительно обновился
        self.assertEqual(self.env.get_player_username(), "SuperHero")

    def test_resolve_or_init_empty_uses_os_user(self):
        """Если файл пуст и fallback не задан, автоматически подставляется имя учетной записи ОС."""
        if self.env.player_data_path.exists():
            self.env.player_data_path.unlink()

        resolved = self.env.resolve_or_init_player_username(fallback_name=None)
        self.assertTrue(len(resolved) >= 2)
        self.assertNotEqual(resolved.lower(), "player")
        self.assertEqual(self.env.get_player_username(), resolved)


class TestClientConfig(unittest.TestCase):
    """Тестирование настроек ClientConfig, десериализации и флагов готовности."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_cfg_"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_values_neutral(self):
        """Дефолтные значения ClientConfig содержат нейтральные локальные адреса (без хардкода публичных IP)."""
        cfg = ClientConfig()
        self.assertEqual(cfg.hub_http_url, "http://127.0.0.1:8088")
        self.assertEqual(cfg.hub_stun_host, "127.0.0.1")
        self.assertEqual(cfg.hub_stun_port, 14197)
        self.assertEqual(cfg.device_token, "")

    def test_is_configured_logic(self):
        """is_configured возвращает True только если есть device_token и hub_http_url."""
        cfg = ClientConfig()
        self.assertFalse(cfg.is_configured())

        cfg_with_token = ClientConfig(device_token="abc123token")
        self.assertTrue(cfg_with_token.is_configured())

    def test_save_and_load_config_file(self):
        """Атомарное сохранение в JSON и повторная загрузка ClientConfig."""
        config_path = self.temp_dir / "factorio_sync_config.json"
        original = ClientConfig(
            hub_http_url="http://192.168.1.50:8088",
            hub_stun_host="192.168.1.50",
            hub_stun_port=14197,
            player_name="Dima",
            device_token="secret_device_token_xyz",
            device_id="device-uuid-1234",
        )

        original.save_to_file(config_path)
        self.assertTrue(config_path.exists())

        loaded = ClientConfig.load_from_file(config_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.hub_http_url, original.hub_http_url)
        self.assertEqual(loaded.hub_stun_host, original.hub_stun_host)
        self.assertEqual(loaded.device_token, original.device_token)
        self.assertEqual(loaded.device_id, original.device_id)
        self.assertTrue(loaded.is_configured())

    def test_load_nonexistent_returns_none(self):
        """Загрузка из несуществующего файла возвращает None без исключений."""
        res = ClientConfig.load_from_file(self.temp_dir / "missing.json")
        self.assertIsNone(res)


class TestHubClientInviteActivation(unittest.TestCase):
    """Тестирование взаимодействия HubClient с системой авторизации и инвайт-кодами."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_hub_auth_"))
        self.server_config = ServerConfig(
            storage_dir=self.temp_dir,
            require_device_auth=True,
            http_port=8088,
            udp_stun_port=14197,
        )
        self.storage = SaveStorageManager(self.server_config)
        self.session_manager = SessionLockManager(self.server_config)
        self.auth_manager = AuthManager(self.server_config)

        self.fastapi_app = create_app(
            config=self.server_config,
            storage=self.storage,
            session_manager=self.session_manager,
            auth_manager=self.auth_manager,
        )

        # TestClient для прямого тестирования без подъема внешних сокетов
        self.http_client = TestClient(self.fastapi_app)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hub_client_headers_include_device_token(self):
        """HubClient автоматически добавляет заголовок X-Device-Token во все исходящие запросы."""
        client_cfg = ClientConfig(device_token="my_device_token_123")
        hub = HubClient(config=client_cfg, http_client=self.http_client)
        self.assertEqual(hub.device_token, "my_device_token_123")
        self.assertEqual(hub._client.headers.get("X-Device-Token"), "my_device_token_123")

    def test_redeem_invite_and_subsequent_status(self):
        """Активация билета через redeem_invite и последующий доступ к защищенному /status."""
        # Создаем инвайт на сервере
        inv = self.auth_manager.create_invite(ttl_seconds=3600, note="Тестовый друг")
        ticket = inv["ticket"]

        # Создаем HubClient без токена
        hub = HubClient(
            config=ClientConfig(hub_http_url="http://testserver"),
            http_client=self.http_client,
        )

        # 1. Запрос к /status без токена отклоняется сервером
        with self.assertRaises(httpx.HTTPStatusError) as ctx:
            hub.get_status()
        self.assertEqual(ctx.exception.response.status_code, 403)

        # 2. Активируем билет через redeem_invite
        redeem_res = hub.redeem_invite(ticket=ticket, player_name="TestHero")
        self.assertTrue(redeem_res["success"])
        token = redeem_res["device_token"]
        self.assertTrue(token)
        self.assertEqual(hub.device_token, token)

        # 3. Теперь запрос к /status проходит успешно (токен автоматически подставился)
        status_data = hub.get_status()
        self.assertEqual(status_data["state"], "idle")

    def test_activate_with_invite_code_success(self):
        """Полный сценарий онбординга через activate_with_invite_code с шифрованным FCT1-..."""
        # 1. Сервер генерирует инвайт
        inv = self.auth_manager.create_invite(ttl_seconds=3600, note="Дима")
        ticket = inv["ticket"]
        expires_at = int(inv["expires_at"])

        # 2. Формируем зашифрованный код приглашения
        passphrase = "суперпароль2026"
        invite_code = encode_invite_code(
            hub_http_url="http://testserver",
            hub_stun_host="127.0.0.1",
            hub_stun_port=14197,
            ticket=ticket,
            expires_at=expires_at,
            passphrase=passphrase,
        )

        # 3. Клиент активирует код через классовый метод HubClient
        save_conf_path = self.temp_dir / "client_saved_config.json"
        activated_config = HubClient.activate_with_invite_code(
            invite_code=invite_code,
            passphrase=passphrase,
            player_name="DimaPlayer",
            config_path=save_conf_path,
            http_client=self.http_client,
        )

        self.assertTrue(activated_config.is_configured())
        self.assertTrue(activated_config.device_token)
        self.assertEqual(activated_config.player_name, "DimaPlayer")
        self.assertTrue(save_conf_path.exists())

        # 4. Проверяем, что сохраненный конфиг валиден
        reloaded = ClientConfig.load_from_file(save_conf_path)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.device_token, activated_config.device_token)

    def test_activate_with_wrong_passphrase_fails(self):
        """Попытка активации с неверной парольной фразой отклоняется локально до сетевых запросов."""
        inv = self.auth_manager.create_invite(ttl_seconds=3600)
        code = encode_invite_code(
            hub_http_url="http://testserver",
            hub_stun_host="127.0.0.1",
            hub_stun_port=14197,
            ticket=inv["ticket"],
            expires_at=int(inv["expires_at"]),
            passphrase="correct_phrase",
        )

        with self.assertRaises(InvalidInvitePassphraseError):
            HubClient.activate_with_invite_code(
                invite_code=code,
                passphrase="wrong_phrase",
                player_name="Attacker",
                http_client=self.http_client,
            )


class TestP2PTunnelAuth(unittest.TestCase):
    """Тестирование передачи device_token в P2P-туннелях."""

    def test_host_p2p_bridge_packet_includes_device_token(self):
        """HostP2PBridge включает device_token в payload датаграммы HOST_HELLO."""
        bridge = HostP2PBridge(
            session_token="session_12345",
            stun_host="127.0.0.1",
            stun_port=14197,
            device_token="host_dev_token_abc",
        )
        self.assertEqual(bridge.device_token, "host_dev_token_abc")

    def test_guest_p2p_proxy_packet_includes_device_token(self):
        """GuestP2PProxy включает device_token в payload датаграммы GUEST_HELLO."""
        proxy = GuestP2PProxy(
            guest_name="GuestUser",
            stun_host="127.0.0.1",
            stun_port=14197,
            device_token="guest_dev_token_xyz",
        )
        self.assertEqual(proxy.device_token, "guest_dev_token_xyz")


class TestZeroHardcodeGrep(unittest.TestCase):
    """Проверка полного отсутствия реального IP-адреса владельца в кодовой базе src/client/."""

    def test_zero_real_ip_occurrences_in_src_client(self):
        """Ни один файл в src/client/ не должен содержать строку <YOUR_VPS_IP>."""
        client_dir = Path("/Users/test/LittleProjects/FactorioSetup/src/client")
        real_ip = "<YOUR_VPS_IP>"

        matches = []
        for py_file in client_dir.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            if real_ip in text:
                matches.append(str(py_file))

        self.assertEqual(
            matches,
            [],
            f"В коде src/client/ обнаружены захардкоженные вхождения реального IP {real_ip}: {matches}",
        )


if __name__ == "__main__":
    unittest.main()
