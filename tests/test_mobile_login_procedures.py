from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = {
    "runtime-smoke-tester": "mobile-smoke-contract.md",
    "ci-monitor": "workflow-contract.md",
    "delivery-operator": "worktree-contract.md",
}


class MobileLoginInstallationTest(unittest.TestCase):
    def test_installed_skill_resolves_required_contracts_outside_consumer(self) -> None:
        # Source discovery only, not model obedience or runtime qualification.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer = root / "consumer"
            consumer.mkdir()
            for filename in CONTRACTS.values():
                lookalike = consumer / "contracts" / filename
                lookalike.parent.mkdir(exist_ok=True)
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
                    self.assertFalse((persistence / "contracts").exists())

                    installed_skill = persistence / "skills/kent-engineering-kit"
                    self.assertTrue(installed_skill.is_symlink())
                    qualified_kit = installed_skill.resolve(strict=True).parents[1]
                    self.assertEqual(qualified_kit, ROOT)
                    for role, filename in CONTRACTS.items():
                        with self.subTest(role=role):
                            relative_role = Path("agents") / f"{role}.md"
                            installed_role = persistence / relative_role
                            self.assertTrue(installed_role.is_symlink())
                            self.assertEqual(
                                installed_role.resolve(strict=True), ROOT / relative_role,
                            )
                            contract = qualified_kit / "contracts" / filename
                            lookalike = consumer / "contracts" / filename
                            self.assertTrue(contract.is_file())
                            self.assertTrue(contract.read_bytes())
                            self.assertNotEqual(contract.resolve(), lookalike.resolve())
                            self.assertNotEqual(contract.read_bytes(), lookalike.read_bytes())


if __name__ == "__main__":
    unittest.main()
