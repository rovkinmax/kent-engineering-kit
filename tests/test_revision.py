from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workflowkit.revision import (
    RevisionPreflightError,
    preflight_project_revision,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROFILE = REPO_ROOT / "contracts" / "project-profile.example.toml"


class RevisionPreflightTest(unittest.TestCase):
    def create_project(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.run_git(root, "init", "-q")
        self.run_git(root, "config", "user.name", "Kent Test")
        self.run_git(root, "config", "user.email", "kent@example.invalid")
        self.run_git(root, "config", "core.fileMode", "true")

        kent = root / ".kent"
        scripts = kent / "scripts"
        scripts.mkdir(parents=True)
        (kent / "workflow-profile.toml").write_text(EXAMPLE_PROFILE.read_text())
        (kent / "project-contract.md").write_text("# Project contract\n")
        for name in ("workflow-verification-dispatch", "workflow-verify"):
            path = scripts / name
            path.write_text("#!/usr/bin/env bash\nexit 0\n")
            path.chmod(0o755)
        self.commit_all(root, "Add project workflow adapter")
        return root

    def test_preflight_accepts_ready_revision(self) -> None:
        root = self.create_project()
        result = preflight_project_revision(root, "HEAD", "HEAD")

        self.assertEqual(result.project_name, "example")
        self.assertEqual(result.workflow_prefix, "Example")
        self.assertEqual(
            {path.path for path in result.checked_paths},
            {
                ".kent/project-contract.md",
                ".kent/scripts/workflow-verification-dispatch",
                ".kent/scripts/workflow-verify",
                ".kent/workflow-profile.toml",
            },
        )

    def test_preflight_rejects_revision_without_profile(self) -> None:
        root = self.create_project()
        baseline = self.rev_parse(root, "HEAD")
        self.run_git(root, "rm", "-q", ".kent/workflow-profile.toml")
        self.commit_all(root, "Remove profile")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "project profile not found",
        ):
            preflight_project_revision(root, "HEAD", baseline)

    def test_preflight_rejects_missing_command(self) -> None:
        root = self.create_project()
        baseline = self.rev_parse(root, "HEAD")
        self.run_git(root, "rm", "-q", ".kent/scripts/workflow-verify")
        self.commit_all(root, "Remove verifier")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "required path not found.*workflow-verify",
        ):
            preflight_project_revision(root, "HEAD", baseline)

    def test_preflight_rejects_non_executable_command(self) -> None:
        root = self.create_project()
        baseline = self.rev_parse(root, "HEAD")
        verifier = root / ".kent" / "scripts" / "workflow-verify"
        verifier.chmod(0o644)
        self.commit_all(root, "Drop executable mode")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "not executable.*100644",
        ):
            preflight_project_revision(root, "HEAD", baseline)

    def test_preflight_rejects_revision_before_audited_baseline(self) -> None:
        root = self.create_project()
        older_revision = self.rev_parse(root, "HEAD")
        self.add_required_adapter(root)
        baseline = self.rev_parse(root, "HEAD")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "is not an ancestor",
        ):
            preflight_project_revision(root, older_revision, baseline)

    def test_preflight_rejects_profile_weakening_after_baseline(self) -> None:
        root = self.create_project()
        self.add_required_adapter(root)
        baseline = self.rev_parse(root, "HEAD")
        profile = root / ".kent" / "workflow-profile.toml"
        profile.write_text(
            profile.read_text().replace(
                'required_adapters = ["mobile_resource_lock"]',
                "required_adapters = []",
            )
        )
        self.commit_all(root, "Weaken adapter contract")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "differs from audited baseline.*required_adapters",
        ):
            preflight_project_revision(root, "HEAD", baseline)

    def test_cli_reports_ready_revision_as_json(self) -> None:
        root = self.create_project()
        script = REPO_ROOT / "scripts" / "preflight-revision"
        launcher = root / "kent-preflight-revision"
        launcher.symlink_to(script)
        result = subprocess.run(
            [
                str(launcher),
                "--project",
                str(root),
                "--ref",
                "HEAD",
                "--baseline-ref",
                "HEAD",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": sys.executable,
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["requested_ref"], "HEAD")
        self.assertEqual(payload["baseline_ref"], "HEAD")

    def test_cli_rejects_invalid_explicit_python_override(self) -> None:
        root = self.create_project()
        fake_python = root / "python3-old"
        fake_python.write_text("#!/bin/sh\nexit 1\n")
        fake_python.chmod(0o755)
        result = subprocess.run(
            [
                str(REPO_ROOT / "scripts" / "preflight-revision"),
                "--project",
                str(root),
                "--ref",
                "HEAD",
                "--baseline-ref",
                "HEAD",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": str(fake_python),
            },
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("must be Python 3.11 or newer", result.stderr)

    def test_cli_does_not_import_project_local_workflowkit(self) -> None:
        root = self.create_project()
        marker = root / "shadow-imported"
        shadow = root / "workflowkit"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("")
        (shadow / "revision_cli.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('unsafe')\n"
        )
        hostile_path = root / "hostile-python-path"
        hostile_path.mkdir()
        (hostile_path / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('unsafe')\n"
        )
        result = subprocess.run(
            [
                str(REPO_ROOT / "scripts" / "preflight-revision"),
                "--project",
                str(root),
                "--ref",
                "HEAD",
                "--baseline-ref",
                "HEAD",
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": sys.executable,
                "PYTHONPATH": str(hostile_path),
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertTrue(json.loads(result.stdout)["ready"])

    def test_cli_help_uses_stable_program_name(self) -> None:
        result = subprocess.run(
            [str(REPO_ROOT / "scripts" / "preflight-revision"), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": sys.executable,
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            result.stdout.startswith("usage: kent-preflight-revision"),
            result.stdout,
        )

    def add_required_adapter(self, root: Path) -> None:
        profile = root / ".kent" / "workflow-profile.toml"
        profile.write_text(
            profile.read_text().replace(
                "required_adapters = []",
                'required_adapters = ["mobile_resource_lock"]',
            )
            + (
                "\n[adapters]\n"
                "mobile_resource_lock = "
                '".kent/adapters/mobile/emulator-resource-lock.sh"\n'
            )
        )
        adapter = (
            root
            / ".kent"
            / "adapters"
            / "mobile"
            / "emulator-resource-lock.sh"
        )
        adapter.parent.mkdir(parents=True)
        adapter.write_text("#!/usr/bin/env bash\nexit 0\n")
        adapter.chmod(0o755)
        self.commit_all(root, "Add audited adapter contract")

    def rev_parse(self, root: Path, revision: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", revision],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()

    def commit_all(self, root: Path, message: str) -> None:
        self.run_git(root, "add", "-A")
        self.run_git(root, "commit", "-q", "-m", message)

    def run_git(self, root: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
