"""
Универсальный набор тестов подсистемы авторизации и «Кодов Дружбы» (Task 013).
Проверяет:
1. Криптографический модуль encode/decode_invite_code (FCT1-..., Encrypt-then-MAC, сокрытие IP, защита от перебора).
2. Серверный менеджер AuthManager (одноразовые билеты, rate limiting, сгорание билетов, Stealth Mode, отзыв).
3. HTTP REST API эндпоинты под защитой X-Device-Token и публичный /auth/redeem-invite.
4. UDP STUN-координатор в Stealth Mode (молчаливый сброс пакетов от неавторизованных адресов).
5. Консольную утилиту администратора admin_cli (create-invite, list, revoke, cleanup).
"""

import asyncio
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Tuple

from fastapi.testclient import TestClient

from src.common.invite_codec import (
    INVITE_PREFIX,
    InvalidInviteFormatError,
    InvalidInvitePassphraseError,
    InviteExpiredError,
    decode_invite_code,
    encode_invite_code,
)
from src.server.admin_cli import main as admin_cli_main
from src.server.api import create_app
from src.server.auth_manager import (
    AuthManager,
    InvalidInviteTicketError,
    InviteTicketAlreadyUsedError,
    InviteTicketExpiredError,
    RateLimitExceededError,
)
from src.server.config import ServerConfig
from src.server.session_manager import SessionLockManager
from src.server.storage import SaveStorageManager
from src.server.stun_coordinator import (
    HEADER_CONTROL,
    MAGIC_PREFIX,
    StunCoordinatorProtocol,
)


class DummyStunClientProtocol(asyncio.DatagramProtocol):
    """Тестовый клиентский UDP-протокол для проверки пакетов STUN."""

    def __init__(self):
        self.queue: asyncio.Queue[Tuple[bytes, Tuple[str, int]]] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        self.queue.put_nowait((data, addr))

    async def wait_for_packet(self, timeout: float = 0.5) -> bytes:
        """Ожидает поступления датаграммы, либо выбрасывает TimeoutError при Stealth Mode."""
        data, _ = await asyncio.wait_for(self.queue.get(), timeout=timeout)
        return data


