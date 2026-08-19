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
WORK_KIND_PROCEDURES = (
    ".kent/commands/feature-start.md",
    ".kent/commands/feature-implement.md",
    ".kent/commands/bugfix-start.md",
    ".kent/commands/bugfix-implement.md",
    ".kent/commands/refactor-start.md",
    ".kent/commands/migration-start.md",
    ".kent/commands/dependency-update.md",
    ".kent/commands/test-coverage.md",
)
CONTEXT_MANIFESTS = (
    ".kent/context/plan.md",
    ".kent/context/implement.md",
    ".kent/context/review.md",
    ".kent/context/smoke.md",
    ".kent/context/delivery.md",
)


def schema4_profile_contents(*, metadata_only: bool = False) -> str:
    contents = EXAMPLE_PROFILE.read_text()
    contents = contents.replace(
        "schema_version = 3\n",
        (
            "schema_version = 4\n"
            'kit_managed_commands = ["dispatch"]\n'
        ),
    )
    contents = contents.replace('release_topology = "none"\n', "")
    topology_kind = (
        "sdk-merged-main-publication"
        if metadata_only
        else "appsome-release-publication"
    )
    adoption_mode = "metadata-only" if metadata_only else "managed-in-place"
    builder_path = "" if metadata_only else ".kent/release/build.sh"
    contents += (
        "\n[command_versions]\n"
        'dispatch = "1.2.3"\n'
        "\n[release]\n"
        f'topology_kind = "{topology_kind}"\n'
        f'adoption_mode = "{adoption_mode}"\n'
        'spec_path = ".kent/release/spec.toml"\n'
        f'builder_path = "{builder_path}"\n'
        'snapshot_path = ".kent/release/snapshot.toml"\n'
    )
    return contents


class RevisionPreflightTest(unittest.TestCase):
    def create_project(
        self,
        *,
        schema4: bool = False,
        metadata_only: bool = False,
    ) -> Path:
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
        (kent / "workflow-profile.toml").write_text(
            schema4_profile_contents(metadata_only=metadata_only)
            if schema4
            else EXAMPLE_PROFILE.read_text()
        )
        (kent / "project-contract.md").write_text("# Project contract\n")
        for configured_path in WORK_KIND_PROCEDURES + CONTEXT_MANIFESTS:
            path = root / configured_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Test procedure\n")
        for name in (
            "workflow-checkpoint",
            "workflow-evidence-ledger",
            "workflow-plan-contract",
            "workflow-plan-contract-accept",
            "workflow-plan-contract-continue",
            "workflow-plan-contract-fix-continue",
            "workflow-plan-contract-verify",
            "workflow-task-janitor",
            "workflow-verification-dispatch",
            "workflow-verify",
            "workflow-wait-github-ci",
            "workflow-wait-github-pr",
        ):
            path = scripts / name
            path.write_text("#!/usr/bin/env bash\nexit 0\n")
            path.chmod(0o755)
        if schema4:
            for configured_path in (
                ".kent/release/spec.toml",
                ".kent/release/snapshot.toml",
            ):
                path = root / configured_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Test release artifact\n")
            if not metadata_only:
                builder = root / ".kent/release/build.sh"
                builder.write_text("#!/usr/bin/env bash\nexit 0\n")
        self.commit_all(root, "Add project workflow adapter")
        return root

    def test_preflight_accepts_ready_revision(self) -> None:
        root = self.create_project()
        result = preflight_project_revision(root, "HEAD")

        self.assertEqual(result.project_name, "example")
        self.assertEqual(result.workflow_prefix, "Example")
        self.assertEqual(
            {path.path for path in result.checked_paths},
            {
                ".kent/project-contract.md",
                ".kent/scripts/workflow-checkpoint",
                ".kent/scripts/workflow-evidence-ledger",
                ".kent/scripts/workflow-plan-contract",
                ".kent/scripts/workflow-plan-contract-accept",
                ".kent/scripts/workflow-plan-contract-continue",
                ".kent/scripts/workflow-plan-contract-fix-continue",
                ".kent/scripts/workflow-plan-contract-verify",
                ".kent/scripts/workflow-task-janitor",
                ".kent/scripts/workflow-verification-dispatch",
                ".kent/scripts/workflow-verify",
                ".kent/scripts/workflow-wait-github-ci",
                ".kent/scripts/workflow-wait-github-pr",
                ".kent/workflow-profile.toml",
                *WORK_KIND_PROCEDURES,
                *CONTEXT_MANIFESTS,
            },
        )

    def test_preflight_rejects_revision_without_profile(self) -> None:
        root = self.create_project()
        self.run_git(root, "rm", "-q", ".kent/workflow-profile.toml")
        self.commit_all(root, "Remove profile")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "project profile not found",
        ):
            preflight_project_revision(root, "HEAD")

    def test_preflight_rejects_missing_command(self) -> None:
        root = self.create_project()
        self.run_git(root, "rm", "-q", ".kent/scripts/workflow-verify")
        self.commit_all(root, "Remove verifier")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "required path not found.*workflow-verify",
        ):
            preflight_project_revision(root, "HEAD")

    def test_preflight_rejects_non_executable_command(self) -> None:
        root = self.create_project()
        verifier = root / ".kent" / "scripts" / "workflow-verify"
        verifier.chmod(0o644)
        self.commit_all(root, "Drop executable mode")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "not executable.*100644",
        ):
            preflight_project_revision(root, "HEAD")

    def test_preflight_uses_selected_profile_adapter_requirements(self) -> None:
        root = self.create_project()
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
        self.commit_all(root, "Require mobile resource lock")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "required path not found.*emulator-resource-lock",
        ):
            preflight_project_revision(root, "HEAD")

    def test_preflight_includes_schema_four_release_closure(self) -> None:
        root = self.create_project(schema4=True)

        result = preflight_project_revision(root, "HEAD")

        checked = {path.path for path in result.checked_paths}
        self.assertIn(".kent/release/spec.toml", checked)
        self.assertIn(".kent/release/build.sh", checked)
        self.assertIn(".kent/release/snapshot.toml", checked)

    def test_preflight_omits_optional_metadata_only_builder(self) -> None:
        root = self.create_project(schema4=True, metadata_only=True)

        checked = {
            path.path
            for path in preflight_project_revision(root, "HEAD").checked_paths
        }

        self.assertIn(".kent/release/spec.toml", checked)
        self.assertNotIn(".kent/release/build.sh", checked)
        self.assertIn(".kent/release/snapshot.toml", checked)

    def test_preflight_requires_schema_four_release_files(self) -> None:
        root = self.create_project(schema4=True)
        self.run_git(root, "rm", "-q", ".kent/release/snapshot.toml")
        self.commit_all(root, "Remove release snapshot")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "required path not found.*release/snapshot.toml",
        ):
            preflight_project_revision(root, "HEAD")

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
        self.assertNotIn("--baseline-ref", result.stdout)

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
