from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "templates" / "project" / "workflow-branch-identity"


def load_script_module():
    loader = importlib.machinery.SourceFileLoader(
        "workflow_branch_identity",
        str(SCRIPT),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("cannot load branch identity script")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


BRANCH_IDENTITY = load_script_module()


class BranchIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.remote = Path(temporary.name) / "remote.git"
        self.root.mkdir()
        self.run_git(self.root, "init", "-q", "-b", "TASK-1")
        self.run_git(self.root, "config", "user.name", "Kent Test")
        self.run_git(self.root, "config", "user.email", "kent@example.com")
        (self.root / "README.md").write_text("test\n")
        self.run_git(self.root, "add", "README.md")
        self.run_git(self.root, "commit", "-q", "-m", "Initial")
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.remote)],
            check=True,
        )
        self.run_git(self.root, "remote", "add", "origin", str(self.remote))

        kent = Path(temporary.name) / "kent"
        kent.write_text("#!/bin/sh\ncat \"$KENT_TASK_PAYLOAD\"\n")
        kent.chmod(0o755)
        self.kent = kent
        self.payload_path = Path(temporary.name) / "task.json"

    def run_git(
        self,
        root: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def configure(self, policy: str) -> None:
        profile = self.root / ".kent" / "workflow-profile.toml"
        profile.parent.mkdir()
        profile.write_text(
            "[policies]\n"
            f'branch_identity = "{policy}"\n'
        )

    def task(
        self,
        *,
        source_url: str = "",
        body: str = "",
        short_id: str = "TASK-1",
    ) -> None:
        self.payload_path.write_text(
            json.dumps(
                {
                    "summary": {"short_id": short_id},
                    "source_url": source_url,
                    "body": body,
                }
            )
        )

    def run_script(
        self,
        *,
        handoff: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        environment = os.environ.copy()
        environment["KENT_BIN"] = str(self.kent)
        environment["KENT_TASK_PAYLOAD"] = str(self.payload_path)
        workflow_input = {"_kent": {"task_id": "task-uuid"}}
        if handoff:
            workflow_input.update(
                {
                    "workspace_path": str(self.root),
                    "plan_path": ".todo/canary/plan.md",
                    "work_kind": "test",
                }
            )
        result = subprocess.run(
            [str(SCRIPT)],
            cwd=self.root,
            input=json.dumps(workflow_input),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        return result, payload

    def branch(self) -> str:
        return self.run_git(
            self.root,
            "branch",
            "--show-current",
        ).stdout.strip()

    def test_jira_source_url_wins_and_renames_branch(self) -> None:
        self.configure("jira")
        self.task(
            source_url="https://example.atlassian.net/browse/MBL-742",
            body="Related MBL-999",
        )

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(self.branch(), "feature/MBL-742")

    def test_delivery_handoff_is_preserved_after_rename(self) -> None:
        self.configure("jira")
        self.task(source_url="https://example.atlassian.net/browse/MBL-742")

        result, payload = self.run_script(handoff=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["workspace_path"], str(self.root))
        self.assertEqual(payload["plan_path"], ".todo/canary/plan.md")
        self.assertEqual(payload["work_kind"], "test")

    def test_runtime_failure_routes_to_blocked_with_handoff(self) -> None:
        self.configure("jira")
        self.task(source_url="https://example.atlassian.net/browse/MBL-742")
        environment = os.environ.copy()
        environment["KENT_BIN"] = str(self.root / "missing-kent")
        result = subprocess.run(
            [str(SCRIPT)],
            cwd=self.root,
            input=json.dumps(
                {
                    "workspace_path": str(self.root),
                    "plan_path": "not-applicable",
                    "work_kind": "test",
                    "_kent": {"task_id": "task-uuid"},
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_blocked")
        self.assertEqual(payload["workspace_path"], str(self.root))
        self.assertEqual(payload["plan_path"], "not-applicable")
        self.assertEqual(payload["work_kind"], "test")
        self.assertIn("инфраструктурной", payload["blocker_reason"])

    def test_jira_body_uses_first_listed_key(self) -> None:
        self.configure("jira")
        self.task(body="Issues:\n- MBL-783\n- MBL-784\n")

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(self.branch(), "feature/MBL-783")

    def test_missing_external_id_keeps_task_branch(self) -> None:
        self.configure("jira")
        self.task(body="No external issue is linked.")

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(self.branch(), "TASK-1")

    def test_local_collision_blocks_without_renaming(self) -> None:
        self.configure("jira")
        self.task(source_url="https://example.atlassian.net/browse/MBL-742")
        self.run_git(self.root, "branch", "feature/MBL-742")

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_blocked")
        self.assertIn("уже существует", payload["blocker_reason"])
        self.assertEqual(self.branch(), "TASK-1")

    def test_remote_collision_blocks_without_renaming(self) -> None:
        self.configure("jira")
        self.task(source_url="https://example.atlassian.net/browse/MBL-742")
        self.run_git(
            self.root,
            "push",
            "-q",
            "origin",
            "HEAD:refs/heads/feature/MBL-742",
        )

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_blocked")
        self.assertIn("Remote-ветка", payload["blocker_reason"])
        self.assertEqual(self.branch(), "TASK-1")

    def test_rerun_is_idempotent_after_rename(self) -> None:
        self.configure("jira")
        self.task(source_url="https://example.atlassian.net/browse/MBL-742")
        first, _ = self.run_script()
        second, payload = self.run_script()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(self.branch(), "feature/MBL-742")

    def test_github_issue_accepts_only_same_repository(self) -> None:
        self.run_git(
            self.root,
            "remote",
            "set-url",
            "origin",
            "git@github.com:rovkinmax/Puber.git",
        )

        same = BRANCH_IDENTITY.github_issue_number(
            self.root,
            "https://github.com/rovkinmax/Puber/issues/51",
            "",
        )
        cross = BRANCH_IDENTITY.github_issue_number(
            self.root,
            "https://github.com/other/Puber/issues/52",
            "",
        )
        body_fallback = BRANCH_IDENTITY.github_issue_number(
            self.root,
            "https://github.com/other/Puber/issues/52",
            "Use https://github.com/rovkinmax/Puber/issues/53.",
        )

        self.assertEqual(same, "51")
        self.assertEqual(cross, "")
        self.assertEqual(body_fallback, "53")


if __name__ == "__main__":
    unittest.main()