class TestInviteCodec(unittest.TestCase):
    """Тестирование криптографического кодека инвайт-кодов (invite_codec.py)."""

    def setUp(self):
        self.hub_url = "http://<YOUR_VPS_IP>:8088"
        self.stun_host = "<YOUR_VPS_IP>"
        self.stun_port = 14197
        self.ticket = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        self.passphrase = "суперсекрет2026"
        self.expires_at = int(time.time()) + 3600

    def test_encode_decode_roundtrip(self):
        """Успешное прямое и обратное преобразование при верной парольной фразе."""
        code = encode_invite_code(
            hub_http_url=self.hub_url,
            hub_stun_host=self.stun_host,
            hub_stun_port=self.stun_port,
            ticket=self.ticket,
            expires_at=self.expires_at,
            passphrase=self.passphrase,
        )

        self.assertTrue(code.startswith(INVITE_PREFIX))
        # Проверяем, что в открытом виде IP-адрес не содержится
        self.assertNotIn(self.stun_host, code)
        self.assertNotIn("8088", code)

        decoded = decode_invite_code(code, self.passphrase)
        self.assertEqual(decoded["hub_http_url"], self.hub_url)
        self.assertEqual(decoded["hub_stun_host"], self.stun_host)
        self.assertEqual(decoded["hub_stun_port"], self.stun_port)
        self.assertEqual(decoded["ticket"], self.ticket)
        self.assertEqual(decoded["expires_at"], self.expires_at)

    def test_code_does_not_leak_ip_in_base64(self):
        """Проверка, что тело кода зашифровано, а не является простым Base64 от JSON."""
        code = encode_invite_code(
            hub_http_url=self.hub_url,
            hub_stun_host=self.stun_host,
            hub_stun_port=self.stun_port,
            ticket=self.ticket,
            expires_at=self.expires_at,
            passphrase=self.passphrase,
        )
        body = code[len(INVITE_PREFIX):]
        # Даже если сырой base64 декодировать, там не должно быть открытого текста JSON
        import base64
        raw = base64.urlsafe_b64decode(body + "==")
        self.assertNotIn(b"<YOUR_VPS_IP>", raw)
        self.assertNotIn(b"hub_http_url", raw)

    def test_decode_with_wrong_passphrase(self):
        """Попытка расшифровки с неверным паролем должна приводить к InvalidInvitePassphraseError."""
        code = encode_invite_code(
            hub_http_url=self.hub_url,
            hub_stun_host=self.stun_host,
            hub_stun_port=self.stun_port,
            ticket=self.ticket,
            expires_at=self.expires_at,
            passphrase=self.passphrase,
        )
        with self.assertRaises(InvalidInvitePassphraseError):
            decode_invite_code(code, "неправильный_пароль")

    def test_decode_with_tampered_ciphertext(self):
        """Модификация даже одного байта в теле кода должна отвергаться HMAC (Encrypt-then-MAC)."""
        code = encode_invite_code(
            hub_http_url=self.hub_url,
            hub_stun_host=self.stun_host,
            hub_stun_port=self.stun_port,
            ticket=self.ticket,
            expires_at=self.expires_at,
            passphrase=self.passphrase,
        )
        # Подменяем символ в середине тела
        body = list(code[len(INVITE_PREFIX):])
        body[15] = "A" if body[15] != "A" else "B"
        tampered_code = INVITE_PREFIX + "".join(body)

        with self.assertRaises((InvalidInvitePassphraseError, InvalidInviteFormatError)):
            decode_invite_code(tampered_code, self.passphrase)

    def test_decode_with_bad_prefix(self):
        """Код без правильного префикса FCT1- должен вызывать InvalidInviteFormatError."""
        with self.assertRaises(InvalidInviteFormatError):
            decode_invite_code("BADPREFIX-abc12345", self.passphrase)

    def test_decode_with_truncated_data(self):
        """Слишком короткое тело кода должно вызывать InvalidInviteFormatError."""
        with self.assertRaises(InvalidInviteFormatError):
            decode_invite_code("FCT1-YWJj", self.passphrase)

    def test_decode_expired_code(self):
        """Код с истекшим сроком действия должен вызывать InviteExpiredError до сетевых запросов."""
        past_timestamp = int(time.time()) - 100
        code = encode_invite_code(
            hub_http_url=self.hub_url,
            hub_stun_host=self.stun_host,
            hub_stun_port=self.stun_port,
            ticket=self.ticket,
            expires_at=past_timestamp,
            passphrase=self.passphrase,
        )
        with self.assertRaises(InviteExpiredError):
            decode_invite_code(code, self.passphrase)

    def test_encode_parameter_validations(self):
        """Валидация пустых аргументов при кодировании."""
        with self.assertRaises(ValueError):
            encode_invite_code("", self.stun_host, self.stun_port, self.ticket, self.expires_at, self.passphrase)
        with self.assertRaises(ValueError):
            encode_invite_code(self.hub_url, self.stun_host, self.stun_port, self.ticket, self.expires_at, "")


