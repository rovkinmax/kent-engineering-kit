from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = REPO_ROOT / "templates" / "project" / "workflow-checkpoint"
PR_WATCH = REPO_ROOT / "templates" / "project" / "workflow-wait-github-pr"
JANITOR = REPO_ROOT / "templates" / "project" / "workflow-task-janitor"


class GitRepositoryTest(unittest.TestCase):
    def create_repository(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.run_git(root, "init", "-q")
        self.run_git(root, "config", "user.name", "Kent Test")
        self.run_git(root, "config", "user.email", "kent@example.invalid")
        (root / ".kent").mkdir()
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        (root / "tracked.txt").write_text("ready\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "Initial")
        return root

    def run_git(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )


class WorkflowCheckpointTest(GitRepositoryTest):
    def test_checkpoint_write_read_and_validate(self) -> None:
        root = self.create_repository()
        payload = {
            "completed": ["diagnosis"],
            "remaining": ["focused fix"],
            "mutation_ledger": [],
            "next_action": "Apply the focused fix.",
        }
        write = subprocess.run(
            [
                str(CHECKPOINT),
                "write",
                "--stage",
                "fix",
                "--task",
                "TASK-1",
                "--workspace",
                str(root),
            ],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(write.returncode, 0, write.stderr)
        path = root / ".kent" / "runtime" / "TASK-1" / "fix-checkpoint.json"
        stored = json.loads(path.read_text())
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(stored["task_short_id"], "TASK-1")
        self.assertEqual(stored["stage"], "fix")
        self.assertEqual(stored["workspace_path"], str(root.resolve()))

        validate = subprocess.run(
            [
                str(CHECKPOINT),
                "validate",
                "--stage",
                "fix",
                "--task",
                "TASK-1",
                "--workspace",
                str(root),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(validate.returncode, 0, validate.stderr)
        self.assertTrue(json.loads(validate.stdout)["valid"])

    def test_checkpoint_refuses_tracked_runtime_path(self) -> None:
        root = self.create_repository()
        (root / ".gitignore").write_text("")
        payload = {
            "completed": [],
            "remaining": ["smoke"],
            "mutation_ledger": [],
            "next_action": "Acquire the runtime target.",
        }
        result = subprocess.run(
            [
                str(CHECKPOINT),
                "write",
                "--stage",
                "smoke",
                "--task",
                "TASK-2",
                "--workspace",
                str(root),
            ],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not ignored by Git", result.stderr)

    def test_checkpoint_rejects_runtime_symlink_escape(self) -> None:
        root = self.create_repository()
        external_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(external_temporary.cleanup)
        external = Path(external_temporary.name)
        (root / ".kent" / "runtime").symlink_to(
            external,
            target_is_directory=True,
        )
        result = subprocess.run(
            [
                str(CHECKPOINT),
                "write",
                "--stage",
                "fix",
                "--task",
                "TASK-3",
                "--workspace",
                str(root),
            ],
            input=json.dumps(
                {
                    "completed": [],
                    "remaining": ["repair"],
                    "mutation_ledger": [],
                    "next_action": "Repair safely.",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(external.iterdir()), [])


class GitHubPrWatchTest(GitRepositoryTest):
    def fake_gh(self, root: Path, payload: dict[str, object]) -> Path:
        payload_path = root / "pr-state.json"
        payload_path.write_text(json.dumps(payload))
        executable = root / "gh"
        executable.write_text(
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            '  "pr view") cat "$KENT_TEST_PR_STATE" ;;\n'
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        executable.chmod(0o755)
        return executable

    def watch(
        self,
        root: Path,
        state: dict[str, object],
        *,
        head: str = "abc123",
    ) -> dict[str, object]:
        fake_gh = self.fake_gh(root, state)
        result = subprocess.run(
            [str(PR_WATCH)],
            cwd=root,
            input=json.dumps(
                {
                    "workspace_path": str(root),
                    "pr_url": "https://github.com/example/repo/pull/1",
                    "branch_name": "TASK-1",
                    "merge_strategy": "rebase",
                    "pr_head_oid": head,
                    "pr_base_oid": "base123",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "PATH": f"{root}:{os.environ.get('PATH', '')}",
                "KENT_TEST_PR_STATE": str(root / "pr-state.json"),
                "KENT_PR_WATCH_TEST_MODE": "1",
                "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
                "KENT_PR_WATCH_MAX_POLLS": "1",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_merged_pr_advances_without_agent(self) -> None:
        root = self.create_repository()
        result = self.watch(
            root,
            {
                "state": "MERGED",
                "mergedAt": "2026-08-01T00:00:00Z",
                "mergeCommit": {"oid": "def456"},
                "headRefName": "TASK-1",
                "headRefOid": "abc123",
                "baseRefName": "main",
                "baseRefOid": "base123",
                "reviewDecision": "APPROVED",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/example/repo/pull/1",
            },
        )
        self.assertEqual(result["transition"], "pr_merged")
        self.assertIn("def456", result["merge_report"])

    def test_changed_head_wakes_waiting_pr_agent(self) -> None:
        root = self.create_repository()
        result = self.watch(
            root,
            {
                "state": "OPEN",
                "mergedAt": None,
                "mergeCommit": None,
                "headRefName": "TASK-1",
                "headRefOid": "changed",
                "baseRefName": "main",
                "baseRefOid": "base123",
                "reviewDecision": "APPROVED",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/example/repo/pull/1",
            },
        )
        self.assertEqual(result["transition"], "state_changed")
        self.assertIn("pull_request_head_changed", result["pr_report"])

    def test_unchanged_open_pr_stays_in_script_loop(self) -> None:
        root = self.create_repository()
        result = self.watch(
            root,
            {
                "state": "OPEN",
                "mergedAt": None,
                "mergeCommit": None,
                "headRefName": "TASK-1",
                "headRefOid": "abc123",
                "baseRefName": "main",
                "baseRefOid": "base123",
                "reviewDecision": "APPROVED",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/example/repo/pull/1",
            },
        )
        self.assertEqual(result["transition"], "still_waiting")

    def test_failed_check_wakes_with_compact_report(self) -> None:
        root = self.create_repository()
        result = self.watch(
            root,
            {
                "state": "OPEN",
                "mergedAt": None,
                "mergeCommit": None,
                "headRefName": "TASK-1",
                "headRefOid": "abc123",
                "baseRefName": "main",
                "baseRefOid": "base123",
                "reviewDecision": "REVIEW_REQUIRED",
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [
                    {
                        "name": "unit",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://example.invalid/large-detail",
                    }
                ],
                "url": "https://github.com/example/repo/pull/1",
            },
        )
        self.assertEqual(result["transition"], "state_changed")
        self.assertIn('"reason": "checks_failed"', result["pr_report"])
        self.assertIn('"name": "unit"', result["pr_report"])
        self.assertNotIn("detailsUrl", result["pr_report"])

    def test_hung_github_query_is_bounded(self) -> None:
        root = self.create_repository()
        fake_gh = root / "gh-hung"
        fake_gh.write_text("#!/bin/sh\nsleep 2\n")
        fake_gh.chmod(0o755)
        result = subprocess.run(
            [str(PR_WATCH)],
            cwd=root,
            input=json.dumps(
                {
                    "workspace_path": str(root),
                    "pr_url": "https://github.com/example/repo/pull/1",
                    "branch_name": "TASK-1",
                    "merge_strategy": "rebase",
                    "pr_head_oid": "abc123",
                    "pr_base_oid": "base123",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_PR_WATCH_TEST_MODE": "1",
                "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
                "KENT_PR_WATCH_MAX_POLLS": "1",
                "KENT_PR_WATCH_MAX_ERRORS": "1",
                "KENT_PR_WATCH_QUERY_TIMEOUT_SECONDS": "1",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "state_changed")
        self.assertIn("exceeded 1 seconds", payload["pr_report"])


class WorkflowJanitorTest(GitRepositoryTest):
    def janitor_input(self, root: Path, **overrides: str) -> str:
        payload = {
            "workspace_path": str(root),
            "task_short_id": "TASK-1",
            "branch_name": "",
            "pr_url": "",
            "merge_report": "",
            "cleanup_mode": "report_only",
            "cleanup_session_id": "session-test",
            "cleanup_report": "Cleanup preflight passed.",
        }
        payload.update(overrides)
        return json.dumps(payload)

    def test_primary_checkout_is_never_deleted(self) -> None:
        root = self.create_repository()
        runtime = root / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        (runtime / "fix-checkpoint.json").write_text("{}")
        result = subprocess.run(
            [str(JANITOR)],
            cwd=root,
            input=self.janitor_input(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "done")
        self.assertIn("kept the primary checkout", payload["cleanup_report"])
        self.assertFalse(runtime.exists())

    def test_primary_runtime_symlink_is_preserved(self) -> None:
        root = self.create_repository()
        external_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(external_temporary.cleanup)
        external = Path(external_temporary.name)
        marker = external / "keep.txt"
        marker.write_text("keep\n")
        (root / ".kent" / "runtime").symlink_to(
            external,
            target_is_directory=True,
        )
        result = subprocess.run(
            [str(JANITOR)],
            cwd=root,
            input=self.janitor_input(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "done")
        self.assertIn("preserved runtime state", payload["cleanup_report"])
        self.assertEqual(marker.read_text(), "keep\n")

    def test_primary_tracked_checkpoint_is_preserved(self) -> None:
        root = self.create_repository()
        runtime = root / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        checkpoint = runtime / "fix-checkpoint.json"
        checkpoint.write_text("{}\n")
        self.run_git(root, "add", "-f", str(checkpoint.relative_to(root)))
        self.run_git(root, "commit", "-q", "-m", "Track checkpoint")
        result = subprocess.run(
            [str(JANITOR)],
            cwd=root,
            input=self.janitor_input(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("preserved runtime state", payload["cleanup_report"])
        self.assertTrue(checkpoint.is_file())
        self.assertEqual(
            self.run_git(root, "status", "--porcelain").stdout,
            "",
        )

    def test_dirty_managed_worktree_is_preserved(self) -> None:
        root = self.create_repository()
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        (worktree / "tracked.txt").write_text("dirty\n")
        result = subprocess.run(
            [str(JANITOR)],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="TASK-1",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "done")
        self.assertIn("preserved dirty worktree", payload["cleanup_report"])
        self.assertTrue(worktree.exists())

    def test_exact_merged_pr_invokes_kent_worktree_deletion(self) -> None:
        root = self.create_repository()
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
        wrapper_log = root / "wrapper-args.json"
        fake_wrapper = root / "kent-worktree"
        fake_wrapper.write_text(
            "#!/bin/sh\n"
            "python3 -c 'import json, os, sys; "
            "open(os.environ[\"KENT_TEST_WRAPPER_LOG\"], \"w\").write("
            "json.dumps(sys.argv[1:]))' \"$@\"\n"
        )
        fake_wrapper.chmod(0o755)

        result = subprocess.run(
            [str(JANITOR)],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="TASK-1",
                pr_url="https://github.com/example/repo/pull/1",
                merge_report="merged",
                cleanup_mode="merged",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_TEST_PR_STATE": str(pr_state),
                "KENT_WORKTREE_WRAPPER": str(fake_wrapper),
                "KENT_TEST_WRAPPER_LOG": str(wrapper_log),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "done")
        wrapper_args = json.loads(wrapper_log.read_text())
        self.assertEqual(wrapper_args[0:3], ["delete", "--session", "session-test"])
        self.assertIn("--delete-branch", wrapper_args)
        self.assertIn(str(worktree.resolve()), wrapper_args)

    def test_remote_branch_deletion_uses_exact_lease(self) -> None:
        root = self.create_repository()
        remote_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(remote_temporary.cleanup)
        remote = Path(remote_temporary.name)
        self.run_git(remote, "init", "--bare", "-q")
        self.run_git(root, "remote", "add", "origin", str(remote))
        primary_branch = self.run_git(
            root,
            "branch",
            "--show-current",
        ).stdout.strip()
        self.run_git(root, "push", "-q", "-u", "origin", primary_branch)

        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        self.run_git(worktree, "push", "-q", "-u", "origin", "TASK-1")
        head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
        fake_wrapper = root / "kent-worktree"
        fake_wrapper.write_text("#!/bin/sh\nexit 0\n")
        fake_wrapper.chmod(0o755)

        result = subprocess.run(
            [str(JANITOR)],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="TASK-1",
                pr_url="https://github.com/example/repo/pull/1",
                merge_report="merged",
                cleanup_mode="merged",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_TEST_PR_STATE": str(pr_state),
                "KENT_WORKTREE_WRAPPER": str(fake_wrapper),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("Remote branch removed: true", payload["cleanup_report"])
        remote_ref = subprocess.run(
            [
                "git",
                "--git-dir",
                str(remote),
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/TASK-1",
            ],
            check=False,
        )
        self.assertNotEqual(remote_ref.returncode, 0)

    def test_stale_remote_tracking_ref_does_not_authorize_cleanup(self) -> None:
        root = self.create_repository()
        remote_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(remote_temporary.cleanup)
        remote = Path(remote_temporary.name)
        self.run_git(remote, "init", "--bare", "-q")
        self.run_git(root, "remote", "add", "origin", str(remote))
        head = self.run_git(root, "rev-parse", "HEAD").stdout.strip()
        self.run_git(root, "update-ref", "refs/remotes/origin/stale", head)
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        wrapper_marker = root / "wrapper-called"
        fake_wrapper = root / "kent-worktree"
        fake_wrapper.write_text(
            "#!/bin/sh\n"
            f"touch {str(wrapper_marker)!r}\n"
        )
        fake_wrapper.chmod(0o755)

        result = subprocess.run(
            [str(JANITOR)],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="TASK-1",
                cleanup_mode="no_pr",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_WORKTREE_WRAPPER": str(fake_wrapper),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("not conclusively recoverable", payload["cleanup_report"])
        self.assertFalse(wrapper_marker.exists())
        self.assertTrue(worktree.exists())

    def test_local_branch_oid_change_is_preserved_after_worktree_removal(self) -> None:
        root = self.create_repository()
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        task_head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        (root / "tracked.txt").write_text("new main state\n")
        self.run_git(root, "add", "tracked.txt")
        self.run_git(root, "commit", "-q", "-m", "Advance main")
        new_oid = self.run_git(root, "rev-parse", "HEAD").stdout.strip()

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": task_head,
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
        fake_wrapper = root / "kent-worktree"
        fake_wrapper.write_text(
            "#!/bin/sh\n"
            "for argument in \"$@\"; do target=\"$argument\"; done\n"
            "git -C \"$KENT_TEST_PRIMARY\" worktree remove --force \"$target\"\n"
            "git -C \"$KENT_TEST_PRIMARY\" update-ref "
            "refs/heads/TASK-1 \"$KENT_TEST_NEW_OID\"\n"
        )
        fake_wrapper.chmod(0o755)

        result = subprocess.run(
            [str(JANITOR)],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="TASK-1",
                pr_url="https://github.com/example/repo/pull/1",
                merge_report="merged",
                cleanup_mode="merged",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_TEST_PR_STATE": str(pr_state),
                "KENT_WORKTREE_WRAPPER": str(fake_wrapper),
                "KENT_TEST_PRIMARY": str(root),
                "KENT_TEST_NEW_OID": new_oid,
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("OID changed", payload["cleanup_report"])
        self.assertEqual(
            self.run_git(root, "rev-parse", "refs/heads/TASK-1").stdout.strip(),
            new_oid,
        )


if __name__ == "__main__":
    unittest.main()
