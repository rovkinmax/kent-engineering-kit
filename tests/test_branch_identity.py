from __future__ import annotations

import importlib.machinery
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


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
    def test_handoff_preserves_exact_delivery_context(self) -> None:
        from workflowkit import runtime

        raw = '{ "schema": "workflow-delivery-context-v1", "phase": "pre_pr" }'
        incoming = {
            "workspace_path": str(self.root),
            "plan_path": ".todo/plan.md",
            "work_kind": "test",
            "delivery_context": raw,
        }
        with mock.patch.dict(sys.modules, {"workflow_runtime_contracts": runtime}):
            self.assertEqual(BRANCH_IDENTITY.handoff_values(incoming), incoming)
            with self.assertRaises(ValueError):
                BRANCH_IDENTITY.handoff_values({**incoming, "delivery_context": "{}"})
            with self.assertRaises(ValueError):
                BRANCH_IDENTITY.handoff_values({"delivery_context": raw})

    def test_jira_continuation_remote_collision_is_not_reused(self) -> None:
        self.configure("jira")
        branch = "feature/MBL-826-continue"
        self.run_git(self.root, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        self.task(
            source_url="https://example.atlassian.net/browse/MBL-826",
            body=f"branch_name: {branch}",
        )
        result, payload = self.run_script(handoff=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_blocked")
        self.assertEqual(self.branch(), "TASK-1")

    def test_explicit_jira_continuation_branch(self) -> None:
        self.configure("jira")
        self.task(
            source_url="https://example.atlassian.net/browse/MBL-826",
            body="Approved continuation.\nbranch_name: feature/MBL-826-continue\n",
        )
        result, payload = self.run_script(handoff=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(self.branch(), "feature/MBL-826-continue")

    def test_jira_branch_directive_rejects_ambiguous_wrong_key_and_unsafe(self) -> None:
        self.configure("jira")
        for body in (
            "branch_name: feature/MBL-827-continue",
            "branch_name: feature/MBL-826-continue\nbranch_name: feature/MBL-826-next",
            "branch_name: feature/MBL-826-../unsafe",
            "branch_name: feature/MBL-826-",
            "branch_name: unrelated",
        ):
            with self.subTest(body=body):
                self.task(
                    source_url="https://example.atlassian.net/browse/MBL-826",
                    body=body,
                )
                result, payload = self.run_script(handoff=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(payload["transition"], "branch_identity_blocked")
                self.assertEqual(self.branch(), "TASK-1")

    def test_jira_continuation_branch_collision_is_not_reused(self) -> None:
        self.configure("jira")
        branch = "feature/MBL-826-continue"
        self.run_git(self.root, "branch", branch)
        self.task(
            source_url="https://example.atlassian.net/browse/MBL-826",
            body=f"branch_name: {branch}",
        )
        result, payload = self.run_script(handoff=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_blocked")
        self.assertEqual(self.branch(), "TASK-1")

    def test_kent_binary_falls_back_when_service_path_is_minimal(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fake_kent = Path(temporary.name) / "kent"
        fake_kent.write_text("#!/bin/sh\nexit 0\n")
        fake_kent.chmod(0o755)

        with (
            mock.patch.object(
                BRANCH_IDENTITY,
                "DEFAULT_KENT_PATHS",
                (str(fake_kent),),
            ),
            mock.patch.dict(
                os.environ,
                {"PATH": "/usr/bin:/bin"},
                clear=True,
            ),
        ):
            self.assertEqual(
                BRANCH_IDENTITY.resolve_kent_bin(),
                str(fake_kent),
            )

    def test_system_python_can_compile_runtime_script(self) -> None:
        system_python = Path("/usr/bin/python3")
        if not system_python.is_file():
            self.skipTest("/usr/bin/python3 is unavailable")
        result = subprocess.run(
            [str(system_python), "-m", "py_compile", str(SCRIPT)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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
        kent.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "task" ] && [ "$2" = "sessions" ]; then\n'
            '  exec /bin/cat "$KENT_SESSION_PAYLOAD"\n'
            "fi\n"
            'exec /bin/cat "$KENT_TASK_PAYLOAD"\n'
        )
        kent.chmod(0o755)
        self.kent = kent
        self.payload_path = Path(temporary.name) / "task.json"
        self.sessions_path = Path(temporary.name) / "sessions.json"
        self.sessions_path.write_text(
            json.dumps({"task_id": "task-uuid", "items": []})
        )
        self.coder_handoff: dict[str, str] = {}

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

    def configure(
        self,
        policy: str,
        *,
        coder_selection: bool = False,
    ) -> None:
        profile = self.root / ".kent" / "workflow-profile.toml"
        profile.parent.mkdir()
        profile_contents = (
            "[policies]\n"
            f'branch_identity = "{policy}"\n'
        )
        if coder_selection:
            profile_contents += (
                'writer_sessions = "continuous"\n'
                'coder_selection = "complexity"\n'
            )
        profile.write_text(profile_contents)

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
                    "summary": {"id": "task-uuid", "short_id": short_id},
                    "source_url": source_url,
                    "body": body,
                }
            )
        )

    def run_script(
        self,
        *,
        handoff: bool = False,
        coder_selection: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        environment = os.environ.copy()
        environment["KENT_BIN"] = str(self.kent)
        environment["KENT_TASK_PAYLOAD"] = str(self.payload_path)
        environment["KENT_SESSION_PAYLOAD"] = str(self.sessions_path)
        workflow_input = {"_kent": {"task_id": "task-uuid"}}
        if handoff:
            workflow_input.update(
                {
                    "workspace_path": str(self.root),
                    "plan_path": ".todo/canary/plan.md",
                    "work_kind": "test",
                }
            )
        if coder_selection:
            workflow_input.update(self.coder_handoff)
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

    def prepare_coder_selection(self, complexity: str) -> Path:
        plan = self.root / ".todo" / "canary" / "plan.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        selection = {
            "schema": "coder-selection-v1",
            "task_short_id": "TASK-1",
            "complexity": complexity,
            "rationale": "The accepted implementation scope supports this choice.",
        }
        contents = (
            "# Accepted plan\n\n"
            "- [ ] Implement the reviewed behavior.\n\n"
            "```json\n"
            + json.dumps(selection, indent=2)
            + "\n```\n"
        )
        plan.write_text(contents)
        normalized = BRANCH_IDENTITY.normalized_plan_bytes(plan)
        digest = hashlib.sha256(normalized).hexdigest()
        runtime = self.root / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        (runtime / "plan-contract.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_short_id": "TASK-1",
                    "plan_path": ".todo/canary/plan.md",
                    "work_kind": "test",
                    "normalized_sha256": digest,
                    "normalized_plan": normalized.decode("utf-8"),
                    "coder_selection": selection,
                    "coder_selection_sha256": digest,
                }
            )
        )
        self.coder_handoff = {
            "task_short_id": "TASK-1",
            "accepted_plan_sha256": digest,
            "plan_route": "start",
            "plan_route_context": "not-applicable",
            "review_context": "Reviewed selection with human approval.",
        }
        return plan

    def branch(self) -> str:
        return self.run_git(
            self.root,
            "branch",
            "--show-current",
        ).stdout.strip()

    def test_complexity_selection_is_revalidated_and_emitted_on_branch_entry(
        self,
    ) -> None:
        self.configure("task", coder_selection=True)
        self.task(short_id="TASK-1")
        plan = self.prepare_coder_selection("simple")
        snapshot_path = (
            self.root
            / ".kent"
            / "runtime"
            / "TASK-1"
            / "plan-contract.json"
        )
        accepted_snapshot = snapshot_path.read_text()

        result, payload = self.run_script(handoff=True, coder_selection=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(payload["target_assignee"], "implementation-simple")
        self.assertEqual(payload["target_thinking"], "xhigh")
        self.assertEqual(payload["accepted_plan_sha256"], self.coder_handoff["accepted_plan_sha256"])

        retry, retry_payload = self.run_script(
            handoff=True,
            coder_selection=True,
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(retry_payload["transition"], "branch_identity_ready")
        self.assertEqual(retry_payload["target_assignee"], "implementation-simple")
        self.assertEqual(retry_payload["target_thinking"], "xhigh")
        self.assertEqual(
            retry_payload["accepted_plan_sha256"],
            self.coder_handoff["accepted_plan_sha256"],
        )

        accepted_plan = plan.read_text()
        plan.write_text(plan.read_text() + "\nPlan changed after acceptance.\n")
        stale, stale_payload = self.run_script(
            handoff=True,
            coder_selection=True,
        )
        self.assertEqual(stale.returncode, 0, stale.stderr)
        self.assertEqual(stale_payload["transition"], "branch_identity_revalidate")
        self.assertNotIn("target_assignee", stale_payload)
        self.assertIn("changed", stale_payload["plan_change_report"])

        plan.write_text(accepted_plan)
        substituted_snapshot = json.loads(accepted_snapshot)
        substituted_snapshot["coder_selection"]["complexity"] = "complex"
        snapshot_path.write_text(json.dumps(substituted_snapshot))
        substituted, substituted_payload = self.run_script(
            handoff=True,
            coder_selection=True,
        )
        self.assertEqual(substituted.returncode, 0, substituted.stderr)
        self.assertEqual(
            substituted_payload["transition"],
            "branch_identity_revalidate",
        )
        self.assertIn(
            "differs from the digest-bound plan choice",
            substituted_payload["plan_change_report"],
        )
        snapshot_path.write_text(accepted_snapshot)

        self.sessions_path.write_text(
            json.dumps(
                {
                    "task_id": "task-uuid",
                    "items": [
                        {
                            "session_id": "existing-writer",
                            "agent_role": "implementation-simple",
                            "status": "idle",
                        }
                    ],
                }
            )
        )
        existing, existing_payload = self.run_script(
            handoff=True,
            coder_selection=True,
        )
        self.assertEqual(existing.returncode, 0, existing.stderr)
        self.assertEqual(
            existing_payload["transition"],
            "branch_identity_revalidate",
        )
        self.assertIn(
            "already exists",
            existing_payload["plan_change_report"],
        )

    def test_malformed_complexity_types_revalidate_before_writer_dispatch(self) -> None:
        self.configure("task", coder_selection=True)
        self.task(short_id="TASK-1")
        plan = self.prepare_coder_selection("simple")
        snapshot_path = self.root / ".kent/runtime/TASK-1/plan-contract.json"
        original_snapshot = json.loads(snapshot_path.read_text())
        original_plan = plan.read_text()
        for location in ("snapshot", "plan"):
            for malformed in ([], {}):
                with self.subTest(location=location, complexity=malformed):
                    snapshot = json.loads(json.dumps(original_snapshot))
                    plan.write_text(original_plan)
                    if location == "snapshot":
                        snapshot["coder_selection"]["complexity"] = malformed
                    else:
                        # Keep the digest valid so entry reaches the plan parser,
                        # rather than being rejected by an earlier drift check.
                        plan.write_text(original_plan.replace(
                            '"complexity": "simple"',
                            f'"complexity": {json.dumps(malformed)}',
                        ))
                        normalized = BRANCH_IDENTITY.normalized_plan_bytes(plan)
                        digest = hashlib.sha256(normalized).hexdigest()
                        snapshot.update(
                            normalized_plan=normalized.decode("utf-8"),
                            normalized_sha256=digest,
                            coder_selection_sha256=digest,
                        )
                    snapshot_path.write_text(json.dumps(snapshot))
                    self.coder_handoff["accepted_plan_sha256"] = snapshot["normalized_sha256"]
                    branch_before = self.branch()
                    result, payload = self.run_script(handoff=True, coder_selection=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(payload["transition"], "branch_identity_revalidate")
                    self.assertIn("malformed", payload["plan_change_report"])
                    self.assertEqual(payload["accepted_plan_sha256"], snapshot["normalized_sha256"])
                    self.assertNotIn("target_assignee", payload)
                    self.assertNotIn("target_thinking", payload)
                    self.assertEqual(self.branch(), branch_before)

    def test_complexity_selection_survives_branch_collision_retry(self) -> None:
        self.configure("jira", coder_selection=True)
        self.task(
            source_url="https://example.atlassian.net/browse/MBL-780",
            short_id="TASK-1",
        )
        self.prepare_coder_selection("complex")
        accepted_digest = self.coder_handoff["accepted_plan_sha256"]
        desired_branch = "feature/MBL-780"
        self.run_git(self.root, "branch", desired_branch)

        blocked, blocked_payload = self.run_script(
            handoff=True,
            coder_selection=True,
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(blocked_payload["transition"], "branch_identity_blocked")
        self.assertTrue(blocked_payload["blocker_reason"])
        self.assertNotIn("target_assignee", blocked_payload)

        # The collision belongs only to this disposable Git fixture. After its
        # explicit resolution, the same accepted snapshot is revalidated.
        self.run_git(self.root, "branch", "-D", desired_branch)
        retried, retried_payload = self.run_script(
            handoff=True,
            coder_selection=True,
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(retried_payload["transition"], "branch_identity_ready")
        self.assertEqual(
            (
                retried_payload["target_assignee"],
                retried_payload["target_thinking"],
                retried_payload["accepted_plan_sha256"],
            ),
            ("implementation-complex", "medium", accepted_digest),
        )

    def test_jira_source_url_wins_and_renames_branch(self) -> None:
        self.configure("jira")
        self.task(
            source_url="https://example.atlassian.net/browse/MBL-742",
            body=(
                "Related evidence: "
                "https://example.atlassian.net/browse/MBL-999"
            ),
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

    def test_jira_body_uses_single_issue_url(self) -> None:
        self.configure("jira")
        self.task(
            body="Root: https://example.atlassian.net/browse/MBL-783"
        )

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(self.branch(), "feature/MBL-783")

    def test_jira_body_with_multiple_issue_urls_is_ambiguous(self) -> None:
        self.configure("jira")
        self.task(
            body=(
                "Issues:\n"
                "- https://example.atlassian.net/browse/MBL-783\n"
                "- https://example.atlassian.net/browse/MBL-784\n"
            )
        )

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_blocked")
        self.assertIn("MBL-783, MBL-784", payload["blocker_reason"])
        self.assertIn("root issue", payload["blocker_reason"])
        self.assertEqual(self.branch(), "TASK-1")

    def test_jira_body_ignores_referenced_task_keys(self) -> None:
        self.configure("jira")
        self.task(
            short_id="OSM-53",
            body=(
                "The exact release baseline and OSM-51 PR #1542 fail alike.\n"
                "Evidence: https://github.com/example/repo/actions/runs/123.\n"
                "Do not modify OSM-51 or OSM-52 from this task.\n"
            ),
        )
        self.run_git(self.root, "branch", "-m", "OSM-53")

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(self.branch(), "OSM-53")

    def test_jira_body_plain_key_is_not_authoritative(self) -> None:
        self.configure("jira")
        self.task(body="Related issue MBL-783 is evidence, not task identity.")

        result, payload = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["transition"], "branch_identity_ready")
        self.assertEqual(self.branch(), "TASK-1")

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

    def test_github_issue_body_with_multiple_candidates_is_ambiguous(self) -> None:
        self.run_git(
            self.root,
            "remote",
            "set-url",
            "origin",
            "git@github.com:rovkinmax/Puber.git",
        )

        with self.assertRaisesRegex(
            BRANCH_IDENTITY.IdentityAmbiguity,
            "51, 52",
        ):
            BRANCH_IDENTITY.github_issue_number(
                self.root,
                "",
                (
                    "Root? https://github.com/rovkinmax/Puber/issues/51\n"
                    "Related? https://github.com/rovkinmax/Puber/issues/52"
                ),
            )


if __name__ == "__main__":
    unittest.main()
