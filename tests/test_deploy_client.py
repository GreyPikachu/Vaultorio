"""
Automated validation and static verification tests for Client deployment artifacts.
Verifies file existence, bash syntax validity, dependency files, and cross-platform scripts.
"""

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_DEPLOY_DIR = REPO_ROOT / "deploy" / "client"
MACOS_DIR = CLIENT_DEPLOY_DIR / "macos"
WINDOWS_DIR = CLIENT_DEPLOY_DIR / "windows"


class TestDeployClient(unittest.TestCase):
    def test_artifacts_exist(self):
        """Verifies that all client deployment files exist."""
        self.assertTrue((CLIENT_DEPLOY_DIR / "requirements.txt").is_file())
        self.assertTrue((MACOS_DIR / "install_mac.command").is_file())
        self.assertTrue((MACOS_DIR / "uninstall_mac.command").is_file())
        self.assertTrue((WINDOWS_DIR / "install_windows.bat").is_file())
        self.assertTrue((WINDOWS_DIR / "uninstall_windows.bat").is_file())

    def test_requirements_content(self):
        """Verifies client requirements contains httpx."""
        req_content = (CLIENT_DEPLOY_DIR / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("httpx", req_content)

    def test_bash_script_syntax_macos_install(self):
        """Verifies bash -n syntax validity for install_mac.command."""
        script = MACOS_DIR / "install_mac.command"
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Syntax error in install_mac.command: {res.stderr}")

    def test_bash_script_syntax_macos_uninstall(self):
        """Verifies bash -n syntax validity for uninstall_mac.command."""
        script = MACOS_DIR / "uninstall_mac.command"
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Syntax error in uninstall_mac.command: {res.stderr}")

    def test_no_hardcoded_user_or_ip(self):
        """Ensures scripts do not have hardcoded test usernames or IPs."""
        forbidden_ip = ".".join(["45", "91", "248", "78"])
        for p in [
            MACOS_DIR / "install_mac.command",
            MACOS_DIR / "uninstall_mac.command",
            WINDOWS_DIR / "install_windows.bat",
            WINDOWS_DIR / "uninstall_windows.bat",
        ]:
            content = p.read_text(encoding="utf-8")
            self.assertNotIn("CliPlayer", content, f"Hardcoded CliPlayer found in {p}")
            self.assertNotIn(forbidden_ip, content, f"Hardcoded IP found in {p}")


if __name__ == "__main__":
    unittest.main()
