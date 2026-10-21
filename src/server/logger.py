"""
Logging configuration for Factorio P2P Cloud Sync & Coordinator server.
Provides dual logging (stdout + rotating file) with ISO-8601 UTC timestamps.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from src.server.config import ServerConfig


class UTCFormatter(logging.Formatter):
    """Custom logging formatter that renders timestamps in ISO-8601 UTC format."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


_LOGGING_INITIALIZED = False


def setup_server_logging(
    config: Optional[ServerConfig] = None,
    log_level: int = logging.INFO,
) -> logging.Logger:
    """
    Initializes root server logger with Console and RotatingFile handlers.

    Args:
        config: Optional ServerConfig. If omitted or logs directory is inaccessible,
                falls back safely to console logging.
        log_level: Minimum logging level (default INFO).

    Returns:
        logging.Logger: Configured logger for 'factorio_server'.
    """
    global _LOGGING_INITIALIZED

    logger = logging.getLogger("factorio_server")
    logger.setLevel(log_level)

    # Avoid adding duplicate handlers on multiple calls
    if _LOGGING_INITIALIZED:
        return logger

    formatter = UTCFormatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] [%(module)s.%(funcName)s:%(lineno)d] %(message)s"
    )

    # 1. Console Handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 2. Rotating File Handler
    if config is not None:
        try:
            logs_dir = config.logs_dir
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file = logs_dir / "server.log"

            file_handler = RotatingFileHandler(
                filename=str(log_file),
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            logger.info("Rotating file logging configured at %s", log_file)
        except Exception as exc:
            logger.warning(
                "Could not initialize rotating file logger: %s. Continuing with console logging only.",
                exc,
            )

    _LOGGING_INITIALIZED = True
    return logger


def get_logger(module_name: str) -> logging.Logger:
    """
    Retrieves a child logger namespaced under 'factorio_server.<module_name>'.

    Args:
        module_name: Submodule name.

    Returns:
        logging.Logger: Child logger.
    """
    return logging.getLogger(f"factorio_server.{module_name}")
