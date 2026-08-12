from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = REPO_ROOT / "templates" / "project" / "workflow-checkpoint"
EVIDENCE = REPO_ROOT / "templates" / "project" / "workflow-evidence-ledger"
CI_WATCH = REPO_ROOT / "templates" / "project" / "workflow-wait-github-ci"
PR_WATCH = REPO_ROOT / "templates" / "project" / "workflow-wait-github-pr"
JANITOR = REPO_ROOT / "templates" / "project" / "workflow-task-janitor"
PLAN_CONTRACT = (
    REPO_ROOT / "templates" / "project" / "workflow-plan-contract"
)
PLAN_CONTRACT_ACCEPT = (
    REPO_ROOT / "templates" / "project" / "workflow-plan-contract-accept"
)
PLAN_CONTRACT_CHECK = {
    "continue": (
        REPO_ROOT / "templates" / "project" / "workflow-plan-contract-continue"
    ),
    "verify": (
        REPO_ROOT / "templates" / "project" / "workflow-plan-contract-verify"
    ),
    "fix_continue": (
        REPO_ROOT
        / "templates"
        / "project"
        / "workflow-plan-contract-fix-continue"
    ),
}
APK_INSTALL = (
    REPO_ROOT / "templates" / "project" / "android-apk-install-preserve"
)


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

    def run_with_open_stdin(
        self,
        command: list[str],
        *,
        initial_input: str = "",
        timeout: float = 4.0,
    ) -> tuple[int, str, str]:
        with subprocess.Popen(
            command,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            assert process.stdin is not None
            if initial_input:
                process.stdin.write(initial_input)
                process.stdin.flush()
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                self.fail("command did not finish while stdin remained open")
            finally:
                if process.stdin is not None:
                    process.stdin.close()
            assert process.stdout is not None
            assert process.stderr is not None
            return returncode, process.stdout.read(), process.stderr.read()


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

    def test_checkpoint_write_without_json_fails_instead_of_waiting(self) -> None:
        root = self.create_repository()
        result = subprocess.run(
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
            input="",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=2,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "checkpoint input requires one JSON object on stdin",
            result.stderr,
        )

    def test_checkpoint_open_empty_stdin_times_out(self) -> None:
        root = self.create_repository()
        returncode, _, stderr = self.run_with_open_stdin(
            [
                str(CHECKPOINT),
                "write",
                "--stage",
                "fix",
                "--task",
                "TASK-1",
                "--workspace",
                str(root),
            ]
        )

        self.assertEqual(returncode, 1)
        self.assertIn(
            "checkpoint input timed out waiting for one JSON object on stdin",
            stderr,
        )

    def test_checkpoint_open_partial_stdin_times_out(self) -> None:
        root = self.create_repository()
        returncode, _, stderr = self.run_with_open_stdin(
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
            initial_input="{",
        )

        self.assertEqual(returncode, 1)
        self.assertIn(
            "checkpoint input timed out waiting for a complete JSON object",
            stderr,
        )

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


class WorkflowPlanContractTest(GitRepositoryTest):
    def run_contract(
        self,
        root: Path,
        *,
        mode: str,
        route: str = "continue",
        spoof_mode: str | None = None,
        spoof_route: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        executable = (
            PLAN_CONTRACT_ACCEPT
            if mode == "accept"
            else PLAN_CONTRACT_CHECK[route]
        )
        payload = {
            "workspace_path": str(root),
            "plan_path": ".todo/task/plan.md",
            "work_kind": "feature",
            "plan_route": spoof_route or route,
            "plan_route_context": (
                "remaining fix bundle"
                if route == "fix_continue"
                else "not-applicable"
            ),
            "review_context": "bounded review context",
            "task_short_id": "TASK-PLAN",
        }
        if route == "fix_continue":
            payload["fix_context"] = "remaining fix bundle"
        if spoof_mode is not None:
            payload["plan_contract_mode"] = spoof_mode
        return subprocess.run(
            [str(executable)],
            cwd=root,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_checkbox_progress_does_not_change_accepted_contract(self) -> None:
        root = self.create_repository()
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")

        accepted = self.run_contract(root, mode="accept")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(
            json.loads(accepted.stdout)["transition"],
            "plan_contract_continue",
        )

        plan.write_text("# Plan\n\n- [x] Implement feature\n")
        checked = self.run_contract(root, mode="check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(
            json.loads(checked.stdout)["transition"],
            "plan_contract_continue_stable",
        )

    def test_material_plan_change_routes_to_revalidation(self) -> None:
        root = self.create_repository()
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")
        self.assertEqual(self.run_contract(root, mode="accept").returncode, 0)

        plan.write_text(
            "# Plan\n\n- [x] Implement feature\n- [ ] Change acceptance\n"
        )
        changed = self.run_contract(root, mode="check", route="verify")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        payload = json.loads(changed.stdout)
        self.assertEqual(payload["transition"], "plan_contract_verify_changed")
        self.assertEqual(payload["plan_route"], "verify")
        self.assertIn("Checkbox-only progress", payload["plan_change_report"])

    def test_writer_payload_cannot_override_check_mode(self) -> None:
        root = self.create_repository()
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")
        self.assertEqual(self.run_contract(root, mode="accept").returncode, 0)

        plan.write_text("# Plan\n\n- [ ] Changed acceptance\n")
        result = self.run_contract(
            root,
            mode="check",
            spoof_mode="accept",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["transition"],
            "plan_contract_continue_changed",
        )

    def test_writer_payload_cannot_override_check_route(self) -> None:
        root = self.create_repository()
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")
        self.assertEqual(self.run_contract(root, mode="accept").returncode, 0)

        result = self.run_contract(
            root,
            mode="check",
            route="continue",
            spoof_route="verify",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["transition"],
            "plan_contract_continue_stable",
        )

    def test_fix_continue_preserves_fix_bundle(self) -> None:
        root = self.create_repository()
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")
        self.assertEqual(
            self.run_contract(root, mode="accept", route="fix_continue").returncode,
            0,
        )

        checked = self.run_contract(
            root,
            mode="check",
            route="fix_continue",
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        payload = json.loads(checked.stdout)
        self.assertEqual(
            payload["transition"],
            "plan_contract_fix_continue_stable",
        )
        self.assertEqual(payload["fix_context"], "remaining fix bundle")


class AndroidApkInstallPreserveTest(GitRepositoryTest):
    def create_fake_tools(
        self,
        root: Path,
        *,
        installed_version: int | None,
        installed_signer: str | None = "aa",
        install_failure: str = "",
    ) -> tuple[Path, dict[str, str]]:
        tools = root / "fake-tools"
        tools.mkdir()
        log = root / "adb.log"
        adb = tools / "adb"
        adb.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            f"printf '%s\\n' \"$*\" >>{str(log)!r}\n"
            "if [[ \"$1\" == '-s' ]]; then shift 2; fi\n"
            "case \"${1:-} ${2:-}\" in\n"
            "  'get-state ') echo device ;;\n"
            + (
                "  'shell pm') echo package:/data/app/base.apk ;;\n"
                if installed_version is not None
                else "  'shell pm') exit 1 ;;\n"
            )
            + (
                f"  'shell dumpsys') echo 'versionCode={installed_version}' ;;\n"
                if installed_version is not None
                else "  'shell dumpsys') exit 1 ;;\n"
            )
            + (
                "  'pull /data/app/base.apk') cp \"$2\" \"$3\" ;;\n"
                if installed_version is not None
                else ""
            )
            + (
                f"  'install -r') echo 'Failure [INSTALL_FAILED_{install_failure}]'; exit 1 ;;\n"
                if install_failure
                else "  'install -r') echo Success ;;\n"
            )
            + "  *) exit 1 ;;\n"
            "esac\n"
        )
        apkanalyzer = tools / "apkanalyzer"
        apkanalyzer.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$2\" in\n"
            "  application-id) echo com.example.app ;;\n"
            "  version-code) echo 10 ;;\n"
            "  version-name) echo 1.0 ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        apksigner = tools / "apksigner"
        apksigner.write_text(
            "#!/usr/bin/env bash\n"
            "case \"${3:-}\" in\n"
            "  *kent-installed-*)\n"
            + (
                f"    echo 'Signer #1 certificate SHA-256 digest: {installed_signer}'\n"
                if installed_signer is not None
                else "    exit 1\n"
            )
            + "    ;;\n"
            "  *) echo 'Signer #1 certificate SHA-256 digest: aa' ;;\n"
            "esac\n"
        )
        for tool in (adb, apkanalyzer, apksigner):
            tool.chmod(0o755)
        return log, {
            "ADB": str(adb),
            "APKANALYZER": str(apkanalyzer),
            "APKSIGNER": str(apksigner),
        }

    def run_installer(
        self,
        root: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        apk = root / "candidate.apk"
        apk.write_bytes(b"test-apk")
        return subprocess.run(
            [
                str(APK_INSTALL),
                "install-preserve",
                "--serial",
                "emulator-5554",
                "--package",
                "com.example.app",
                "--apk",
                str(apk),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, **env},
        )

    def test_preservation_install_never_uninstalls_or_clears(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(root, installed_version=None)
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outcome"], "installed")
        commands = log.read_text()
        self.assertIn("install -r", commands)
        self.assertNotIn("uninstall", commands)
        self.assertNotIn(" pm clear", commands)
        self.assertNotIn(" -d", commands)

    def test_downgrade_is_classified_without_install_attempt(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(root, installed_version=11)
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["classification"], "downgrade_blocked")
        self.assertNotIn("install -r", log.read_text())

    def test_unknown_installed_signer_blocks_install_attempt(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(
            root,
            installed_version=10,
            installed_signer=None,
        )
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outcome"], "blocked")
        self.assertEqual(payload["classification"], "signer_unknown")
        self.assertNotIn("install -r", log.read_text())


class WorkflowEvidenceLedgerTest(GitRepositoryTest):
    def append(
        self,
        root: Path,
        *,
        summary: str,
        files_read: list[str],
        run_id: str | None = None,
    ) -> dict[str, object]:
        run_number = getattr(self, "_evidence_run_number", 0) + 1
        self._evidence_run_number = run_number
        resolved_run_id = run_id or f"test-run-{run_number}"
        environment = os.environ.copy()
        environment.update(
            {
                "KENT_SESSION_ID": "test-session",
                "KENT_RUN_ID": resolved_run_id,
                "KENT_STEP_ID": f"{resolved_run_id}-step",
            }
        )
        result = subprocess.run(
            [
                str(EVIDENCE),
                "append",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ],
            input=json.dumps(
                {
                    "node_key": "implement",
                    "evidence_type": "implementation",
                    "summary": summary,
                    "artifacts": [".todo/task/plan.md"],
                    "checks": ["focused test passed"],
                    "decisions": [],
                    "context": {
                        "manifest_path": ".kent/context/implement.md",
                        "files_read": files_read,
                        "model_calls": None,
                        "compaction_count": None,
                        "repeated_questions": 0,
                        "verification_loops": 1,
                    },
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_ledger_is_append_only_hash_chained_and_measures_context(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "context").mkdir()
        (root / ".kent" / "context" / "implement.md").write_text("manifest\n")
        (root / "AGENTS.md").write_text("rules\n")
        (root / ".todo" / "task").mkdir(parents=True)
        (root / ".todo" / "task" / "plan.md").write_text("plan\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "Add context")

        first = self.append(
            root,
            summary="Implemented one slice.",
            files_read=[
                "./.kent/context/implement.md",
                "AGENTS.md",
                "AGENTS.md",
            ],
        )
        second = self.append(
            root,
            summary="Verified the slice.",
            files_read=["AGENTS.md"],
        )
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)
        self.assertFalse(first["duplicate_suppressed"])
        self.assertFalse(second["duplicate_suppressed"])

        ledger = (
            root
            / ".kent"
            / "runtime"
            / "TASK-4"
            / "evidence-ledger.jsonl"
        )
        entries = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["context"]["repeated_file_count"], 1)
        self.assertEqual(
            entries[0]["context"]["files_read"],
            ["AGENTS.md", "AGENTS.md"],
        )
        self.assertEqual(
            entries[1]["previous_hash"],
            entries[0]["event_hash"],
        )

        summary = subprocess.run(
            [
                str(EVIDENCE),
                "summary",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(summary.returncode, 0, summary.stderr)
        metrics = json.loads(summary.stdout)
        self.assertEqual(metrics["entry_count"], 2)
        self.assertEqual(metrics["repeated_file_count"], 1)
        self.assertEqual(metrics["verification_loops"], 2)
        self.assertEqual(metrics["unknown_model_call_entries"], 2)

    def test_ledger_suppresses_duplicate_append_for_same_kent_run(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "context").mkdir()
        (root / ".kent" / "context" / "implement.md").write_text("manifest\n")
        (root / "AGENTS.md").write_text("rules\n")
        (root / ".todo" / "task").mkdir(parents=True)
        (root / ".todo" / "task" / "plan.md").write_text("plan\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "Add context")

        first = self.append(
            root,
            summary="Original transition evidence.",
            files_read=["AGENTS.md"],
            run_id="recovered-run",
        )
        duplicate = self.append(
            root,
            summary="Rephrased recovery evidence.",
            files_read=["AGENTS.md"],
            run_id="recovered-run",
        )

        self.assertFalse(first["duplicate_suppressed"])
        self.assertTrue(duplicate["duplicate_suppressed"])
        self.assertEqual(duplicate["sequence"], first["sequence"])
        self.assertEqual(duplicate["event_hash"], first["event_hash"])

        ledger = (
            root
            / ".kent"
            / "runtime"
            / "TASK-4"
            / "evidence-ledger.jsonl"
        )
        entries = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["summary"], "Original transition evidence.")
        self.assertEqual(entries[0]["run_id"], "recovered-run")
        self.assertEqual(entries[0]["step_id"], "recovered-run-step")

    def test_ledger_append_without_json_fails_instead_of_waiting(self) -> None:
        root = self.create_repository()
        result = subprocess.run(
            [
                str(EVIDENCE),
                "append",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ],
            input="",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=2,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "evidence input requires one JSON object on stdin",
            result.stderr,
        )

    def test_ledger_open_empty_stdin_times_out(self) -> None:
        root = self.create_repository()
        returncode, _, stderr = self.run_with_open_stdin(
            [
                str(EVIDENCE),
                "append",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ]
        )

        self.assertEqual(returncode, 1)
        self.assertIn(
            "evidence input timed out waiting for one JSON object on stdin",
            stderr,
        )

    def test_ledger_open_partial_stdin_times_out(self) -> None:
        root = self.create_repository()
        returncode, _, stderr = self.run_with_open_stdin(
            [
                str(EVIDENCE),
                "append",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ],
            initial_input="{",
        )

        self.assertEqual(returncode, 1)
        self.assertIn(
            "evidence input timed out waiting for a complete JSON object",
            stderr,
        )

    def test_ledger_detects_history_rewrite(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "context").mkdir()
        (root / ".kent" / "context" / "implement.md").write_text("manifest\n")
        (root / "AGENTS.md").write_text("rules\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "Add context")
        self.append(
            root,
            summary="Original evidence.",
            files_read=["AGENTS.md"],
        )
        ledger = (
            root
            / ".kent"
            / "runtime"
            / "TASK-4"
            / "evidence-ledger.jsonl"
        )
        entry = json.loads(ledger.read_text())
        entry["summary"] = "Rewritten evidence."
        ledger.write_text(json.dumps(entry) + "\n")

        validate = subprocess.run(
            [
                str(EVIDENCE),
                "validate",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(validate.returncode, 1)
        self.assertIn("invalid hash", validate.stderr)


class GitHubCiWatchTest(GitRepositoryTest):
    def watch(
        self,
        root: Path,
        *,
        pr_state: dict[str, object],
        checks: list[dict[str, object]],
        watch_exit: int = 0,
        prior_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        (root / "pr-state.json").write_text(json.dumps(pr_state))
        (root / "checks.json").write_text(json.dumps(checks))
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            'if [ "$1 $2" = "pr view" ]; then\n'
            '  cat "$KENT_TEST_PR_STATE"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1 $2" = "pr checks" ]; then\n'
            '  case " $* " in\n'
            '    *" --watch "*) exit "$KENT_TEST_WATCH_EXIT" ;;\n'
            "  esac\n"
            '  cat "$KENT_TEST_CHECKS"\n'
            '  exit "$KENT_TEST_WATCH_EXIT"\n'
            "fi\n"
            "exit 2\n"
        )
        fake_gh.chmod(0o755)
        workflow_input = {
            "workspace_path": str(root),
            "pr_url": "https://github.com/example/repo/pull/1",
            "branch_name": "TASK-5",
            "merge_strategy": "rebase",
        }
        if prior_report is not None:
            workflow_input["ci_report"] = json.dumps(prior_report)
        result = subprocess.run(
            [str(CI_WATCH)],
            cwd=root,
            input=json.dumps(workflow_input),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "PATH": "{}:{}".format(root, os.environ.get("PATH", "")),
                "KENT_TEST_PR_STATE": str(root / "pr-state.json"),
                "KENT_TEST_CHECKS": str(root / "checks.json"),
                "KENT_TEST_WATCH_EXIT": str(watch_exit),
                "KENT_CI_WATCH_TEST_MODE": "1",
                "KENT_CI_WATCH_INTERVAL_SECONDS": "0",
                "KENT_CI_WATCH_MAX_POLLS": "1",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_green_checks_advance_without_agent_polling(self) -> None:
        root = self.create_repository()
        result = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "mergedAt": None,
                "mergeCommit": None,
                "headRefName": "TASK-5",
                "headRefOid": "head",
                "baseRefName": "main",
                "baseRefOid": "base",
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[
                {
                    "name": "unit",
                    "workflow": "PR",
                    "bucket": "pass",
                    "state": "SUCCESS",
                    "link": "https://github.com/example/repo/actions/runs/1",
                }
            ],
        )
        self.assertEqual(result["transition"], "ci_watch_passed")
        report = json.loads(result["ci_report"])
        self.assertEqual(report["reason"], "all_checks_terminal_green")
        self.assertEqual(len(report["attempts"]), 1)

    def test_failed_checks_wake_diagnosis_agent_once(self) -> None:
        root = self.create_repository()
        result = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "headRefName": "TASK-5",
                "headRefOid": "head",
                "baseRefName": "main",
                "baseRefOid": "base",
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[
                {
                    "name": "detekt",
                    "workflow": "PR",
                    "bucket": "fail",
                    "state": "FAILURE",
                    "link": "https://github.com/example/repo/actions/runs/2",
                }
            ],
            watch_exit=1,
        )
        self.assertEqual(result["transition"], "ci_watch_failed")
        report = json.loads(result["ci_report"])
        self.assertEqual(report["reason"], "terminal_check_failure")
        self.assertEqual(len(report["attempts"]), 1)

    def test_green_retry_preserves_prior_failure_attempt(self) -> None:
        root = self.create_repository()
        pr_state = {
            "state": "OPEN",
            "headRefName": "TASK-5",
            "headRefOid": "head",
            "baseRefName": "main",
            "baseRefOid": "base",
            "url": "https://github.com/example/repo/pull/1",
        }
        failed = self.watch(
            root,
            pr_state=pr_state,
            checks=[
                {
                    "name": "unit",
                    "workflow": "PR",
                    "bucket": "fail",
                    "state": "FAILURE",
                    "link": "https://github.com/example/repo/actions/runs/2",
                }
            ],
            watch_exit=1,
        )
        failed_report = json.loads(failed["ci_report"])
        failed_report["failure_fingerprint"] = "unit:assertion"
        failed_report["retry_job_id"] = 42

        passed = self.watch(
            root,
            pr_state=pr_state,
            checks=[
                {
                    "name": "unit",
                    "workflow": "PR",
                    "bucket": "pass",
                    "state": "SUCCESS",
                    "link": "https://github.com/example/repo/actions/runs/3",
                }
            ],
            prior_report=failed_report,
        )

        report = json.loads(passed["ci_report"])
        self.assertEqual(report["reason"], "all_checks_terminal_green")
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(
            report["attempts"][0]["failure_fingerprint"],
            "unit:assertion",
        )
        self.assertEqual(report["attempts"][0]["retry_job_id"], 42)
        self.assertEqual(
            report["attempts"][1]["reason"],
            "all_checks_terminal_green",
        )

    def test_merged_pr_skips_obsolete_ci_diagnosis(self) -> None:
        root = self.create_repository()
        result = self.watch(
            root,
            pr_state={
                "state": "MERGED",
                "mergedAt": "2026-08-05T00:00:00Z",
                "mergeCommit": {"oid": "merged"},
                "headRefName": "TASK-5",
                "headRefOid": "head",
                "baseRefName": "main",
                "baseRefOid": "base",
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[],
        )
        self.assertEqual(result["transition"], "ci_watch_pr_merged")
        self.assertIn("merged", result["merge_report"])


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
        self.assertEqual(result["transition"], "merge_watch_pr_merged")
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
        self.assertEqual(result["transition"], "merge_watch_state_changed")
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
        self.assertEqual(result["transition"], "merge_watch_still_waiting")

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
        self.assertEqual(result["transition"], "merge_watch_state_changed")
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
        self.assertEqual(payload["transition"], "merge_watch_state_changed")
        self.assertIn("exceeded 1 seconds", payload["pr_report"])

    def test_invalid_configured_github_cli_fails_clearly(self) -> None:
        root = self.create_repository()
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
                "KENT_GH_BIN": str(root / "missing-gh"),
                "KENT_PR_WATCH_TEST_MODE": "1",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "configured GitHub CLI is not executable",
            result.stderr,
        )


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

    def completed_wrapper(self, root: Path) -> Path:
        wrapper = root / "kent-worktree"
        wrapper.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "if [ \"$1\" = status ]; then\n"
            "  python3 -c 'import json, os; root = "
            "os.environ[\"KENT_TEST_PRIMARY\"]; print(json.dumps({"
            "\"target\": {\"EffectiveWorkdir\": root}, "
            "\"worktree\": {\"recorded_root\": root, "
            "\"observed_root\": root}, \"problems\": None}))'\n"
            "  exit 0\n"
            "fi\n"
            "if [ -n \"${KENT_TEST_WRAPPER_LOG:-}\" ]; then\n"
            "  python3 -c 'import json, os, sys; "
            "open(os.environ[\"KENT_TEST_WRAPPER_LOG\"], \"w\").write("
            "json.dumps(sys.argv[1:]))' \"$@\"\n"
            "fi\n"
            "if [ -n \"${KENT_TEST_WRAPPER_MARKER:-}\" ]; then\n"
            "  touch \"$KENT_TEST_WRAPPER_MARKER\"\n"
            "fi\n"
            "for argument in \"$@\"; do target=\"$argument\"; done\n"
            "git -C \"$KENT_TEST_PRIMARY\" worktree remove --force \"$target\"\n"
            "if [ -n \"${KENT_TEST_NEW_OID:-}\" ]; then\n"
            "  git -C \"$KENT_TEST_PRIMARY\" update-ref "
            "refs/heads/TASK-1 \"$KENT_TEST_NEW_OID\"\n"
            "fi\n"
            "printf '%s\\n' "
            "'{\"kind\":\"completed\",\"completed\":{\"cleanup\":"
            "{\"kind\":\"retained\"}}}'\n"
        )
        wrapper.chmod(0o755)
        return wrapper

    def test_primary_checkout_is_never_deleted(self) -> None:
        root = self.create_repository()
        runtime = root / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        (runtime / "fix-checkpoint.json").write_text("{}")
        (runtime / "evidence-ledger.jsonl").write_text("{}\n")
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
        self.assertEqual(payload["transition"], "task_janitor_done")
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
        self.assertEqual(payload["transition"], "task_janitor_done")
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
        self.assertEqual(payload["transition"], "task_janitor_done")
        self.assertIn("preserved dirty worktree", payload["cleanup_report"])
        self.assertTrue(worktree.exists())

    def test_missing_branch_sentinel_returns_to_cleanup(self) -> None:
        root = self.create_repository()
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))

        result = subprocess.run(
            [str(JANITOR)],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="null",
                cleanup_mode="no_pr",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("missing branch identity", payload["cleanup_report"])
        self.assertIn("exact current Git branch", payload["blocker_reason"])
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
        fake_wrapper = self.completed_wrapper(root)

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
                "KENT_TEST_PRIMARY": str(root),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_done")
        wrapper_args = json.loads(wrapper_log.read_text())
        self.assertEqual(wrapper_args[0:3], ["delete", "--session", "session-test"])
        self.assertIn("--delete-branch", wrapper_args)
        self.assertIn(str(worktree.resolve()), wrapper_args)

    def test_merged_pr_lookup_retries_before_cleanup(self) -> None:
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
        attempts = root / "gh-attempts"
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "attempt=$(cat \"$KENT_TEST_GH_ATTEMPTS\" 2>/dev/null || echo 0)\n"
            "attempt=$((attempt + 1))\n"
            "echo \"$attempt\" > \"$KENT_TEST_GH_ATTEMPTS\"\n"
            "if [ \"$attempt\" -lt 3 ]; then\n"
            "  echo 'temporary GitHub failure' >&2\n"
            "  exit 1\n"
            "fi\n"
            "cat \"$KENT_TEST_PR_STATE\"\n"
        )
        fake_gh.chmod(0o755)
        wrapper_marker = root / "wrapper-called"
        fake_wrapper = self.completed_wrapper(root)

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
                "KENT_JANITOR_GH_RETRY_DELAY_SECONDS": "0",
                "KENT_TEST_GH_ATTEMPTS": str(attempts),
                "KENT_TEST_PR_STATE": str(pr_state),
                "KENT_WORKTREE_WRAPPER": str(fake_wrapper),
                "KENT_TEST_WRAPPER_MARKER": str(wrapper_marker),
                "KENT_TEST_PRIMARY": str(root),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_done")
        self.assertEqual(attempts.read_text().strip(), "3")
        self.assertTrue(wrapper_marker.exists())

    def test_merged_pr_lookup_failure_returns_to_cleanup(self) -> None:
        root = self.create_repository()
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))

        attempts = root / "gh-attempts"
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "attempt=$(cat \"$KENT_TEST_GH_ATTEMPTS\" 2>/dev/null || echo 0)\n"
            "attempt=$((attempt + 1))\n"
            "echo \"$attempt\" > \"$KENT_TEST_GH_ATTEMPTS\"\n"
            "echo 'temporary GitHub failure' >&2\n"
            "exit 1\n"
        )
        fake_gh.chmod(0o755)
        wrapper_marker = root / "wrapper-called"
        fake_wrapper = root / "kent-worktree"
        fake_wrapper.write_text(
            "#!/bin/sh\n"
            "touch \"$KENT_TEST_WRAPPER_MARKER\"\n"
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
                "KENT_JANITOR_GH_RETRY_DELAY_SECONDS": "0",
                "KENT_TEST_GH_ATTEMPTS": str(attempts),
                "KENT_WORKTREE_WRAPPER": str(fake_wrapper),
                "KENT_TEST_WRAPPER_MARKER": str(wrapper_marker),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("failed after 3 attempts", payload["cleanup_report"])
        self.assertIn("bounded retries", payload["blocker_reason"])
        self.assertEqual(attempts.read_text().strip(), "3")
        self.assertFalse(wrapper_marker.exists())
        self.assertTrue(worktree.exists())

    def test_cleanup_session_must_leave_task_worktree(self) -> None:
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
        wrapper_marker = root / "wrapper-called"
        fake_wrapper = root / "kent-worktree"
        fake_wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then\n"
            "  python3 -c 'import json, os; root = "
            "os.environ[\"KENT_TEST_WORKTREE\"]; print(json.dumps({"
            "\"target\": {\"EffectiveWorkdir\": root}, "
            "\"worktree\": {\"recorded_root\": root, "
            "\"observed_root\": root}, \"problems\": None}))'\n"
            "  exit 0\n"
            "fi\n"
            "touch \"$KENT_TEST_WRAPPER_MARKER\"\n"
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
                "KENT_TEST_WORKTREE": str(worktree),
                "KENT_TEST_WRAPPER_MARKER": str(wrapper_marker),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("still targets the task worktree", payload["cleanup_report"])
        self.assertIn("kent worktree leave", payload["blocker_reason"])
        self.assertFalse(wrapper_marker.exists())
        self.assertTrue(worktree.exists())

    def test_scheduled_delete_is_not_reported_as_completed(self) -> None:
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
        fake_wrapper = root / "kent-worktree"
        fake_wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then\n"
            "  python3 -c 'import json, os; root = "
            "os.environ[\"KENT_TEST_PRIMARY\"]; print(json.dumps({"
            "\"target\": {\"EffectiveWorkdir\": root}, "
            "\"worktree\": {\"recorded_root\": root, "
            "\"observed_root\": root}, \"problems\": None}))'\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s\\n' "
            "'{\"kind\":\"scheduled\",\"scheduled\":"
            "{\"operation_id\":\"operation-test\"}}'\n"
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
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("operation-test", payload["cleanup_report"])
        self.assertIn("did not confirm completion", payload["cleanup_report"])
        self.assertTrue(worktree.exists())

    def test_completed_delete_requires_absent_worktree(self) -> None:
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
        fake_wrapper = root / "kent-worktree"
        fake_wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then\n"
            "  python3 -c 'import json, os; root = "
            "os.environ[\"KENT_TEST_PRIMARY\"]; print(json.dumps({"
            "\"target\": {\"EffectiveWorkdir\": root}, "
            "\"worktree\": {\"recorded_root\": root, "
            "\"observed_root\": root}, \"problems\": None}))'\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s\\n' "
            "'{\"kind\":\"completed\",\"completed\":"
            "{\"cleanup\":{\"kind\":\"retained\"}}}'\n"
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
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("postconditions failed", payload["blocker_reason"])
        self.assertTrue(worktree.exists())

    def test_merged_pr_descendant_invokes_kent_worktree_deletion(self) -> None:
        root = self.create_repository()
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        local_head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        (root / "remote-only.txt").write_text("user update\n")
        self.run_git(root, "add", "remote-only.txt")
        self.run_git(root, "commit", "-q", "-m", "User update")
        pr_head = self.run_git(root, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            self.run_git(
                root,
                "merge-base",
                "--is-ancestor",
                local_head,
                pr_head,
            ).returncode,
            0,
        )

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": pr_head,
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
        wrapper_marker = root / "wrapper-called"
        fake_wrapper = self.completed_wrapper(root)

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
                "KENT_TEST_WRAPPER_MARKER": str(wrapper_marker),
                "KENT_TEST_PRIMARY": str(root),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_done")
        self.assertTrue(wrapper_marker.exists())

    def test_diverged_merged_pr_head_is_preserved(self) -> None:
        root = self.create_repository()
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        (worktree / "local-only.txt").write_text("local\n")
        self.run_git(worktree, "add", "local-only.txt")
        self.run_git(worktree, "commit", "-q", "-m", "Local only")
        (root / "remote-only.txt").write_text("remote\n")
        self.run_git(root, "add", "remote-only.txt")
        self.run_git(root, "commit", "-q", "-m", "Remote only")
        pr_head = self.run_git(root, "rev-parse", "HEAD").stdout.strip()

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": pr_head,
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
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
        self.assertIn("not conclusively recoverable", payload["cleanup_report"])
        self.assertFalse(wrapper_marker.exists())
        self.assertTrue(worktree.exists())

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
        fake_wrapper = self.completed_wrapper(root)

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

    def test_remote_descendant_branch_deletion_uses_pr_head_lease(self) -> None:
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
        (root / "remote-only.txt").write_text("user update\n")
        self.run_git(root, "add", "remote-only.txt")
        self.run_git(root, "commit", "-q", "-m", "User update")
        pr_head = self.run_git(root, "rev-parse", "HEAD").stdout.strip()
        self.run_git(
            root,
            "push",
            "-q",
            "origin",
            f"{pr_head}:refs/heads/TASK-1",
        )

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": pr_head,
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
        fake_wrapper = self.completed_wrapper(root)

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
        fake_wrapper = self.completed_wrapper(root)

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
