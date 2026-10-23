"""Модуль кроссплатформенного определения зажатой клавиши Shift при запуске.

Позволяет вызвать окно настроек Factorio Co-op и выбора активного мира
при старте игры с зажатой клавишей Shift, либо через переменную окружения
FACTORIO_FORCE_SETUP_GUI=1.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("factorio_client.shift_detector")


def is_shift_key_pressed() -> bool:
    """Проверяет, зажата ли клавиша Shift в момент вызова.

    Поддерживает:
    1. Переменную окружения FACTORIO_FORCE_SETUP_GUI ("1", "true", "yes").
    2. Windows: GetAsyncKeyState через ctypes.windll.user32 (VK_SHIFT, VK_LSHIFT, VK_RSHIFT).
    3. macOS: CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState) через ApplicationServices
       (проверка битовой маски kCGEventFlagMaskShift = 0x00020000 без требования Accessibility).
    4. Безопасный fallback (False) для Linux или при недоступности графической подсистемы.

    Returns:
        True, если клавиша Shift зажата или активирована переменная окружения, иначе False.
    """
    # 1. Проверка переменной окружения (удобно для скриптов, ярлыков и тестов)
    env_force = os.getenv("FACTORIO_FORCE_SETUP_GUI", "").strip().lower()
    if env_force in ("1", "true", "yes"):
        logger.info("Принудительное открытие GUI через FACTORIO_FORCE_SETUP_GUI")
        return True

    # 2. Windows
    if sys.platform == "win32":
        try:
            import ctypes

            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                user32 = getattr(windll, "user32", None)
                if user32 is not None:
                    # 0x10 = VK_SHIFT, 0xA0 = VK_LSHIFT, 0xA1 = VK_RSHIFT
                    # Старший бит (& 0x8000) показывает, что клавиша удерживается прямо сейчас
                    for vk in (0x10, 0xA0, 0xA1):
                        res = user32.GetAsyncKeyState(vk)
                        try:
                            val = int(res)
                        except (TypeError, ValueError):
                            continue
                        if bool(val & 0x8000):
                            logger.info(f"Обнаружена зажатая клавиша Shift на Windows (VK: {hex(vk)})")
                            return True
        except Exception as err:
            logger.debug(f"Ошибка проверки клавиши Shift на Windows: {err}")
        return False

    # 3. macOS (Darwin)
    if sys.platform == "darwin":
        try:
            import ctypes

            # Загружаем CoreGraphics / ApplicationServices
            app_services = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            # CGEventFlags CGEventSourceFlagsState(CGEventSourceStateID stateID);
            # kCGEventSourceStateHIDSystemState = 1
            # kCGEventFlagMaskShift = 0x00020000
            app_services.CGEventSourceFlagsState.restype = ctypes.c_uint64
            app_services.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]

            flags = app_services.CGEventSourceFlagsState(1)
            kCGEventFlagMaskShift = 0x00020000
            if bool(flags & kCGEventFlagMaskShift):
                logger.info("Обнаружена зажатая клавиша Shift на macOS (CGEventSourceFlagsState)")
                return True
        except Exception as err:
            logger.debug(f"Ошибка проверки клавиши Shift на macOS: {err}")
        return False

    # 4. Linux / Fallback
    logger.debug("Определение Shift не поддерживается на данной платформе или нет доступа к дисплею.")
    return False
