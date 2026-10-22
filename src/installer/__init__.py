"""Модуль установщика и деинсталлятора Factorio P2P Cloud Sync & Coordinator.

Экспортирует:
- FactorioWrapperInstaller: класс управления установкой, обновлением и откатом.
- main: CLI точка входа установщика.
"""

from src.installer.installer import FactorioWrapperInstaller, main

__all__ = [
    "FactorioWrapperInstaller",
    "main",
]
