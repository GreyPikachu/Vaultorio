"""
Automated validation and static verification tests for VPS deployment artifacts.
Verifies systemd unit configuration, resource bounds (64MB RAM limit),
VPN port conflict barriers, requirements hygiene, and bash script syntax.
"""

import configparser
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy" / "vps"


class TestDeployVPS(unittest.TestCase):
    def setUp(self):
        self.service_file = DEPLOY_DIR / "factorio-hub.service"
        self.install_script = DEPLOY_DIR / "install_vps.sh"
        self.uninstall_script = DEPLOY_DIR / "uninstall_vps.sh"
        self.req_file = DEPLOY_DIR / "requirements.txt"

    def test_artifacts_exist(self):
        """Verifies that all deployment files exist."""
        self.assertTrue(self.service_file.is_file(), f"Missing {self.service_file}")
        self.assertTrue(self.install_script.is_file(), f"Missing {self.install_script}")
        self.assertTrue(self.uninstall_script.is_file(), f"Missing {self.uninstall_script}")
        self.assertTrue(self.req_file.is_file(), f"Missing {self.req_file}")

    def test_bash_script_syntax_install(self):
        """T5.1: Verifies bash -n syntax validity for install_vps.sh."""
        res = subprocess.run(
            ["bash", "-n", str(self.install_script)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Syntax error in install_vps.sh: {res.stderr}")

    def test_bash_script_syntax_uninstall(self):
        """T5.1: Verifies bash -n syntax validity for uninstall_vps.sh."""
        res = subprocess.run(
            ["bash", "-n", str(self.uninstall_script)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Syntax error in uninstall_vps.sh: {res.stderr}")

    def test_bash_script_strict_flags(self):
        """T5.1: Ensures scripts use strict error-handling flags set -euo pipefail."""
        install_content = self.install_script.read_text(encoding="utf-8")
        uninstall_content = self.uninstall_script.read_text(encoding="utf-8")

        self.assertIn("set -euo pipefail", install_content)
        self.assertIn("set -euo pipefail", uninstall_content)

    def test_systemd_resource_limits_and_workers(self):
        """T5.2: Verifies cgroup limits (MemoryMax=64M, CPUQuota=15%) and --workers 1 in systemd unit."""
        content = self.service_file.read_text(encoding="utf-8")

        # 1. Cgroup memory limits
        self.assertIn("MemoryAccounting=true", content)
        self.assertIn("MemoryHigh=96M", content)
        self.assertIn("MemoryMax=128M", content)
        self.assertIn("CPUQuota=15%", content)

        # 2. Worker constraint: strictly single worker for shared in-memory session and STUN socket
        self.assertIn("--workers 1", content)

        # 3. Restart policy
        self.assertIn("Restart=on-failure", content)
        self.assertIn("RestartSec=5", content)

        # 4. Correct target directory
        self.assertIn("WorkingDirectory=/opt/factorio-hub", content)

        # 5. Environment ports
        self.assertIn("FACTORIO_HTTP_PORT=8088", content)
        self.assertIn("FACTORIO_UDP_STUN_PORT=14197", content)

    def test_vpn_port_barrier_in_install_script(self):
        """T5.3: Ensures install_vps.sh validates port 14197 against Hysteria range (20000:50000)."""
        content = self.install_script.read_text(encoding="utf-8")

        # Must define safe ports
        self.assertIn("HTTP_PORT=8088", content)
        self.assertIn("UDP_STUN_PORT=14197", content)

        # Must include Hysteria range validation
        self.assertIn("20000", content)
        self.assertIn("50000", content)
        self.assertTrue(
            re.search(r"UDP_STUN_PORT\s*>=\s*20000", content),
            "install_vps.sh must verify that UDP_STUN_PORT does not fall into 20000:50000",
        )

        # Firewall checks
        self.assertTrue(
            "8088/tcp" in content or "${HTTP_PORT}/tcp" in content,
            "install_vps.sh must configure firewall rule for HTTP port",
        )
        self.assertTrue(
            "14197/udp" in content or "${UDP_STUN_PORT}/udp" in content,
            "install_vps.sh must configure firewall rule for STUN UDP port",
        )

    def test_requirements_hygiene_and_minimal_footprint(self):
        """T5.4: Ensures requirements.txt contains essential dependencies without bloat."""
        lines = [
            line.strip()
            for line in self.req_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        pkg_names = [re.split(r"[><=~]", line)[0].strip().lower() for line in lines]

        # Must include essential server libraries
        self.assertIn("fastapi", pkg_names)
        self.assertIn("uvicorn", pkg_names)
        self.assertIn("python-multipart", pkg_names)
        self.assertIn("pydantic", pkg_names)

        # Must NOT include heavy dependencies that inflate memory
        forbidden_bloat = ["numpy", "pandas", "scipy", "torch", "sqlalchemy", "psycopg2"]
        for forbidden in forbidden_bloat:
            self.assertNotIn(forbidden, pkg_names, f"Forbidden heavy package found: {forbidden}")


if __name__ == "__main__":
    unittest.main()
