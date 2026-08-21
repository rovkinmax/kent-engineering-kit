from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CiContractTest(unittest.TestCase):
    def test_workflow_is_credential_free_and_source_only(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertEqual(
            workflow,
            """name: validate

on:
  pull_request:
  push:
    branches:
      - main

permissions:
  contents: read

jobs:
  validate:
    runs-on: ubuntu-latest
    env:
      RUNNER_ENVIRONMENT: github-hosted
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          persist-credentials: false
      - name: Assert hosted runner
        run: test "$RUNNER_ENVIRONMENT" = github-hosted
      - name: Validate source tree
        run: ./scripts/validate
""",
        )

    def test_source_only_validation_does_not_create_installed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            home = temporary_root / "home"
            persistence = Path(temporary) / "persistence"
            home.mkdir()
            copied_root = temporary_root / "kit"
            shutil.copytree(
                ROOT,
                copied_root,
                ignore=shutil.ignore_patterns(".git", "__pycache__"),
            )
            (copied_root / "tests" / "test_ci_contract.py").write_text(
                "import unittest\n"
                "class SourceValidationProbe(unittest.TestCase):\n"
                "    def test_probe(self):\n"
                "        pass\n"
            )
            result = subprocess.run(
                [str(copied_root / "scripts" / "validate")],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "KENT_PERSISTENCE_ROOT": str(persistence),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((persistence / "config.toml").exists())
            self.assertFalse((home / ".kent" / "config.toml").exists())

    def test_validate_has_an_explicit_installed_state_mode(self) -> None:
        script = (ROOT / "scripts" / "validate").read_text()
        self.assertIn("--installed-state", script)
        self.assertIn("usage: scripts/validate [--installed-state]", script)
        self.assertIn('if [[ "$installed_state" -eq 1 ]]; then', script)

    def test_validate_rejects_unknown_arguments(self) -> None:
        result = subprocess.run(
            [str(ROOT / "scripts" / "validate"), "--unexpected"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
