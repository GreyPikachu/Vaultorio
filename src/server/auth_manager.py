"""Модуль управления приглашениями и доверенными устройствами (AuthManager).

Реализует:
1. Создание и валидацию одноразовых/временных билетов приглашений (tickets).
2. Активацию билетов (redeem) с мгновенным сгоранием и выдачей постоянного device_token.
3. Защиту от перебора билетов (Rate limiting: не более 5 ошибок в минуту на IP).
4. Проверку токенов устройств (validate_device_token) в Stealth Mode.
5. Отзыв доступа у устройств (revoke_device) и персистентность в auth_state.json.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.server.config import ServerConfig
from src.server.logger import get_logger

logger = get_logger("auth_manager")


class AuthError(Exception):
    """Базовое исключение подсистемы авторизации."""


class InvalidInviteTicketError(AuthError):
    """Выбрасывается, если билет приглашения не найден или некорректен."""


class InviteTicketExpiredError(AuthError):
    """Выбрасывается, если срок действия билета приглашения истек."""


class InviteTicketAlreadyUsedError(AuthError):
    """Выбрасывается, если одноразовый билет уже был активирован ранее."""


class RateLimitExceededError(AuthError):
    """Выбрасывается при превышении допустимого числа неудачных попыток ввода билета."""


class AuthManager:
    """Потокобезопасный менеджер инвайтов и доверенных устройств."""

    MAX_FAILED_ATTEMPTS_PER_WINDOW: int = 5
    RATE_LIMIT_WINDOW_SECONDS: float = 60.0

    def __init__(self, config: ServerConfig) -> None:
        """Инициализирует менеджер авторизации.

        Args:
            config: Конфигурация сервера.
        """
        self.config = config
        self.state_file_path: Path = config.auth_state_path
        self._lock = threading.RLock()

        self._invites: Dict[str, Dict[str, Any]] = {}
        self._trusted_devices: Dict[str, Dict[str, Any]] = {}

        # Словарь учета неудачных попыток ввода для защиты от перебора: client_ip -> [timestamp, ...]
        self._failed_attempts: Dict[str, List[float]] = {}
        self._last_save_time: float = 0.0
        self._last_loaded_mtime: float = 0.0

        self._load_state_from_disk()

    @property
    def require_auth(self) -> bool:
        """Флаг обязательности авторизации устройств."""
        return bool(self.config.require_device_auth)

    def _load_state_from_disk(self) -> None:
        """Загружает сохраненное состояние из auth_state.json."""
        with self._lock:
            if not self.state_file_path.exists():
                logger.info(f"Файл {self.state_file_path} отсутствует. Инициализация пустого состояния авторизации.")
                self._persist_state_locked()
                return

            try:
                mtime = self.state_file_path.stat().st_mtime
                with open(self.state_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict):
                    self._invites = data.get("invites", {})
                    self._trusted_devices = data.get("trusted_devices", {})
                    self._last_loaded_mtime = mtime
                    logger.info(
                        f"Загружено состояние авторизации: инвайтов={len(self._invites)}, "
                        f"доверенных устройств={len(self._trusted_devices)}"
                    )
            except Exception as exc:
                logger.error(f"Ошибка чтения {self.state_file_path}: {exc}. Создается резервная копия.")
                try:
                    corrupt_backup = self.state_file_path.with_suffix(f".corrupt_{int(time.time())}")
                    self.state_file_path.rename(corrupt_backup)
                    logger.warning(f"Поврежденный файл сохранен как {corrupt_backup.name}")
                except Exception:
                    pass
                self._persist_state_locked()

    def _reload_if_modified_locked(self) -> None:
        """Перечитывает состояние с диска, если файл был изменен внешним процессом (например, admin_cli)."""
        try:
            if self.state_file_path.is_file():
                current_mtime = self.state_file_path.stat().st_mtime
                if current_mtime > self._last_loaded_mtime:
                    logger.info("Обнаружено изменение auth_state.json на диске. Выполняется автоматическая перезагрузка.")
                    self._load_state_from_disk()
        except Exception as e:
            logger.debug(f"Ошибка проверки времени изменения {self.state_file_path}: {e}")

    def _persist_state_locked(self) -> None:
        """Атомарно сохраняет состояние инвайтов и устройств на диск через временный файл."""
        try:
            self.state_file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "invites": self._invites,
                "trusted_devices": self._trusted_devices,
                "saved_at": time.time(),
            }

            temp_dir = self.config.tmp_dir
            temp_dir.mkdir(parents=True, exist_ok=True)

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=temp_dir,
                delete=False,
                prefix="auth_state_",
                suffix=".tmp",
            ) as tmp_f:
                json.dump(payload, tmp_f, ensure_ascii=False, indent=2)
                temp_path = Path(tmp_f.name)

            os.replace(temp_path, self.state_file_path)
            self._last_save_time = time.time()
            try:
                self._last_loaded_mtime = self.state_file_path.stat().st_mtime
            except Exception:
                pass
            logger.debug("Состояние авторизации успешно сохранено на диск.")
        except Exception as exc:
            logger.error(f"Ошибка сохранения состояния авторизации в {self.state_file_path}: {exc}")

    def _check_rate_limit_locked(self, client_ip: str) -> None:
        """Проверяет, не превысил ли клиент лимит неудачных попыток активации билета."""
        if not client_ip:
            return

        now = time.time()
        attempts = self._failed_attempts.get(client_ip, [])
        # Оставляем только попытки за последние 60 секунд
        recent_attempts = [ts for ts in attempts if now - ts < self.RATE_LIMIT_WINDOW_SECONDS]
        self._failed_attempts[client_ip] = recent_attempts

        if len(recent_attempts) >= self.MAX_FAILED_ATTEMPTS_PER_WINDOW:
            logger.warning(
                f"Rate limit превышен для IP {client_ip}: {len(recent_attempts)} "
                f"неудачных попыток за последние {self.RATE_LIMIT_WINDOW_SECONDS} сек."
            )
            raise RateLimitExceededError(
                "Слишком много неудачных попыток ввода инвайта. Пожалуйста, подождите 60 секунд."
            )

    def _record_failed_attempt_locked(self, client_ip: Optional[str]) -> None:
        """Фиксирует неудачную попытку ввода инвайта для rate limit."""
        if not client_ip:
            return
        now = time.time()
        if client_ip not in self._failed_attempts:
            self._failed_attempts[client_ip] = []
        self._failed_attempts[client_ip].append(now)

    def create_invite(
        self,
        ttl_seconds: int = 3600,
        max_uses: int = 1,
        note: str = "",
        created_by: str = "admin",
    ) -> Dict[str, Any]:
        """Создает новый билет приглашения.

        Args:
            ttl_seconds: Время жизни билета в секундах (по умолчанию 1 час).
            max_uses: Максимальное количество активаций (по умолчанию 1 — одноразовый).
            note: Заметка (например, имя друга: 'Для Димы').
            created_by: Инициатор создания.

        Returns:
            Словарь с данными билета.
        """
        if ttl_seconds <= 0:
            raise ValueError("Время жизни инвайта (ttl_seconds) должно быть больше нуля.")
        if max_uses <= 0:
            raise ValueError("Число использований (max_uses) должно быть больше нуля.")

        with self._lock:
            # 16 байт = 32 шестнадцатеричных символа криптостойкой энтропии
            ticket = secrets.token_hex(16)
            now = time.time()
            expires_at = now + ttl_seconds

            invite_data: Dict[str, Any] = {
                "ticket": ticket,
                "created_at": now,
                "expires_at": expires_at,
                "max_uses": max_uses,
                "uses_count": 0,
                "note": note.strip() if note else "",
                "created_by": created_by.strip() if created_by else "admin",
            }

            self._invites[ticket] = invite_data
            self._persist_state_locked()

            logger.info(
                f"Создан новый инвайт {ticket[:8]}... (TTL: {ttl_seconds}с, max_uses: {max_uses}, note: '{note}')"
            )
            return invite_data

    def get_invite(self, ticket: str) -> Optional[Dict[str, Any]]:
        """Возвращает информацию о билете по его значению."""
        with self._lock:
            return self._invites.get(ticket)

    def redeem_invite(
        self,
        ticket: str,
        player_name: str,
        client_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Активирует одноразовый билет приглашения и регистрирует доверенное устройство.

        Args:
            ticket: Билет приглашения.
            player_name: Никнейм игрока.
            client_ip: IP-адрес клиента (для rate-limiting и аудита).

        Returns:
            Словарь с выданным device_token, device_id и player_name.

        Raises:
            RateLimitExceededError: Если превышен лимит попыток с этого IP.
            InvalidInviteTicketError: Если билет не существует.
            InviteTicketExpiredError: Если срок действия билета истек.
            InviteTicketAlreadyUsedError: Если билет уже исчерпал лимит использований.
        """
        clean_ticket = (ticket or "").strip()
        clean_player = (player_name or "").strip()
        ip_addr = (client_ip or "").strip()

        if not clean_ticket:
            raise ValueError("Билет приглашения (ticket) не может быть пустым.")
        if not clean_player:
            raise ValueError("Имя игрока (player_name) не может быть пустым.")

        with self._lock:
            # 1. Синхронизация с диском при внешних изменениях (например, от admin_cli)
            self._reload_if_modified_locked()

            # 2. Проверка Rate Limit
            if ip_addr:
                self._check_rate_limit_locked(ip_addr)

            # 3. Поиск билета
            invite = self._invites.get(clean_ticket)
            if not invite:
                self._record_failed_attempt_locked(ip_addr)
                logger.warning(f"Попытка активации несуществующего билета '{clean_ticket[:8]}...' с IP {ip_addr}")
                raise InvalidInviteTicketError("Указан недействительный билет приглашения.")

            # 3. Проверка срока действия
            now = time.time()
            if now > invite.get("expires_at", 0):
                self._record_failed_attempt_locked(ip_addr)
                logger.warning(f"Попытка активации просроченного билета '{clean_ticket[:8]}...' с IP {ip_addr}")
                raise InviteTicketExpiredError("Срок действия этого билета приглашения истек.")

            # 4. Проверка одноразовости (исчерпания лимита)
            max_uses = invite.get("max_uses", 1)
            uses_count = invite.get("uses_count", 0)
            if uses_count >= max_uses:
                self._record_failed_attempt_locked(ip_addr)
                logger.warning(
                    f"Попытка повторного использования исчерпанного билета '{clean_ticket[:8]}...' "
                    f"({uses_count}/{max_uses}) с IP {ip_addr}"
                )
                raise InviteTicketAlreadyUsedError("Этот билет приглашения уже был активирован ранее.")

            # 5. Успешная активация: сжигаем билет
            invite["uses_count"] = uses_count + 1

            # 6. Генерация постоянного токена доверенного устройства
            device_token = secrets.token_hex(32)
            device_id = str(uuid.uuid4())

            device_record: Dict[str, Any] = {
                "device_id": device_id,
                "device_token": device_token,
                "player_name": clean_player,
                "activated_at": now,
                "last_seen": now,
                "last_ip": ip_addr or "unknown",
                "revoked": False,
                "ticket_used": clean_ticket[:8] + "...",
                "note": invite.get("note", ""),
            }

            self._trusted_devices[device_token] = device_record
            self._persist_state_locked()

            logger.info(
                f"Успешная активация инвайта '{clean_ticket[:8]}...': игрок '{clean_player}', "
                f"device_id={device_id}, IP={ip_addr}"
            )

            return {
                "success": True,
                "device_token": device_token,
                "device_id": device_id,
                "player_name": clean_player,
            }

    def validate_device_token(
        self,
        device_token: Optional[str],
        client_ip: Optional[str] = None,
    ) -> bool:
        """Проверяет валидность токена устройства в Stealth Mode.

        Args:
            device_token: Переданный токен устройства.
            client_ip: IP клиента для обновления времени активности.

        Returns:
            True, если токен валиден и устройство активно, иначе False.
        """
        # Если авторизация выключена в конфигурации — доступ открыт
        if not self.require_auth:
            return True

        if not device_token or not isinstance(device_token, str):
            return False

        clean_token = device_token.strip()
        if not clean_token:
            return False

        with self._lock:
            self._reload_if_modified_locked()
            # Ищем токен с защитой от тайминг-атак
            matched_record: Optional[Dict[str, Any]] = None
            for stored_token, record in self._trusted_devices.items():
                if secrets.compare_digest(stored_token, clean_token):
                    matched_record = record
                    break

            if matched_record is None:
                return False

            if matched_record.get("revoked", False):
                logger.warning(
                    f"Доступ отклонен: устройство {matched_record.get('player_name')} "
                    f"({matched_record.get('device_id')}) отозвано администратором."
                )
                return False

            # Обновляем last_seen
            now = time.time()
            if now - matched_record.get("last_seen", 0) > 60.0:
                matched_record["last_seen"] = now
                if client_ip:
                    matched_record["last_ip"] = client_ip
                # Периодически сбрасываем на диск (раз в 5 минут)
                if now - self._last_save_time > 300.0:
                    self._persist_state_locked()

            return True

    def revoke_device(self, identifier: str) -> bool:
        """Отзывает доступ у устройства по токену, player_name или device_id.

        Args:
            identifier: Токен, имя игрока или UUID устройства.

        Returns:
            True, если хотя бы одно устройство было отозвано, иначе False.
        """
        clean_id = (identifier or "").strip()
        if not clean_id:
            return False

        revoked_count = 0
        with self._lock:
            for stored_token, record in self._trusted_devices.items():
                if (
                    secrets.compare_digest(stored_token, clean_id)
                    or record.get("player_name", "").lower() == clean_id.lower()
                    or record.get("device_id") == clean_id
                ):
                    if not record.get("revoked", False):
                        record["revoked"] = True
                        revoked_count += 1
                        logger.info(
                            f"Отозван доступ для устройства: игрок='{record.get('player_name')}', "
                            f"id={record.get('device_id')}"
                        )

            if revoked_count > 0:
                self._persist_state_locked()
                return True

        return False

    def list_devices_and_invites(self) -> Dict[str, Any]:
        """Возвращает сводный отчет об инвайтах и доверенных устройствах (с маскированием токенов)."""
        with self._lock:
            now = time.time()
            invites_list = []
            for ticket, inv in self._invites.items():
                expires_at = inv.get("expires_at", 0)
                is_expired = now > expires_at
                max_uses = inv.get("max_uses", 1)
                uses_count = inv.get("uses_count", 0)
                is_exhausted = uses_count >= max_uses

                invites_list.append({
                    "ticket_masked": f"{ticket[:6]}...{ticket[-4:]}",
                    "created_at": inv.get("created_at"),
                    "expires_at": expires_at,
                    "is_expired": is_expired,
                    "uses_count": uses_count,
                    "max_uses": max_uses,
                    "is_exhausted": is_exhausted,
                    "note": inv.get("note", ""),
                    "created_by": inv.get("created_by", "admin"),
                })

            devices_list = []
            for token, dev in self._trusted_devices.items():
                devices_list.append({
                    "device_id": dev.get("device_id"),
                    "player_name": dev.get("player_name"),
                    "activated_at": dev.get("activated_at"),
                    "last_seen": dev.get("last_seen"),
                    "last_ip": dev.get("last_ip"),
                    "revoked": dev.get("revoked", False),
                    "token_masked": f"{token[:6]}...{token[-4:]}",
                    "note": dev.get("note", ""),
                })

            return {
                "require_device_auth": self.require_auth,
                "total_invites": len(self._invites),
                "total_devices": len(self._trusted_devices),
                "invites": invites_list,
                "devices": devices_list,
            }

    def cleanup_expired_invites(self, older_than_seconds: float = 7 * 86400) -> int:
        """Очищает устаревшие и исчерпанные инвайты старше указанного срока."""
        with self._lock:
            now = time.time()
            to_remove = []
            for ticket, inv in self._invites.items():
                expires_at = inv.get("expires_at", 0)
                if now - expires_at > older_than_seconds:
                    to_remove.append(ticket)

            for ticket in to_remove:
                del self._invites[ticket]

            if to_remove:
                self._persist_state_locked()
                logger.info(f"Очищено {len(to_remove)} устаревших инвайтов.")

            return len(to_remove)
