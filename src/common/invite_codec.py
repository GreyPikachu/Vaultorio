"""Криптографический модуль упаковки и распаковки «Кодов Дружбы» (InviteCodec).

Обеспечивает двухконтурное сокрытие координат сервера (IP/портов) и билета:
1. Координаты сервера и одноразовый билет упаковываются в компактный сжатый JSON.
2. Данные шифруются симметричным потоковым шифром на базе PBKDF2-HMAC-SHA256 (100 000 итераций)
   с короткой кодовой фразой (passphrase), передаваемой владельцем другу лично.
3. Целостность и аутентичность защищены схемой Encrypt-then-MAC (HMAC-SHA256).
4. На выходе формируется компактная строка 'FCT1-<base64url>', полностью исключающая
   появление реального IP-адреса в открытом виде в коде, репозитории GitHub или логах.
Использует исключительно стандартную библиотеку Python (zero external dependencies).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import zlib
from typing import Any, Dict

logger = logging.getLogger("factorio_common.invite_codec")

INVITE_PREFIX: str = "FCT1-"
PBKDF2_ITERATIONS: int = 100_000
SALT_SIZE_BYTES: int = 16
NONCE_SIZE_BYTES: int = 12
MAC_TAG_SIZE_BYTES: int = 16
KEY_MATERIAL_SIZE_BYTES: int = 64
BLOCK_SIZE_BYTES: int = 32  # Размер блока SHA-256


class InviteCodecError(Exception):
    """Базовое исключение для ошибок кодека приглашений."""


class InvalidInviteFormatError(InviteCodecError):
    """Выбрасывается при некорректном формате строки кода приглашения или повреждении Base64."""


class InvalidInvitePassphraseError(InviteCodecError):
    """Выбрасывается при неверной парольной фразе или нарушении целостности HMAC-SHA256."""


class InviteExpiredError(InviteCodecError):
    """Выбрасывается, если срок действия кода приглашения уже истек."""


def _generate_keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    """Генерирует псевдослучайный поток ключей на базе SHA-256 в режиме счетчика (CTR)."""
    keystream = bytearray()
    counter = 0
    while len(keystream) < length:
        counter_bytes = counter.to_bytes(4, byteorder="big")
        block = hashlib.sha256(enc_key + nonce + counter_bytes).digest()
        keystream.extend(block)
        counter += 1
    return bytes(keystream[:length])


def _xor_bytes(data: bytes, keystream: bytes) -> bytes:
    """Выполняет побайтовое XOR-преобразование между данными и потоком ключей."""
    return bytes(b ^ k for b, k in zip(data, keystream))


def encode_invite_code(
    hub_http_url: str,
    hub_stun_host: str,
    hub_stun_port: int,
    ticket: str,
    expires_at: int | float,
    passphrase: str,
) -> str:
    """Шифрует и упаковывает реквизиты подключения и одноразовый билет в компактный код 'FCT1-...'.

    Args:
        hub_http_url: Полный HTTP URL сервера-сейфа (например: 'http://<YOUR_VPS_IP>:8088').
        hub_stun_host: IP или домен STUN-координатора (например: '<YOUR_VPS_IP>').
        hub_stun_port: UDP-порт STUN-координатора (например: 14197).
        ticket: Одноразовый секретный билет авторизации.
        expires_at: Временная метка окончания срока действия (Unix timestamp).
        passphrase: Парольная фраза для вывода криптографических ключей.

    Returns:
        Строка вида 'FCT1-<base64url>'.
    """
    if not passphrase or not passphrase.strip():
        raise ValueError("Парольная фраза (passphrase) не может быть пустой.")

    if not hub_http_url or not hub_stun_host or not ticket:
        raise ValueError("URL сервера, STUN-хост и тикет являются обязательными параметрами.")

    # 1. Формирование компактного JSON-пейлоада
    payload_dict: Dict[str, Any] = {
        "u": hub_http_url.strip(),
        "s": hub_stun_host.strip(),
        "p": int(hub_stun_port),
        "t": ticket.strip(),
        "e": int(expires_at),
    }

    try:
        raw_json = json.dumps(payload_dict, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        compressed_plaintext = zlib.compress(raw_json, level=9)
    except Exception as exc:
        logger.error(f"Ошибка сериализации данных инвайта: {exc}")
        raise InviteCodecError(f"Ошибка сериализации данных инвайта: {exc}") from exc

    # 2. Криптографические случайные векторы
    salt = secrets.token_bytes(SALT_SIZE_BYTES)
    nonce = secrets.token_bytes(NONCE_SIZE_BYTES)

    # 3. Вывод ключей шифрования (enc_key) и аутентификации (mac_key) через PBKDF2-HMAC-SHA256
    clean_pass = passphrase.strip().encode("utf-8")
    key_material = hashlib.pbkdf2_hmac(
        "sha256",
        clean_pass,
        salt,
        PBKDF2_ITERATIONS,
        dklen=KEY_MATERIAL_SIZE_BYTES,
    )
    enc_key = key_material[:32]
    mac_key = key_material[32:]

    # 4. Потоковое шифрование (CTR)
    keystream = _generate_keystream(enc_key, nonce, len(compressed_plaintext))
    ciphertext = _xor_bytes(compressed_plaintext, keystream)

    # 5. Вычисление имитовставки HMAC-SHA256 (Encrypt-then-MAC)
    # Защищает salt, nonce и ciphertext от любых модификаций
    mac_data = salt + nonce + ciphertext
    full_mac = hmac.new(mac_key, mac_data, hashlib.sha256).digest()
    mac_tag = full_mac[:MAC_TAG_SIZE_BYTES]

    # 6. Сборка бинарного конверта: [SALT (16)] + [NONCE (12)] + [CIPHERTEXT] + [TAG (16)]
    envelope = salt + nonce + ciphertext + mac_tag

    # 7. Кодирование в URL-safe Base64 без лишних символов переноса строки
    b64_payload = base64.urlsafe_b64encode(envelope).decode("ascii").rstrip("=")
    invite_code = f"{INVITE_PREFIX}{b64_payload}"

    logger.debug(f"Успешно сгенерирован код приглашения (длина: {len(invite_code)} символов)")
    return invite_code


def decode_invite_code(invite_code: str, passphrase: str) -> Dict[str, Any]:
    """Расшифровывает и проверяет подлинность «Кода Дружбы» 'FCT1-...'.

    Args:
        invite_code: Строка с кодом приглашения (с префиксом 'FCT1-').
        passphrase: Парольная фраза, использованная при шифровании.

    Returns:
        Словарь с ключами:
        - hub_http_url: str
        - hub_stun_host: str
        - hub_stun_port: int
        - ticket: str
        - expires_at: int

    Raises:
        InvalidInviteFormatError: Если строка не имеет префикса 'FCT1-' или поврежден Base64.
        InvalidInvitePassphraseError: Если указан неверный пароль или нарушена целостность данных.
        InviteExpiredError: Если срок действия кода истек по времени.
    """
    if not isinstance(invite_code, str):
        raise InvalidInviteFormatError("Код приглашения должен быть строкой.")

    clean_code = invite_code.strip()
    if not clean_code.startswith(INVITE_PREFIX):
        raise InvalidInviteFormatError(
            f"Некорректный префикс кода приглашения. Ожидался '{INVITE_PREFIX}', получено: '{clean_code[:10]}...'"
        )

    b64_payload = clean_code[len(INVITE_PREFIX):].strip()
    if not b64_payload:
        raise InvalidInviteFormatError("Тело кода приглашения пустое.")

    # Дополняем Base64 padding при необходимости
    padding = "=" * ((4 - len(b64_payload) % 4) % 4)
    try:
        envelope = base64.urlsafe_b64decode(b64_payload + padding)
    except Exception as b64_err:
        logger.warning(f"Ошибка декодирования Base64 в коде приглашения: {b64_err}")
        raise InvalidInviteFormatError("Код приглашения поврежден или имеет неверный формат Base64.") from b64_err

    min_required_len = SALT_SIZE_BYTES + NONCE_SIZE_BYTES + 1 + MAC_TAG_SIZE_BYTES
    if len(envelope) < min_required_len:
        raise InvalidInviteFormatError("Размер зашифрованного пакета меньше минимально допустимого.")

    # Извлечение компонентов
    salt = envelope[:SALT_SIZE_BYTES]
    nonce = envelope[SALT_SIZE_BYTES : SALT_SIZE_BYTES + NONCE_SIZE_BYTES]
    mac_tag = envelope[-MAC_TAG_SIZE_BYTES:]
    ciphertext = envelope[SALT_SIZE_BYTES + NONCE_SIZE_BYTES : -MAC_TAG_SIZE_BYTES]

    # Вывод ключей
    clean_pass = passphrase.strip().encode("utf-8") if passphrase else b""
    key_material = hashlib.pbkdf2_hmac(
        "sha256",
        clean_pass,
        salt,
        PBKDF2_ITERATIONS,
        dklen=KEY_MATERIAL_SIZE_BYTES,
    )
    enc_key = key_material[:32]
    mac_key = key_material[32:]

    # Проверка целостности HMAC-SHA256 (Constant-time comparison)
    mac_data = salt + nonce + ciphertext
    expected_full_mac = hmac.new(mac_key, mac_data, hashlib.sha256).digest()
    expected_tag = expected_full_mac[:MAC_TAG_SIZE_BYTES]

    if not hmac.compare_digest(mac_tag, expected_tag):
        logger.warning("Несовпадение HMAC-тега при расшифровке инвайт-кода (неверный пароль или подмена данных).")
        raise InvalidInvitePassphraseError("Неверная парольная фраза или код приглашения поврежден.")

    # Расшифровка потоковым шифром (CTR)
    keystream = _generate_keystream(enc_key, nonce, len(ciphertext))
    decompressed_bytes = _xor_bytes(ciphertext, keystream)

    # Декомпрессия и парсинг JSON
    try:
        raw_json = zlib.decompress(decompressed_bytes)
        payload_dict = json.loads(raw_json.decode("utf-8"))
    except Exception as dec_err:
        logger.warning(f"Ошибка декомпрессии или парсинга расшифрованных данных: {dec_err}")
        raise InvalidInvitePassphraseError("Не удалось расшифровать данные приглашения.") from dec_err

    if not isinstance(payload_dict, dict):
        raise InvalidInviteFormatError("Содержимое приглашения имеет некорректную структуру.")

    required_keys = {"u", "s", "p", "t", "e"}
    if not required_keys.issubset(payload_dict.keys()):
        missing = required_keys - set(payload_dict.keys())
        raise InvalidInviteFormatError(f"В коде приглашения отсутствуют обязательные поля: {missing}")

    expires_at = int(payload_dict["e"])
    now = time.time()
    if now > expires_at:
        logger.warning(f"Код приглашения просрочен: {now:.0f} > {expires_at} (разница {now - expires_at:.1f}с)")
        raise InviteExpiredError("Срок действия этого кода приглашения истек.")

    return {
        "hub_http_url": str(payload_dict["u"]),
        "hub_stun_host": str(payload_dict["s"]),
        "hub_stun_port": int(payload_dict["p"]),
        "ticket": str(payload_dict["t"]),
        "expires_at": expires_at,
    }
