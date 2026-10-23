"""Системные уведомления и работа с буфером обмена.

Обеспечивает:
1. Кроссплатформенное копирование адреса прокси (127.0.0.1:34198) в буфер обмена (clip / pbcopy).
2. Неблокирующие всплывающие уведомления (баннеры) с подсказками ролей для Хоста и Гостя.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
from typing import Any, Optional

logger = logging.getLogger("factorio_client.notifier")


def copy_to_clipboard(text: str) -> bool:
    """Копирует текстовую строку в системный буфер обмена.

    Использует нативные утилиты операционной системы:
    - macOS: pbcopy
    - Windows: clip
    - Linux: xclip / xsel

    Args:
        text: Строка для копирования.

    Returns:
        True, если операция выполнена успешно, иначе False.
    """
    if not isinstance(text, str):
        logger.warning(f"Попытка скопировать нестроковый объект в буфер: {type(text)}")
        return False

    current_os = platform.system()
    try:
        if current_os == "Darwin":
            proc = subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                check=True,
                capture_output=True,
                timeout=2.0,
            )
            logger.info("Адрес успешно скопирован в буфер обмена (pbcopy).")
            return proc.returncode == 0

        elif current_os == "Windows":
            # В Windows утилита clip принимает UTF-16LE или активную кодовую страницу
            try:
                proc = subprocess.run(
                    ["clip"],
                    input=text.encode("utf-16le"),
                    check=True,
                    capture_output=True,
                    timeout=2.0,
                    shell=True,
                )
            except Exception:
                proc = subprocess.run(
                    ["clip"],
                    input=text.encode("utf-8"),
                    check=True,
                    capture_output=True,
                    timeout=2.0,
                    shell=True,
                )
            logger.info("Адрес успешно скопирован в буфер обмена (clip).")
            return proc.returncode == 0

        else:  # Linux
            # Пробуем xclip
            try:
                proc = subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text.encode("utf-8"),
                    check=True,
                    capture_output=True,
                    timeout=2.0,
                )
                logger.info("Адрес скопирован в буфер обмена (xclip).")
                return proc.returncode == 0
            except FileNotFoundError:
                # Fallback на xsel
                proc = subprocess.run(
                    ["xsel", "--clipboard", "--input"],
                    input=text.encode("utf-8"),
                    check=True,
                    capture_output=True,
                    timeout=2.0,
                )
                logger.info("Адрес скопирован в буфер обмена (xsel).")
                return proc.returncode == 0

    except Exception as e:
        logger.warning(f"Не удалось скопировать текст в системный буфер обмена: {e}")
        return False


def _dispatch_system_notification(title: str, message: str) -> None:
    """Выполняет вызов нативной утилиты уведомлений ОС."""
    current_os = platform.system()
    try:
        if current_os == "Darwin":
            # Экранируем двойные кавычки и обратные слеши для AppleScript
            escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
            escaped_msg = message.replace("\\", "\\\\").replace('"', '\\"')
            script = f'display notification "{escaped_msg}" with title "{escaped_title}"'
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                timeout=3.0,
            )
        elif current_os == "Windows":
            # Используем PowerShell NotifyIcon балун-нотификацию
            ps_script = (
                "[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null; "
                "[reflection.assembly]::loadwithpartialname('System.Drawing') | Out-Null; "
                "$notify = new-object system.windows.forms.notifyicon; "
                "$notify.icon = [system.drawing.systemicons]::information; "
                "$notify.visible = $true; "
                f"$notify.showballoontip(3500, '{title}', '{message}', [system.windows.forms.tooltipicon]::info); "
                "Start-Sleep -Seconds 4; "
                "$notify.dispose();"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                check=False,
                capture_output=True,
                timeout=5.0,
            )
        else:  # Linux
            subprocess.run(
                ["notify-send", title, message],
                check=False,
                capture_output=True,
                timeout=3.0,
            )
    except Exception as e:
        logger.debug(f"Фоновая отправка системного всплывающего уведомления завершилась с ошибкой: {e}")


def show_role_banner(
    role: str,
    host_name: Optional[str] = None,
    extra_message: Optional[str] = None,
    background: bool = True,
    duration_sec: Optional[float] = None,
    **kwargs: Any,
) -> str:
    """Отображает подсказку о текущей роли игрока в игре.

    Для Хоста:
        🟢 Вы — Хост! Зайдите в: Сетевая игра -> Запустить сохранённую игру -> cloud_world
    Для Гостя:
        🔵 Сейчас хостит {host_name}! Сейв скрыт. Зайдите в: Сетевая игра -> Прямое подключение (адрес уже вставлен)

    Args:
        role: Роль игрока ('host', 'guest' или произвольная).
        host_name: Имя текущего Хоста (для роли Гостя).
        extra_message: Дополнительное пояснение (например, статус синхронизации).
        background: Выполнять ли показ в неблокирующем фоновом потоке (по умолчанию True).
        duration_sec: Длительность показа уведомления в секундах (для совместимости).
        **kwargs: Дополнительные параметры.

    Returns:
        Сформированный текст уведомления.
    """
    role_normalized = role.strip().lower()

    if role_normalized == "host":
        title = "Factorio P2P — Вы Хост!"
        body = "🟢 Вы — Хост! Зайдите в: Сетевая игра -> Запустить сохранённую игру -> cloud_world"
    elif role_normalized == "guest":
        h_name = host_name.strip() if host_name and host_name.strip() else "Друг"
        title = f"Factorio P2P — Хостит {h_name}"
        body = (
            f"🔵 Сейчас хостит {h_name}! Сейв скрыт. "
            f"Зайдите в: Сетевая игра -> Прямое подключение (адрес уже вставлен)"
        )
    else:
        title = "Factorio P2P Cloud Sync"
        body = f"Статус роли: {role}"

    if extra_message:
        body = f"{body}\n{extra_message.strip()}"

    logger.info(f"УВЕДОМЛЕНИЕ [{title}]: {body}")

    if background:
        thread = threading.Thread(
            target=_dispatch_system_notification,
            args=(title, body),
            name="NotificationThread",
            daemon=True,
        )
        thread.start()
    else:
        _dispatch_system_notification(title, body)

    return body
