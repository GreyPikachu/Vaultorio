"""Пакет средств тестирования и эмуляции для Factorio P2P Cloud Sync.

Экспортирует:
- MockFactorioCore: имитатор игрового процесса Factorio.
- create_dummy_save: утилита создания тестовых архивов сохранений.
"""

from src.testing_support.mock_factorio_core import MockFactorioCore, create_dummy_save

__all__ = [
    "MockFactorioCore",
    "create_dummy_save",
]
