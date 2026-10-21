"""
Server executable entry point for Factorio P2P Cloud Sync & Coordinator («Сейф»).
Loads configuration, binds HTTP REST service to port 8088/tcp, and manages graceful shutdown.
"""

from __future__ import annotations

import sys
import uvicorn

from src.server.api import create_app
from src.server.config import load_server_config
from src.server.logger import get_logger, setup_server_logging

logger = get_logger("main")


def main() -> None:
    """Initializes logging, configuration, and launches the uvicorn web server."""
    try:
        config = load_server_config()
    except Exception as exc:
        print(f"FATAL: Invalid configuration: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_server_logging(
        log_dir=config.logs_dir,
        log_filename="factorio_server.log",
        log_level="INFO",
    )

    logger.info("=" * 60)
    logger.info("Starting Factorio Safe HTTP API on %s:%d", config.http_host, config.http_port)
    logger.info("Storage directory: %s", config.storage_dir)
    logger.info("Current save target: %s", config.current_save_path)
    logger.info("=" * 60)

    app = create_app(config=config)

    uvicorn.run(
        app,
        host=config.http_host,
        port=config.http_port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
