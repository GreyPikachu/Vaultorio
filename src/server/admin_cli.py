"""Консольная утилита администратора для управления инвайтами и устройствами (Admin CLI).

Предоставляет команды:
1. create-invite: Создает одноразовый билет и шифрует его в компактный код FCT1-...
2. list: Выводит сводный список билетов приглашений и зарегистрированных устройств.
3. revoke: Отзывает доступ у игрока или устройства по имени, UUID или токену.
4. cleanup: Очищает устаревшие и исчерпанные билеты приглашений.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from src.common.invite_codec import encode_invite_code
from src.server.auth_manager import AuthManager
from src.server.config import ServerConfig, load_server_config
from src.server.telegram_bot import TelegramBotSettings

logger = logging.getLogger("admin_cli")


def _get_server_config(
    config_path: Optional[str] = None,
    storage_dir: Optional[str] = None,
) -> ServerConfig:
    """Загружает конфигурацию сервера с учетом переопределений из CLI."""
    cfg = load_server_config()
    if storage_dir:
        cfg = ServerConfig(
            storage_dir=Path(storage_dir).resolve(),
            current_save_filename=cfg.current_save_filename,
            max_backups=cfg.max_backups,
            max_upload_size_bytes=cfg.max_upload_size_bytes,
            http_host=cfg.http_host,
            http_port=cfg.http_port,
            udp_stun_port=cfg.udp_stun_port,
            lease_timeout_seconds=cfg.lease_timeout_seconds,
            require_device_auth=cfg.require_device_auth,
        )
    return cfg


def _detect_public_ip() -> str:
    """Пытается определить внешний публичный IPv4 адрес сервера."""
    import urllib.request
    services = [
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://ifconfig.me/ip",
    ]
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                ip = resp.read().decode("utf-8").strip()
                parts = ip.split(".")
                if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    return ip
        except Exception:
            continue
    return "127.0.0.1"


def handle_create_invite(args: argparse.Namespace) -> int:
    """Обрабатывает команду create-invite."""
    try:
        config = _get_server_config(args.config, args.storage_dir)
        auth_mgr = AuthManager(config)

        # 1. Валидация входных данных
        passphrase = (args.passphrase or "").strip()
        if not passphrase:
            sys.stderr.write("Ошибка: Кодовая фраза (--passphrase) не может быть пустой.\n")
            return 1

        ttl_minutes = int(args.ttl_minutes)
        if ttl_minutes <= 0:
            sys.stderr.write("Ошибка: Время жизни (--ttl-minutes) должно быть положительным числом.\n")
            return 1

        max_uses = int(args.max_uses)
        if max_uses <= 0:
            sys.stderr.write("Ошибка: Количество использований (--max-uses) должно быть больше 0.\n")
            return 1

        hub_url = (args.hub_url or "").strip()
        if not hub_url:
            # Автоматическое определение по умолчанию из конфига или внешнего IP
            if config.http_host != "0.0.0.0":
                host = config.http_host
            else:
                host = _detect_public_ip()
            hub_url = f"http://{host}:{config.http_port}"

        stun_host = (args.stun_host or "").strip()
        if not stun_host:
            # Извлекаем хост из hub_url
            from urllib.parse import urlparse
            parsed = urlparse(hub_url)
            stun_host = parsed.hostname or (config.http_host if config.http_host != "0.0.0.0" else "127.0.0.1")

        stun_port = int(args.stun_port) if args.stun_port else config.udp_stun_port

        # 2. Создание билета на сервере
        ttl_seconds = ttl_minutes * 60
        invite_data = auth_mgr.create_invite(
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
            note=args.note or "",
            created_by=args.created_by or "admin_cli",
        )

        ticket = invite_data["ticket"]
        expires_at = int(invite_data["expires_at"])

        # 3. Криптографическая упаковка и шифрование
        invite_code = encode_invite_code(
            hub_http_url=hub_url,
            hub_stun_host=stun_host,
            hub_stun_port=stun_port,
            ticket=ticket,
            expires_at=expires_at,
            passphrase=passphrase,
        )

        exp_dt = datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc).astimezone()
        exp_str = exp_dt.strftime("%Y-%m-%d %H:%M:%S")

        sys.stdout.write("=================================================================\n")
        sys.stdout.write("              КОД ПРИГЛАШЕНИЯ (INVITE CODE)                     \n")
        sys.stdout.write("=================================================================\n")
        sys.stdout.write(f"Код:        {invite_code}\n")
        sys.stdout.write(f"Пароль:     {passphrase}\n")
        sys.stdout.write(f"Действует:  до {exp_str} ({ttl_minutes} мин.)\n")
        sys.stdout.write(f"Лимит мест: {max_uses}\n")
        if args.note:
            sys.stdout.write(f"Заметка:    {args.note}\n")
        sys.stdout.write("-----------------------------------------------------------------\n")
        sys.stdout.write("Инструкция для друга:\n")
        sys.stdout.write("1. Запустите установщик клиента (installer.py).\n")
        sys.stdout.write("2. Вставьте Код приглашения (FCT1-...) и введите пароль.\n")
        sys.stdout.write("3. После активации билет автоматически сгорит и превратится в токен.\n")
        sys.stdout.write("=================================================================\n")

        if args.raw_code_only:
            # Для скриптов автоматизации
            sys.stdout.write(f"\nRAW_CODE:{invite_code}\n")

        return 0

    except Exception as exc:
        sys.stderr.write(f"Ошибка при создании приглашения: {exc}\n")
        return 1


def handle_list(args: argparse.Namespace) -> int:
    """Обрабатывает команду list."""
    try:
        config = _get_server_config(args.config, args.storage_dir)
        auth_mgr = AuthManager(config)

        report = auth_mgr.list_devices_and_invites()

        if getattr(args, "json", False):
            sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            return 0

        sys.stdout.write("=================================================================\n")
        sys.stdout.write(f" СОСТОЯНИЕ АВТОРИЗАЦИИ (Require Auth: {report['require_device_auth']}) \n")
        sys.stdout.write("=================================================================\n\n")

        # Список инвайтов
        invites = report.get("invites", [])
        sys.stdout.write(f"--- БИЛЕТЫ ПРИГЛАШЕНИЙ ({len(invites)}) ---\n")
        if not invites:
            sys.stdout.write("  Нет активных или сохраненных билетов.\n")
        else:
            for inv in invites:
                status = "ИСЧЕРПАН" if inv["is_exhausted"] else ("ПРОСРОЧЕН" if inv["is_expired"] else "АКТИВЕН")
                exp_dt = datetime.datetime.fromtimestamp(inv["expires_at"], tz=datetime.timezone.utc).astimezone()
                exp_str = exp_dt.strftime("%Y-%m-%d %H:%M")
                note_str = f" [{inv['note']}]" if inv["note"] else ""
                sys.stdout.write(
                    f"  [{status}] {inv['ticket_masked']} | Исп: {inv['uses_count']}/{inv['max_uses']} "
                    f"| До: {exp_str}{note_str}\n"
                )

        sys.stdout.write("\n")

        # Список доверенных устройств
        devices = report.get("devices", [])
        sys.stdout.write(f"--- ДОВЕРЕННЫЕ УСТРОЙСТВА ({len(devices)}) ---\n")
        if not devices:
            sys.stdout.write("  Нет зарегистрированных устройств.\n")
        else:
            for dev in devices:
                status = "ОТОЗВАН" if dev["revoked"] else "АКТИВЕН"
                act_dt = datetime.datetime.fromtimestamp(dev["activated_at"], tz=datetime.timezone.utc).astimezone()
                act_str = act_dt.strftime("%Y-%m-%d %H:%M")
                sys.stdout.write(
                    f"  [{status}] Игрок: '{dev['player_name']}' | ID: {dev['device_id'][:8]}... "
                    f"| IP: {dev['last_ip']} | Акт: {act_str}\n"
                )

        sys.stdout.write("\n=================================================================\n")
        return 0

    except Exception as exc:
        sys.stderr.write(f"Ошибка при получении списка: {exc}\n")
        return 1


def handle_revoke(args: argparse.Namespace) -> int:
    """Обрабатывает команду revoke."""
    try:
        config = _get_server_config(args.config, args.storage_dir)
        auth_mgr = AuthManager(config)

        target = (args.target or "").strip()
        if not target:
            sys.stderr.write("Ошибка: Укажите никнейм игрока, UUID устройства или токен для отзыва.\n")
            return 1

        success = auth_mgr.revoke_device(target)
        if success:
            sys.stdout.write(f"Успех: Доступ для '{target}' успешно отозван.\n")
            return 0
        else:
            sys.stderr.write(f"Ошибка: Устройство или игрок с идентификатором '{target}' не найдены.\n")
            return 1

    except Exception as exc:
        sys.stderr.write(f"Ошибка при отзыве доступа: {exc}\n")
        return 1


def handle_cleanup(args: argparse.Namespace) -> int:
    """Обрабатывает команду cleanup."""
    try:
        config = _get_server_config(args.config, args.storage_dir)
        auth_mgr = AuthManager(config)

        days = int(args.days)
        if days < 0:
            sys.stderr.write("Ошибка: Количество дней (--days) не может быть отрицательным.\n")
            return 1

        older_than_seconds = days * 86400
        removed = auth_mgr.cleanup_expired_invites(older_than_seconds=older_than_seconds)
        sys.stdout.write(f"Успех: Удалено {removed} устаревших инвайтов (старше {days} дней).\n")
        return 0

    except Exception as exc:
        sys.stderr.write(f"Ошибка при очистке инвайтов: {exc}\n")
        return 1


def handle_setup_telegram(args: argparse.Namespace) -> int:
    """Обрабатывает команду setup-telegram для привязки Telegram-бота."""
    try:
        config = _get_server_config(args.config, args.storage_dir)
        settings = TelegramBotSettings.load(config.telegram_state_path)

        bot_token = (args.bot_token or "").strip()
        if not bot_token:
            sys.stderr.write("Ошибка: Токен Telegram-бота (--bot-token) не может быть пустым.\n")
            return 1
        settings.bot_token = bot_token

        if args.chat_id is not None and str(args.chat_id).strip():
            raw_chat = str(args.chat_id).strip()
            try:
                settings.chat_id = int(raw_chat)
            except ValueError:
                settings.chat_id = raw_chat

        if args.openrouter_key is not None:
            settings.openrouter_api_key = args.openrouter_key.strip()

        if args.openrouter_model is not None and args.openrouter_model.strip():
            settings.openrouter_model = args.openrouter_model.strip()

        if args.dashboard_title is not None and args.dashboard_title.strip():
            settings.dashboard_title = args.dashboard_title.strip()

        settings.enabled = True
        settings.save(config.telegram_state_path)

        masked_token = f"{bot_token[:6]}...{bot_token[-4:]}" if len(bot_token) > 10 else "***"
        sys.stdout.write("Успех: Настройки Telegram-бота сохранены.\n")
        sys.stdout.write(f"Файл состояния: {config.telegram_state_path}\n")
        sys.stdout.write(f"Токен бота: {masked_token}\n")
        if settings.chat_id:
            sys.stdout.write(f"ID чата: {settings.chat_id}\n")
        else:
            sys.stdout.write("ID чата: Не привязан. Добавьте бота в группу друзей и отправьте команду /setup в чате.\n")

        if settings.openrouter_api_key:
            masked_key = f"{settings.openrouter_api_key[:6]}...{settings.openrouter_api_key[-4:]}"
            sys.stdout.write(f"Ключ OpenRouter: {masked_key} (модель: {settings.openrouter_model})\n")

        return 0

    except Exception as exc:
        sys.stderr.write(f"Ошибка при настройке Telegram-бота: {exc}\n")
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Формирует и настраивает парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="admin_cli",
        description="Factorio Safe Hub: Утилита администратора для управления доступом и инвайтами.",
    )
    parser.add_argument("--config", "-c", type=str, default=None, help="Путь к файлу конфигурации сервера.")
    parser.add_argument("--storage-dir", type=str, default=None, help="Путь к директории хранилища сервера.")

    subparsers = parser.add_subparsers(dest="command", required=True, help="Команды администратора")

    # 1. create-invite
    create_parser = subparsers.add_parser(
        "create-invite",
        help="Создать одноразовый зашифрованный код приглашения FCT1-...",
    )
    create_parser.add_argument("--passphrase", "-p", required=True, type=str, help="Парольная фраза для шифрования кода.")
    create_parser.add_argument("--hub-url", type=str, default="", help="HTTP URL сервера (по умолчанию из конфига).")
    create_parser.add_argument("--stun-host", type=str, default="", help="STUN хост/IP (по умолчанию из конфига).")
    create_parser.add_argument("--stun-port", type=int, default=0, help="STUN порт (по умолчанию из конфига).")
    create_parser.add_argument("--ttl-minutes", "-t", type=int, default=60, help="Время жизни билета в минутах (по умолчанию 60).")
    create_parser.add_argument("--max-uses", "-m", type=int, default=1, help="Максимальное число активаций (по умолчанию 1).")
    create_parser.add_argument("--note", "-n", type=str, default="", help="Заметка (например, имя друга: 'Для Димы').")
    create_parser.add_argument("--created-by", type=str, default="admin", help="Имя создателя инвайта.")
    create_parser.add_argument("--raw-code-only", action="store_true", help="Вывести только чистый код приглашения.")

    # 2. list
    list_parser = subparsers.add_parser("list", help="Показать список инвайтов и доверенных устройств.")
    list_parser.add_argument("--json", action="store_true", help="Вывод в формате JSON.")

    # 3. revoke
    revoke_parser = subparsers.add_parser("revoke", help="Отозвать доступ у игрока или устройства.")
    revoke_parser.add_argument(
        "target",
        type=str,
        help="Никнейм игрока (player_name), UUID устройства (device_id) или токен.",
    )

    # 4. cleanup
    cleanup_parser = subparsers.add_parser("cleanup", help="Очистить устаревшие и исчерпанные билеты.")
    cleanup_parser.add_argument("--days", type=int, default=7, help="Удалять инвайты старше N дней (по умолчанию 7).")

    # 5. setup-telegram
    tg_parser = subparsers.add_parser(
        "setup-telegram",
        help="Настроить Telegram-бота, закрепленный дашборд и ключ OpenRouter.",
    )
    tg_parser.add_argument("--bot-token", required=True, type=str, help="Токен бота от @BotFather.")
    tg_parser.add_argument("--chat-id", type=str, default=None, help="ID группы/чата для уведомлений и закрепа.")
    tg_parser.add_argument("--openrouter-key", type=str, default=None, help="API-ключ OpenRouter для Vision-хроники.")
    tg_parser.add_argument("--openrouter-model", type=str, default=None, help="Модель OpenRouter (по умолчанию google/gemini-2.0-flash-exp:free).")
    tg_parser.add_argument("--dashboard-title", type=str, default=None, help="Заголовок закрепленного дашборда.")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа в консольную утилиту администратора."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "create-invite":
        return handle_create_invite(args)
    elif args.command == "list":
        return handle_list(args)
    elif args.command == "revoke":
        return handle_revoke(args)
    elif args.command == "cleanup":
        return handle_cleanup(args)
    elif args.command == "setup-telegram":
        return handle_setup_telegram(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