class TestAuthManager(unittest.TestCase):
    """Тестирование серверного менеджера авторизации и инвайтов (auth_manager.py)."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_auth_mgr_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            require_device_auth=True,
        )
        self.auth_mgr = AuthManager(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_invite_and_persistence(self):
        """Создание билета и его сохранение в auth_state.json."""
        inv = self.auth_mgr.create_invite(ttl_seconds=1800, max_uses=1, note="Тест для Димы")
        self.assertIn("ticket", inv)
        self.assertEqual(inv["max_uses"], 1)
        self.assertEqual(inv["uses_count"], 0)
        self.assertEqual(inv["note"], "Тест для Димы")

        # Проверяем персистентность файла
        state_file = self.temp_dir / "auth_state.json"
        self.assertTrue(state_file.exists())

        # Загружаем новый экземпляр менеджера
        reloaded_mgr = AuthManager(self.config)
        loaded_inv = reloaded_mgr.get_invite(inv["ticket"])
        self.assertIsNotNone(loaded_inv)
        self.assertEqual(loaded_inv["note"], "Тест для Димы")

    def test_create_invite_invalid_params(self):
        """Валидация некорректных параметров создания инвайта."""
        with self.assertRaises(ValueError):
            self.auth_mgr.create_invite(ttl_seconds=0)
        with self.assertRaises(ValueError):
            self.auth_mgr.create_invite(max_uses=0)

    def test_redeem_invite_success_and_ticket_burn(self):
        """Успешная активация одноразового билета и его мгновенное сгорание."""
        inv = self.auth_mgr.create_invite(ttl_seconds=3600, max_uses=1, note="Друг")
        ticket = inv["ticket"]

        res = self.auth_mgr.redeem_invite(ticket=ticket, player_name="Dima", client_ip="192.168.1.100")
        self.assertTrue(res["success"])
        self.assertEqual(res["player_name"], "Dima")
        device_token = res["device_token"]
        self.assertIsNotNone(device_token)

        # Проверяем валидность полученного токена устройства
        self.assertTrue(self.auth_mgr.validate_device_token(device_token, client_ip="192.168.1.100"))

        # Повторная попытка активации того же билета должна выбрасывать InviteTicketAlreadyUsedError
        with self.assertRaises(InviteTicketAlreadyUsedError):
            self.auth_mgr.redeem_invite(ticket=ticket, player_name="Dima2", client_ip="192.168.1.100")

    def test_redeem_invite_multi_use(self):
        """Билет с max_uses=2 можно активировать ровно два раза."""
        inv = self.auth_mgr.create_invite(ttl_seconds=3600, max_uses=2, note="Два игрока")
        ticket = inv["ticket"]

        res1 = self.auth_mgr.redeem_invite(ticket=ticket, player_name="Player1", client_ip="1.1.1.1")
        res2 = self.auth_mgr.redeem_invite(ticket=ticket, player_name="Player2", client_ip="2.2.2.2")
        self.assertNotEqual(res1["device_token"], res2["device_token"])

        # Третий раз активировать нельзя
        with self.assertRaises(InviteTicketAlreadyUsedError):
            self.auth_mgr.redeem_invite(ticket=ticket, player_name="Player3", client_ip="3.3.3.3")

    def test_redeem_invite_nonexistent(self):
        """Попытка активации несуществующего билета."""
        with self.assertRaises(InvalidInviteTicketError):
            self.auth_mgr.redeem_invite("nonexistent_ticket_123", player_name="Hacker", client_ip="8.8.8.8")

    def test_redeem_invite_expired(self):
        """Попытка активации просроченного билета."""
        inv = self.auth_mgr.create_invite(ttl_seconds=1, max_uses=1)
        ticket = inv["ticket"]
        time.sleep(1.05)

        with self.assertRaises(InviteTicketExpiredError):
            self.auth_mgr.redeem_invite(ticket=ticket, player_name="LatePlayer", client_ip="1.2.3.4")

    def test_rate_limiting_bruteforce(self):
        """Защита от подбора билетов: превышение 5 неудачных попыток в минуту блокирует IP."""
        client_ip = "198.51.100.55"
        for _ in range(5):
            with self.assertRaises(InvalidInviteTicketError):
                self.auth_mgr.redeem_invite("bad_ticket", player_name="Attacker", client_ip=client_ip)

        # 6-я попытка должна вызвать RateLimitExceededError
        with self.assertRaises(RateLimitExceededError):
            self.auth_mgr.redeem_invite("bad_ticket", player_name="Attacker", client_ip=client_ip)

    def test_validate_device_token_stealth_mode(self):
        """Проверка токенов в Stealth Mode."""
        self.assertFalse(self.auth_mgr.validate_device_token(None))
        self.assertFalse(self.auth_mgr.validate_device_token(""))
        self.assertFalse(self.auth_mgr.validate_device_token("unknown_token_value"))

        inv = self.auth_mgr.create_invite(ttl_seconds=3600)
        res = self.auth_mgr.redeem_invite(inv["ticket"], player_name="ValidPlayer")
        token = res["device_token"]

        self.assertTrue(self.auth_mgr.validate_device_token(token))

        # При require_device_auth=False токен валиден всегда (обратная совместимость)
        open_config = ServerConfig(storage_dir=self.temp_dir, require_device_auth=False)
        open_mgr = AuthManager(open_config)
        self.assertTrue(open_mgr.validate_device_token(None))
        self.assertTrue(open_mgr.validate_device_token("random_noise"))

    def test_revoke_device(self):
        """Отзыв доступа у устройства по player_name, device_id или токену."""
        inv = self.auth_mgr.create_invite(ttl_seconds=3600)
        res = self.auth_mgr.redeem_invite(inv["ticket"], player_name="PlayerToRevoke")
        token = res["device_token"]
        device_id = res["device_id"]

        self.assertTrue(self.auth_mgr.validate_device_token(token))

        # Отзываем по имени игрока
        revoked = self.auth_mgr.revoke_device("playertorevoke")
        self.assertTrue(revoked)
        self.assertFalse(self.auth_mgr.validate_device_token(token))

        # Повторный отзыв возвращает False
        self.assertFalse(self.auth_mgr.revoke_device("playertorevoke"))

        # Проверяем отзыв по device_id для другого устройства
        inv2 = self.auth_mgr.create_invite(ttl_seconds=3600)
        res2 = self.auth_mgr.redeem_invite(inv2["ticket"], player_name="Player2")
        self.assertTrue(self.auth_mgr.revoke_device(res2["device_id"]))
        self.assertFalse(self.auth_mgr.validate_device_token(res2["device_token"]))

    def test_list_and_cleanup(self):
        """Формирование сводного отчета и очистка устаревших билетов."""
        inv1 = self.auth_mgr.create_invite(ttl_seconds=3600, note="Note1")
        self.auth_mgr.redeem_invite(inv1["ticket"], player_name="ActivePlayer")

        # Просроченный инвайт
        inv2 = self.auth_mgr.create_invite(ttl_seconds=1, note="ExpiredNote")
        time.sleep(1.05)

        report = self.auth_mgr.list_devices_and_invites()
        self.assertTrue(report["require_device_auth"])
        self.assertEqual(report["total_invites"], 2)
        self.assertEqual(report["total_devices"], 1)

        # Очистка устаревших старше 0 секунд
        cleaned = self.auth_mgr.cleanup_expired_invites(older_than_seconds=0)
        self.assertEqual(cleaned, 1)

        report_after = self.auth_mgr.list_devices_and_invites()
        self.assertEqual(report_after["total_invites"], 1)


class TestApiAuth(unittest.TestCase):
    """Интеграционные тесты HTTP REST API с включенной авторизацией устройств."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_api_auth_"))
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            require_device_auth=True,
            http_port=8088,
            udp_stun_port=14197,
        )
        self.storage = SaveStorageManager(self.config)
        self.session_manager = SessionLockManager(self.config)
        self.auth_manager = AuthManager(self.config)

        self.app = create_app(
            config=self.config,
            storage=self.storage,
            session_manager=self.session_manager,
            auth_manager=self.auth_manager,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_blocked_without_token(self):
        """Запросы к защищенным эндпоинтам без заголовка X-Device-Token возвращают 403 Forbidden."""
        # /status
        resp = self.client.get("/status")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Forbidden", resp.json()["detail"])

        # /claim
        resp = self.client.post("/claim", json={"player_name": "Ghost"})
        self.assertEqual(resp.status_code, 403)

        # /download-save
        resp = self.client.get("/download-save")
        self.assertEqual(resp.status_code, 403)

    def test_blocked_with_invalid_token(self):
        """Запросы с недействительным X-Device-Token возвращают 403 Forbidden."""
        resp = self.client.get("/status", headers={"X-Device-Token": "invalid_token_12345"})
        self.assertEqual(resp.status_code, 403)

    def test_public_redeem_invite_flow(self):
        """Публичный эндпоинт /auth/redeem-invite доступен без токена и активирует доступ."""
        inv = self.auth_manager.create_invite(ttl_seconds=3600, note="Тестовый билет")
        ticket = inv["ticket"]

        # 1. Активируем инвайт через HTTP
        redeem_resp = self.client.post(
            "/auth/redeem-invite",
            json={"ticket": ticket, "player_name": "TestFriend"},
        )
        self.assertEqual(redeem_resp.status_code, 200)
        data = redeem_resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["player_name"], "TestFriend")
        device_token = data["device_token"]
        self.assertTrue(device_token)

        # 2. Повторная активация того же билета отклоняется (одноразовость)
        redeem_again = self.client.post(
            "/auth/redeem-invite",
            json={"ticket": ticket, "player_name": "TestFriend2"},
        )
        self.assertEqual(redeem_again.status_code, 403)

        # 3. Доступ к защищенному эндпоинту /status с полученным токеном успешен
        status_resp = self.client.get("/status", headers={"X-Device-Token": device_token})
        self.assertEqual(status_resp.status_code, 200)
        status_data = status_resp.json()
        self.assertEqual(status_data["state"], "idle")

    def test_backward_compatibility_when_auth_disabled(self):
        """При require_device_auth=False сервер полностью открыт (обратная совместимость)."""
        open_config = ServerConfig(
            storage_dir=self.temp_dir,
            require_device_auth=False,
        )
        open_storage = SaveStorageManager(open_config)
        open_session = SessionLockManager(open_config)
        open_auth = AuthManager(open_config)

        open_app = create_app(
            config=open_config,
            storage=open_storage,
            session_manager=open_session,
            auth_manager=open_auth,
        )
        client = TestClient(open_app)

        resp = client.get("/status")
        self.assertEqual(resp.status_code, 200)


