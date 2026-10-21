from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = Path("contracts/mobile-smoke-contract.md")
ROLE_PATH = Path("agents/runtime-smoke-tester.md")


class MobileLoginInstallationTest(unittest.TestCase):
    def test_installer_provides_mobile_guidance_from_qualified_kit_source(self) -> None:
        # Availability integration only: this does not execute login recovery.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer = root / "consumer"
            consumer.mkdir()
            lookalike = consumer / CONTRACT_PATH
            lookalike.parent.mkdir()
            lookalike.write_text("Unqualified consumer-local contract.\n")
            home = root / "home"
            for name, explicit_root in (("default", False), ("custom root", True)):
                with self.subTest(persistence=name):
                    persistence = root / name if explicit_root else home / ".kent"
                    environment = {
                        key: value for key, value in os.environ.items()
                        if key not in {"KENT_PERSISTENCE_ROOT", "HOME"}
                    }
                    environment["HOME"] = str(home)
                    if explicit_root:
                        environment["KENT_PERSISTENCE_ROOT"] = str(persistence)
                    result = subprocess.run(
                        ["bash", str(ROOT / "scripts/install")],
                        cwd=consumer,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    installed_role = persistence / ROLE_PATH
                    self.assertTrue(installed_role.is_symlink())
                    self.assertEqual(installed_role.resolve(strict=True), ROOT / ROLE_PATH)
                    self.assertFalse((persistence / "contracts").exists())

                    # Follow the installer's actual source binding, not cwd or
                    # an assumed contract copy in the persistence directory.
                    qualified_kit = installed_role.resolve(strict=True).parents[1]
                    contract = qualified_kit / CONTRACT_PATH
                    self.assertTrue(contract.is_file())
                    self.assertTrue(contract.read_bytes())
                    self.assertEqual(contract, ROOT / CONTRACT_PATH)
                    self.assertNotEqual(contract.resolve(), lookalike.resolve())
                    self.assertNotEqual(contract.read_bytes(), lookalike.read_bytes())


if __name__ == "__main__":
    unittest.main()
