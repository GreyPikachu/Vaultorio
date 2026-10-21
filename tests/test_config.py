"""
Unit tests for server configuration and validation gates.
"""

import os
import unittest
from pathlib import Path

from src.server.config import ConfigValidationError, ServerConfig, load_server_config


class TestServerConfig(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("./data/test_config_storage")

    def test_default_config_valid(self):
        config = ServerConfig(storage_dir=self.test_dir)
        config.validate()
        self.assertEqual(config.current_save_filename, "cloud_world.zip")
        self.assertEqual(config.http_port, 8088)
        self.assertEqual(config.udp_stun_port, 14197)
        self.assertEqual(config.max_backups, 3)

    def test_port_conflict_hysteria_forbidden(self):
        """Crucial test: ensure port within 20000:50000 is rejected immediately."""
        conflict_ports = [20000, 25000, 34197, 49999, 50000]
        for port in conflict_ports:
            with self.subTest(port=port):
                config = ServerConfig(storage_dir=self.test_dir, udp_stun_port=port)
                with self.assertRaises(ConfigValidationError) as ctx:
                    config.validate()
                self.assertIn("Hysteria 2", str(ctx.exception))

    def test_safe_ports_allowed(self):
        safe_ports = [14197, 1024, 19999, 50001, 65535]
        for port in safe_ports:
            with self.subTest(port=port):
                config = ServerConfig(storage_dir=self.test_dir, udp_stun_port=port)
                config.validate()

    def test_invalid_http_port_range(self):
        for port in [-1, 0, 65536, 100000]:
            with self.subTest(port=port):
                config = ServerConfig(storage_dir=self.test_dir, http_port=port)
                with self.assertRaises(ConfigValidationError):
                    config.validate()

    def test_invalid_filename(self):
        invalid_names = ["", "cloud_world.tar.gz", "world.rar", "world"]
        for name in invalid_names:
            with self.subTest(name=name):
                config = ServerConfig(storage_dir=self.test_dir, current_save_filename=name)
                with self.assertRaises(ConfigValidationError):
                    config.validate()

    def test_invalid_max_backups(self):
        for b in [0, -5, 51, 100]:
            with self.subTest(max_backups=b):
                config = ServerConfig(storage_dir=self.test_dir, max_backups=b)
                with self.assertRaises(ConfigValidationError):
                    config.validate()

    def test_invalid_upload_size(self):
        for s in [500, 3 * 1024 * 1024 * 1024]:
            with self.subTest(size=s):
                config = ServerConfig(storage_dir=self.test_dir, max_upload_size_bytes=s)
                with self.assertRaises(ConfigValidationError):
                    config.validate()

    def test_invalid_lease_timeout(self):
        for t in [5, 4000]:
            with self.subTest(timeout=t):
                config = ServerConfig(storage_dir=self.test_dir, lease_timeout_seconds=t)
                with self.assertRaises(ConfigValidationError):
                    config.validate()

    def test_load_server_config_env(self):
        os.environ["FACTORIO_HTTP_PORT"] = "9090"
        os.environ["FACTORIO_UDP_STUN_PORT"] = "15000"
        os.environ["FACTORIO_MAX_BACKUPS"] = "5"
        try:
            cfg = load_server_config()
            self.assertEqual(cfg.http_port, 9090)
            self.assertEqual(cfg.udp_stun_port, 15000)
            self.assertEqual(cfg.max_backups, 5)
        finally:
            os.environ.pop("FACTORIO_HTTP_PORT", None)
            os.environ.pop("FACTORIO_UDP_STUN_PORT", None)
            os.environ.pop("FACTORIO_MAX_BACKUPS", None)


if __name__ == "__main__":
    unittest.main()