class TestStunCoordinatorAuth(unittest.IsolatedAsyncioTestCase):
    """Тестирование STUN-координатора с авторизацией устройств в Stealth Mode."""

    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_stun_auth_"))
        self.test_port = 14199
        self.config = ServerConfig(
            storage_dir=self.temp_dir,
            udp_stun_port=self.test_port,
            require_device_auth=True,
        )
        self.session_manager = SessionLockManager(self.config)
        self.auth_manager = AuthManager(self.config)

        # Создаем авторизованное устройство
        inv = self.auth_manager.create_invite(ttl_seconds=3600)
        res = self.auth_manager.redeem_invite(inv["ticket"], player_name="StunPlayer")
        self.valid_device_token = res["device_token"]

        self.loop = asyncio.get_running_loop()

        # Сервер STUN
        self.server_transport, self.server_protocol = await self.loop.create_datagram_endpoint(
            lambda: StunCoordinatorProtocol(
                self.config,
                self.session_manager,
                auth_manager=self.auth_manager,
            ),
            local_addr=("127.0.0.1", self.test_port),
        )

        # Клиент STUN
        self.client_transport, self.client_protocol = await self.loop.create_datagram_endpoint(
            lambda: DummyStunClientProtocol(),
            local_addr=("127.0.0.1", 0),
        )

    async def asyncTearDown(self):
        if self.client_transport:
            self.client_transport.close()
        if self.server_transport:
            self.server_transport.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_stealth_drop_unauthorized_host_hello(self):
        """Неавторизованный HOST_HELLO молча отбрасывается без ответа (Stealth Mode)."""
        # Сначала клеймим сессию на сессионном менеджере
        claim_res = self.session_manager.claim_host(player_name="StunPlayer", client_public_ip="127.0.0.1")
        token = claim_res["session_token"]

        # Пакет без device_token
        packet_no_auth = HEADER_CONTROL + json.dumps({
            "cmd": "HOST_HELLO",
            "session_token": token,
        }).encode("utf-8")

        self.client_transport.sendto(packet_no_auth, ("127.0.0.1", self.test_port))

        # Ожидаем таймаут: сервер не должен отправлять ответ чужаку
        with self.assertRaises(asyncio.TimeoutError):
            await self.client_protocol.wait_for_packet(timeout=0.3)

    async def test_authorized_host_hello_receives_ack(self):
        """Авторизованный HOST_HELLO с правильным device_token получает HOST_ACK."""
        claim_res = self.session_manager.claim_host(player_name="StunPlayer", client_public_ip="127.0.0.1")
        token = claim_res["session_token"]

        packet_auth = HEADER_CONTROL + json.dumps({
            "cmd": "HOST_HELLO",
            "session_token": token,
            "device_token": self.valid_device_token,
        }).encode("utf-8")

        self.client_transport.sendto(packet_auth, ("127.0.0.1", self.test_port))

        resp_bytes = await self.client_protocol.wait_for_packet(timeout=1.0)
        self.assertTrue(resp_bytes.startswith(HEADER_CONTROL))
        resp_json = json.loads(resp_bytes[len(HEADER_CONTROL):].decode("utf-8"))
        self.assertEqual(resp_json.get("cmd"), "HOST_ACK")


class TestAdminCLI(unittest.TestCase):
    """Тестирование CLI-утилиты администратора (admin_cli.py)."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_admin_cli_"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cli_create_invite_and_list(self):
        """Проверка генерации инвайта и вывода списка через CLI."""
        out = io.StringIO()
        err = io.StringIO()

        # 1. create-invite
        with redirect_stdout(out), redirect_stderr(err):
            code = admin_cli_main([
                "--storage-dir", str(self.temp_dir),
                "create-invite",
                "-p", "testpassphrase",
                "--ttl-minutes", "30",
                "--note", "CLI Friend",
                "--raw-code-only",
            ])
        self.assertEqual(code, 0)
        stdout_val = out.getvalue()
        self.assertIn("FCT1-", stdout_val)
        self.assertIn("RAW_CODE:FCT1-", stdout_val)

        # Извлекаем сырой код
        raw_code = [line for line in stdout_val.splitlines() if line.startswith("RAW_CODE:")][0].split(":", 1)[1].strip()

        # 2. Проверяем, что полученный код успешно расшифровывается кодеком
        decoded = decode_invite_code(raw_code, "testpassphrase")
        self.assertEqual(decoded["hub_stun_port"], 14197)

        # 3. list (текстовый)
        out_list = io.StringIO()
        with redirect_stdout(out_list):
            code_list = admin_cli_main([
                "--storage-dir", str(self.temp_dir),
                "list",
            ])
        self.assertEqual(code_list, 0)
        self.assertIn("БИЛЕТЫ ПРИГЛАШЕНИЙ", out_list.getvalue())
        self.assertIn("CLI Friend", out_list.getvalue())

        # 4. list (JSON)
        out_json = io.StringIO()
        with redirect_stdout(out_json):
            code_json = admin_cli_main([
                "--storage-dir", str(self.temp_dir),
                "list",
                "--json",
            ])
        self.assertEqual(code_json, 0)
        parsed_report = json.loads(out_json.getvalue())
        self.assertEqual(parsed_report["total_invites"], 1)

    def test_cli_revoke_and_cleanup(self):
        """Проверка отзыва устройства и очистки инвайтов через CLI."""
        config = ServerConfig(storage_dir=self.temp_dir, require_device_auth=True)
        auth_mgr = AuthManager(config)
        inv = auth_mgr.create_invite(ttl_seconds=3600)
        auth_mgr.redeem_invite(inv["ticket"], player_name="CliPlayer")

        # Отзыв
        out_rev = io.StringIO()
        with redirect_stdout(out_rev):
            code_rev = admin_cli_main([
                "--storage-dir", str(self.temp_dir),
                "revoke",
                "CliPlayer",
            ])
        self.assertEqual(code_rev, 0)
        self.assertIn("успешно отозван", out_rev.getvalue())

        # Очистка
        out_cln = io.StringIO()
        with redirect_stdout(out_cln):
            code_cln = admin_cli_main([
                "--storage-dir", str(self.temp_dir),
                "cleanup",
                "--days", "0",
            ])
        self.assertEqual(code_cln, 0)
        self.assertIn("Успех: Удалено", out_cln.getvalue())


if __name__ == "__main__":
    unittest.main()
