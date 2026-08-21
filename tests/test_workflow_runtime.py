from __future__ import annotations

import json
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = REPO_ROOT / "templates" / "project" / "workflow-checkpoint"
EVIDENCE = REPO_ROOT / "templates" / "project" / "workflow-evidence-ledger"
CI_WATCH = REPO_ROOT / "templates" / "project" / "workflow-wait-github-ci"
PR_WATCH = REPO_ROOT / "templates" / "project" / "workflow-wait-github-pr"
JANITOR = REPO_ROOT / "templates" / "project" / "workflow-task-janitor"
VERIFY_REPORT = REPO_ROOT / "templates" / "project" / "workflow-verify-report"
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


def load_template_module(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_pids_gone(testcase: unittest.TestCase, pid_file: Path) -> None:
    pids = [int(value) for value in pid_file.read_text().split()]
    deadline = time.monotonic() + 2
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                alive.append(pid)
            else:
                alive.append(pid)
        if alive:
            time.sleep(0.02)
    testcase.assertFalse(alive, "processes remained alive: {}".format(alive))


class RuntimeSupportImportTest(unittest.TestCase):
    def test_v2_commands_load_only_the_sibling_support_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            support = root / "workflow_runtime_contracts.py"
            shutil.copyfile(REPO_ROOT / "workflowkit" / "runtime.py", support)
            fake_package = root / "workflowkit"
            fake_package.mkdir()
            (fake_package / "__init__.py").write_text(
                "raise RuntimeError('installed workflowkit was imported')\n"
            )
            for name, function in (
                ("workflow-evidence-ledger", "_runtime_module"),
                ("workflow-task-janitor", "runtime_contracts"),
                ("workflow-verify-report", "runtime_contracts"),
                ("workflow-wait-github-ci", "runtime_contracts"),
            ):
                script = root / f"{name}.py"
                shutil.copyfile(REPO_ROOT / "templates" / "project" / name, script)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import importlib.util, pathlib, sys\n"
                            "path = pathlib.Path(sys.argv[1])\n"
                            "spec = importlib.util.spec_from_file_location('cmd', path)\n"
                            "module = importlib.util.module_from_spec(spec)\n"
                            "spec.loader.exec_module(module)\n"
                            f"loaded = module.{function}()\n"
                            "assert pathlib.Path(loaded.__file__).resolve() == "
                            "pathlib.Path(sys.argv[2]).resolve()\n"
                        ),
                        str(script),
                        str(support),
                    ],
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "PYTHONPATH": str(root),
                    },
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            for name in (
                "workflow-evidence-ledger",
                "workflow-task-janitor",
                "workflow-verify-report",
                "workflow-wait-github-ci",
                "workflow-wait-github-pr",
            ):
                text = (
                    REPO_ROOT / "templates" / "project" / name
                ).read_text()
                self.assertNotIn("from workflowkit", text)


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
        plan.write_text(
            "# Plan\n\n"
            "### [ ] Step 1: Implement feature\n\n"
            "- [ ] Verify feature\n"
        )

        accepted = self.run_contract(root, mode="accept")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(
            json.loads(accepted.stdout)["transition"],
            "plan_contract_continue",
        )

        plan.write_text(
            "# Plan\n\n"
            "### [x] Step 1: Implement feature\n\n"
            "- [x] Verify feature\n"
        )
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
    def install_v2_runtime_commands(self, root: Path) -> Path:
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        for name in (
            "workflow-evidence-ledger",
            "workflow-task-janitor",
        ):
            target = scripts / name
            shutil.copyfile(REPO_ROOT / "templates" / "project" / name, target)
            target.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        return scripts

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

    def test_v2_seal_is_idempotent_and_janitor_retains_terminal_sentinel(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "context").mkdir()
        (root / ".kent" / "context" / "implement.md").write_text("manifest\n")
        (root / "AGENTS.md").write_text("rules\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "Add context")
        scripts = self.install_v2_runtime_commands(root)
        evidence = scripts / "workflow-evidence-ledger"
        payload = {
            "node_key": "implement",
            "evidence_type": "implementation",
            "summary": "sealed slice",
            "artifacts": [],
            "checks": [],
            "decisions": [],
            "context": {
                "manifest_path": ".kent/context/implement.md",
                "files_read": ["AGENTS.md"],
            },
        }
        appended = subprocess.run(
            [
                str(evidence),
                "append",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(appended.returncode, 0, appended.stderr)
        request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {
                "status": "passed",
                "report_sha256": "a" * 64,
            },
            "retention_class": "cleanup_report_only",
        }
        first = subprocess.run(
            [
                str(evidence),
                "seal",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        second = subprocess.run(
            [
                str(evidence),
                "seal",
                "--task",
                "TASK-4",
                "--workspace",
                str(root),
            ],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            json.loads(first.stdout)["terminal_marker"],
            json.loads(second.stdout)["terminal_marker"],
        )

        janitor = scripts / "workflow-task-janitor"
        janitored = subprocess.run(
            [str(janitor)],
            input=json.dumps(
                {
                    "workspace_path": str(root),
                    "task_short_id": "TASK-4",
                    "branch_name": "",
                    "pr_url": "",
                    "merge_report": "",
                    "cleanup_mode": "report_only",
                    "cleanup_session_id": "test-session",
                    "cleanup_report": json.loads(first.stdout)["terminal_marker"],
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(janitored.returncode, 0, janitored.stderr)
        self.assertEqual(
            json.loads(janitored.stdout)["transition"],
            "task_janitor_done",
        )
        runtime = root / ".kent" / "runtime"
        self.assertFalse((runtime / "TASK-4").exists())
        digest = hashlib.sha256(b"TASK-4").hexdigest()
        self.assertEqual(
            sorted(path.name for path in runtime.iterdir()),
            [f".evidence-lock-{digest}", f".evidence-terminal-{digest}"],
        )

    def test_janitor_never_deletes_exact_tombstone_without_marker(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "context").mkdir()
        (root / ".kent" / "context" / "implement.md").write_text("manifest\n")
        (root / "AGENTS.md").write_text("rules\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "Add context")
        scripts = self.install_v2_runtime_commands(root)
        evidence = scripts / "workflow-evidence-ledger"
        self.append(root, summary="sealed", files_read=["AGENTS.md"])
        request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": "a" * 64},
            "retention_class": "cleanup_report_only",
        }
        sealed = subprocess.run(
            [str(evidence), "seal", "--task", "TASK-4", "--workspace", str(root)],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        marker_line = json.loads(sealed.stdout)["terminal_marker"]
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime_dir = root / ".kent" / "runtime"
        active = runtime_dir / "TASK-4"
        digest = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_for_tombstone_test",
        ).canonical_sha256(marker)
        tombstone = runtime_dir / f".evidence-cleanup-{digest}"
        active.rename(tombstone)
        janitor = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=json.dumps(
                {
                    "workspace_path": str(root),
                    "task_short_id": "TASK-4",
                    "branch_name": "",
                    "pr_url": "",
                    "merge_report": "",
                    "cleanup_mode": "report_only",
                    "cleanup_session_id": "test-session",
                    "cleanup_report": "preflight",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(janitor.returncode, 0, janitor.stderr)
        self.assertTrue(tombstone.exists())
        self.assertTrue((tombstone / "evidence-ledger.jsonl").exists())
        self.assertFalse(
            (runtime_dir / f".evidence-terminal-{hashlib.sha256(b'TASK-4').hexdigest()}").exists()
        )

        janitor = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=json.dumps(
                {
                    "workspace_path": str(root),
                    "task_short_id": "TASK-4",
                    "branch_name": "",
                    "pr_url": "",
                    "merge_report": "",
                    "cleanup_mode": "report_only",
                    "cleanup_session_id": "test-session",
                    "cleanup_report": marker_line,
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(janitor.returncode, 0, janitor.stderr)
        self.assertFalse(tombstone.exists())
        self.assertTrue(
            (runtime_dir / f".evidence-terminal-{hashlib.sha256(b'TASK-4').hexdigest()}").exists()
        )


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

    def test_append_rejects_runtime_replacement_after_admission(self) -> None:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        module = load_template_module(EVIDENCE, "evidence_runtime_replace_test")
        payload = {
            "node_key": "implement",
            "evidence_type": "implementation",
            "summary": "replacement",
            "artifacts": [],
            "checks": [],
            "decisions": [],
            "context": {
                "manifest_path": ".kent/context/implement.md",
                "files_read": [],
            },
        }
        module.read_json_input = lambda _label: payload
        runtime_dir = root / ".kent" / "runtime"
        outside = root / "runtime-preserved"
        preserved = None
        replacement = None

        def replace_runtime(phase: str) -> None:
            if phase != "after_runtime_fd_before_lock_open":
                return
            (runtime_dir / "outside.txt").write_text("preserve\n")
            runtime_dir.rename(outside)
            runtime_dir.mkdir(mode=0o700)
            (runtime_dir / "replacement.txt").write_text("replacement\n")

        try:
            with self.assertRaises(ValueError):
                module.append_entry(
                    root.resolve(),
                    runtime_dir / "TASK-4" / "evidence-ledger.jsonl",
                    task="TASK-4",
                    _phase_hook=replace_runtime,
                )
        finally:
            preserved = (outside / "outside.txt").read_text()
            replacement = (runtime_dir / "replacement.txt").read_text()
            if runtime_dir.exists() and outside.exists():
                for child in runtime_dir.iterdir():
                    child.unlink()
                runtime_dir.rmdir()
                outside.rename(runtime_dir)
        self.assertEqual(preserved, "preserve\n")
        self.assertEqual(replacement, "replacement\n")

    def test_append_rejects_lock_tombstone_conflict_without_task_creation(self) -> None:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        module = load_template_module(EVIDENCE, "evidence_lock_conflict_test")
        module.read_json_input = lambda _label: {
            "node_key": "implement",
            "evidence_type": "implementation",
            "summary": "conflict",
            "artifacts": [],
            "checks": [],
            "decisions": [],
            "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
        }
        tombstone = root / ".kent" / "runtime" / ".evidence-cleanup-conflict"

        def create_conflict(phase: str) -> None:
            if phase == "after_lock_before_state_read":
                tombstone.mkdir(mode=0o700)
                (tombstone / "outside.txt").write_text("preserve\n")

        with self.assertRaises(ValueError):
            module.append_entry(
                root.resolve(),
                root / ".kent" / "runtime" / "TASK-4" / "evidence-ledger.jsonl",
                task="TASK-4",
                _phase_hook=create_conflict,
            )
        self.assertFalse((root / ".kent" / "runtime" / "TASK-4").exists())
        self.assertEqual((tombstone / "outside.txt").read_text(), "preserve\n")

    def test_append_rejects_task_detach_and_replacement(self) -> None:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        self.append(root, summary="seed", files_read=[])
        module = load_template_module(EVIDENCE, "evidence_task_replace_test")
        module.read_json_input = lambda _label: {
            "node_key": "implement",
            "evidence_type": "implementation",
            "summary": "replacement",
            "artifacts": [],
            "checks": [],
            "decisions": [],
            "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
        }
        task = root / ".kent" / "runtime" / "TASK-4"
        detached = root / "task-preserved"

        def replace_task(phase: str) -> None:
            if phase == "after_task_fd_before_link_revalidation":
                task.rename(detached)
                task.mkdir(mode=0o700)
                (task / "replacement.txt").write_text("replacement\n")

        with self.assertRaises(ValueError):
            module.append_entry(
                root.resolve(),
                task / "evidence-ledger.jsonl",
                task="TASK-4",
                _phase_hook=replace_task,
            )
        self.assertEqual(
            (detached / "evidence-ledger.jsonl").read_text().count("\n"),
            1,
        )
        self.assertEqual(
            (task / "replacement.txt").read_text(),
            "replacement\n",
        )
        (task / "replacement.txt").unlink()
        task.rmdir()
        detached.rename(task)

    def test_seal_waits_for_paused_writer_and_rereads_complete_chain(self) -> None:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        scripts = self.install_v2_runtime_commands(root)
        writer = load_template_module(
            scripts / "workflow-evidence-ledger",
            "evidence_paused_writer_test",
        )
        sealer = load_template_module(
            scripts / "workflow-evidence-ledger",
            "evidence_paused_sealer_test",
        )
        payload = {
            "node_key": "implement",
            "evidence_type": "implementation",
            "summary": "writer",
            "artifacts": [],
            "checks": [],
            "decisions": [],
            "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
        }
        request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": "a" * 64},
            "retention_class": "cleanup_report_only",
        }
        writer.read_json_input = lambda _label: payload
        sealer.read_json_input = lambda _label: request
        writer_locked = threading.Event()
        sealer_admitted = threading.Event()
        sealer_flock_attempted = threading.Event()
        release_writer = threading.Event()
        errors: list[BaseException] = []
        result: dict[str, object] = {}
        original_sealer_fcntl = sealer.fcntl

        def sealer_flock(fd: int, operation: int) -> None:
            sealer_flock_attempted.set()
            original_sealer_fcntl.flock(fd, operation)

        sealer.fcntl = types.SimpleNamespace(
            LOCK_EX=original_sealer_fcntl.LOCK_EX,
            LOCK_SH=original_sealer_fcntl.LOCK_SH,
            flock=sealer_flock,
        )

        def writer_hook(phase: str) -> None:
            if phase == "after_lock_before_state_read":
                writer_locked.set()
                release_writer.wait(5)

        def seal_hook(phase: str) -> None:
            if phase == "after_runtime_fd_before_lock_open":
                sealer_admitted.set()

        def run_writer() -> None:
            try:
                writer.append_entry(
                    root.resolve(),
                    root / ".kent/runtime/TASK-4/evidence-ledger.jsonl",
                    task="TASK-4",
                    _phase_hook=writer_hook,
                )
            except BaseException as error:
                errors.append(error)

        def run_sealer() -> None:
            try:
                result.update(
                    sealer.seal_terminal(
                        root.resolve(),
                        task="TASK-4",
                        _phase_hook=seal_hook,
                    )
                )
            except BaseException as error:
                errors.append(error)

        writer_thread = threading.Thread(target=run_writer)
        seal_thread = threading.Thread(target=run_sealer)
        writer_thread.start()
        if not writer_locked.wait(5):
            self.fail("writer did not acquire lock: {!r}".format(errors))
        seal_thread.start()
        self.assertTrue(sealer_admitted.wait(5))
        self.assertTrue(sealer_flock_attempted.wait(5))
        self.assertTrue(seal_thread.is_alive())
        ledger = root / ".kent/runtime/TASK-4/evidence-ledger.jsonl"
        self.assertFalse(ledger.exists())
        self.assertFalse(result)
        release_writer.set()
        writer_thread.join(5)
        seal_thread.join(5)
        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(seal_thread.is_alive())
        self.assertEqual(
            [json.loads(line)["sequence"] for line in ledger.read_text().splitlines()],
            [1, 2],
        )
        self.assertEqual(errors, [])
        self.assertTrue(result["terminal_marker"])
        records = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual([record["sequence"] for record in records], [1, 2])
        self.assertEqual(records[-1]["record_kind"], "terminal_evidence_seal_v1")
        sealed_bytes = ledger.read_bytes()
        sealer.read_json_input = lambda _label: request
        repeated = sealer.seal_terminal(root.resolve(), task="TASK-4")
        self.assertEqual(ledger.read_bytes(), sealed_bytes)
        self.assertEqual(repeated["terminal_marker"], result["terminal_marker"])
        writer.read_json_input = lambda _label: payload
        with self.assertRaises(ValueError):
            writer.append_entry(
                root.resolve(),
                ledger,
                task="TASK-4",
            )

    def test_append_rejects_terminal_sentinel_after_seal(self) -> None:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        scripts = self.install_v2_runtime_commands(root)
        evidence = scripts / "workflow-evidence-ledger"
        self.append(root, summary="sealed", files_read=[])
        seal = subprocess.run(
            [str(evidence), "seal", "--task", "TASK-4", "--workspace", str(root)],
            input=json.dumps(
                {
                    "schema": "terminal-evidence-seal-request-v1",
                    "operation_report_digests": [],
                    "redaction": {"status": "passed", "report_sha256": "a" * 64},
                    "retention_class": "cleanup_report_only",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(seal.returncode, 0, seal.stderr)
        sentinel = root / (
            ".kent/runtime/.evidence-terminal-"
            + hashlib.sha256(b"TASK-4").hexdigest()
        )
        sentinel.write_bytes(b"")
        append = subprocess.run(
            [str(evidence), "append", "--task", "TASK-4", "--workspace", str(root)],
            input=json.dumps(
                {
                    "node_key": "implement",
                    "evidence_type": "implementation",
                    "summary": "late",
                    "artifacts": [],
                    "checks": [],
                    "decisions": [],
                    "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(append.returncode, 1)
        self.assertTrue(sentinel.exists())

    def test_seal_rejects_changed_request_and_corrupt_chain(self) -> None:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        scripts = self.install_v2_runtime_commands(root)
        evidence = scripts / "workflow-evidence-ledger"
        self.append(root, summary="sealed", files_read=[])
        request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": "a" * 64},
            "retention_class": "cleanup_report_only",
        }
        seal = subprocess.run(
            [str(evidence), "seal", "--task", "TASK-4", "--workspace", str(root)],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(seal.returncode, 0, seal.stderr)
        changed = dict(request)
        changed["redaction"] = {"status": "passed", "report_sha256": "b" * 64}
        retry = subprocess.run(
            [str(evidence), "seal", "--task", "TASK-4", "--workspace", str(root)],
            input=json.dumps(changed),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(retry.returncode, 1)
        ledger = root / ".kent/runtime/TASK-4/evidence-ledger.jsonl"
        lines = ledger.read_text().splitlines()
        ledger.write_text(lines[0] + "\n" + "{\n")
        corrupt = subprocess.run(
            [str(evidence), "seal", "--task", "TASK-4", "--workspace", str(root)],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(corrupt.returncode, 1)

    def test_seal_rejects_terminal_sentinel_after_seal_without_byte_change(self) -> None:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        scripts = self.install_v2_runtime_commands(root)
        evidence = scripts / "workflow-evidence-ledger"
        self.append(root, summary="sealed", files_read=[])
        request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": "a" * 64},
            "retention_class": "cleanup_report_only",
        }
        first = subprocess.run(
            [str(evidence), "seal", "--task", "TASK-4", "--workspace", str(root)],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        runtime_dir = root / ".kent" / "runtime"
        sentinel = runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-4").hexdigest()
        )
        sentinel.write_bytes(b"")
        ledger = runtime_dir / "TASK-4" / "evidence-ledger.jsonl"
        before = ledger.read_bytes()
        repeated = subprocess.run(
            [str(evidence), "seal", "--task", "TASK-4", "--workspace", str(root)],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(repeated.returncode, 1)
        self.assertEqual(ledger.read_bytes(), before)

    def _seal_with_write_failure(self, *, complete_partial: bool) -> bytes:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        scripts = self.install_v2_runtime_commands(root)
        self.append(root, summary="partial seal", files_read=[])
        module = load_template_module(
            scripts / "workflow-evidence-ledger",
            "evidence_partial_seal_test",
        )
        request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": "a" * 64},
            "retention_class": "cleanup_report_only",
        }
        module.read_json_input = lambda _label: request
        original_write = module.os.write
        calls = 0

        def injected(fd: int, data) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                written = original_write(fd, data[: max(1, len(data) // 2)])
                if complete_partial:
                    return written
                return written
            if not complete_partial and calls == 2:
                raise OSError("partial seal write injected")
            return original_write(fd, data)

        module.os.write = injected
        try:
            if complete_partial:
                module.seal_terminal(root.resolve(), task="TASK-4")
            else:
                with self.assertRaises(OSError):
                    module.seal_terminal(root.resolve(), task="TASK-4")
        finally:
            module.os.write = original_write
        ledger = root / ".kent" / "runtime" / "TASK-4" / "evidence-ledger.jsonl"
        return ledger.read_bytes()

    def test_seal_partial_write_completion_preserves_valid_chain(self) -> None:
        data = self._seal_with_write_failure(complete_partial=True)
        records = [json.loads(line) for line in data.splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["record_kind"], "terminal_evidence_seal_v1")

    def test_seal_partial_write_error_rolls_back_to_original_chain(self) -> None:
        data = self._seal_with_write_failure(complete_partial=False)
        records = [json.loads(line) for line in data.splitlines()]
        self.assertEqual(len(records), 1)
        self.assertNotIn("terminal_evidence_seal_v1", data.decode())

    def test_append_task_creation_fsync_failure_preserves_empty_task(self) -> None:
        root = self.create_repository()
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        scripts = self.install_v2_runtime_commands(root)
        module = load_template_module(
            scripts / "workflow-evidence-ledger",
            "evidence_fsync_task_creation",
        )
        module.read_json_input = lambda _label: {
            "node_key": "implement",
            "evidence_type": "implementation",
            "summary": "task creation",
            "artifacts": [],
            "checks": [],
            "decisions": [],
            "context": {
                "manifest_path": ".kent/context/implement.md",
                "files_read": [],
            },
        }
        runtime_dir = root / ".kent" / "runtime"
        runtime_dir.mkdir()
        original_fsync = module.os.fsync
        runtime_inode = runtime_dir.stat().st_ino
        task = runtime_dir / "TASK-4"
        fired = False

        def injected(fd: int) -> None:
            nonlocal fired
            if (
                not fired
                and module.os.fstat(fd).st_ino == runtime_inode
                and task.exists()
            ):
                fired = True
                raise OSError("task creation fsync injected")
            original_fsync(fd)

        module.os.fsync = injected
        try:
            with self.assertRaises(OSError):
                module.append_entry(
                    root.resolve(),
                    runtime_dir / "TASK-4" / "evidence-ledger.jsonl",
                    task="TASK-4",
                )
        finally:
            module.os.fsync = original_fsync
        self.assertTrue(fired)
        self.assertTrue((runtime_dir / "TASK-4").is_dir())
        self.assertFalse(
            (runtime_dir / "TASK-4" / "evidence-ledger.jsonl").exists()
        )

    def test_seal_rejects_unsafe_mode_and_hard_link_without_byte_change(self) -> None:
        for mode, hard_link in ((0o644, False), (0o600, True)):
            with self.subTest(mode=oct(mode), hard_link=hard_link):
                root = self.create_repository()
                context = root / ".kent" / "context"
                context.mkdir(parents=True)
                (context / "implement.md").write_text("manifest\n")
                self.run_git(root, "add", ".")
                self.run_git(root, "commit", "-q", "-m", "context")
                scripts = self.install_v2_runtime_commands(root)
                evidence = scripts / "workflow-evidence-ledger"
                self.append(root, summary="unsafe seal", files_read=[])
                ledger = root / ".kent/runtime/TASK-4/evidence-ledger.jsonl"
                before = ledger.read_bytes()
                ledger.chmod(mode)
                link = root / ".ledger-hard-link"
                if hard_link:
                    os.link(ledger, link)
                try:
                    result = subprocess.run(
                        [
                            str(evidence),
                            "seal",
                            "--task",
                            "TASK-4",
                            "--workspace",
                            str(root),
                        ],
                        input=json.dumps(
                            {
                                "schema": "terminal-evidence-seal-request-v1",
                                "operation_report_digests": [],
                                "redaction": {
                                    "status": "passed",
                                    "report_sha256": "a" * 64,
                                },
                                "retention_class": "cleanup_report_only",
                            }
                        ),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                finally:
                    if link.exists():
                        link.unlink()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(ledger.read_bytes(), before)


class WorkflowVerifyReportTest(GitRepositoryTest):
    def test_repository_verify_report_template_is_executable(self) -> None:
        self.assertEqual(VERIFY_REPORT.stat().st_mode & 0o777, 0o755)

    def install_verify_fixture(
        self,
        root: Path,
        script: str,
        *,
        ignored: bool = True,
    ) -> Path:
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        verifier = scripts / "workflow-compile-verify"
        verifier.write_text(script)
        verifier.chmod(0o755)
        wrapper = scripts / "workflow-verify-report"
        shutil.copyfile(VERIFY_REPORT, wrapper)
        wrapper.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        if ignored:
            with (root / ".gitignore").open("a") as stream:
                stream.write("/build/kent-workflow/\n")
        return scripts

    def run_verify_command(
        self,
        root: Path,
        *,
        workspace: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> dict[str, object]:
        scripts = root / ".kent" / "scripts"
        result = subprocess.run(
            [sys.executable, str(scripts / "workflow-verify-report")],
            cwd=root,
            input=json.dumps(
                {"workspace_path": workspace or str(root)}
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def parse_verification_report(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        report = json.loads(str(payload["verification_report"]))
        self.assertEqual(
            set(report),
            {"schema", "code", "log_path", "log_sha256", "exit_code"},
        )
        return report

    def parse_framed_output(self, framed: bytes) -> tuple[bytes, bytes]:
        stdout_header, remainder = framed.split(b"\n", 1)
        self.assertTrue(stdout_header.startswith(b"stdout:"))
        stdout_length = int(stdout_header[len(b"stdout:") :])
        self.assertGreaterEqual(len(remainder), stdout_length)
        stderr_header = remainder[stdout_length:]
        stderr_header, stderr = stderr_header.split(b"\n", 1)
        self.assertTrue(stderr_header.startswith(b"stderr:"))
        stderr_length = int(stderr_header[len(b"stderr:") :])
        self.assertEqual(len(stderr), stderr_length)
        self.assertEqual(
            len(framed),
            len(stdout_header) + 1 + stdout_length
            + len(stderr_header) + 1 + stderr_length,
        )
        return remainder[:stdout_length], stderr

    def run_module_in_workspace(
        self,
        module,
        root: Path,
        *,
        hook=None,
    ) -> dict[str, object]:
        previous = Path.cwd()
        os.chdir(root)
        try:
            return module.run_verification(
                {"workspace_path": str(root)},
                _phase_hook=hook,
            )
        finally:
            os.chdir(previous)

    def wait_for_path(self, path: Path, timeout: float = 1.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return path.exists()

    def assert_no_path_for_duration(
        self,
        path: Path,
        duration: float,
    ) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.assertFalse(path.exists(), f"unexpected path: {path}")
            time.sleep(0.02)

    def test_verify_staged_command_maps_all_child_transitions(self) -> None:
        for transition, status, code in (
            ("passed", "passed", "passed"),
            ("failed", "needs_changes", "verification_failed"),
            ("blocked", "blocked", "verification_blocked"),
        ):
            with self.subTest(transition=transition):
                root = self.create_repository()
                self.install_verify_fixture(
                    root,
                    "#!/bin/sh\nprintf '%s\\n' '"
                    + json.dumps({"transition": transition})
                    + "'\n",
                )
                payload = self.run_verify_command(root)
                report = self.parse_verification_report(payload)
                self.assertEqual(payload["verification_status"], status)
                self.assertEqual(report["code"], code)
                self.assertEqual(report["exit_code"], 0)

    def test_verify_input_workspace_and_verifier_failures_are_safe(self) -> None:
        root = self.create_repository()
        scripts = self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
        )
        command = [str(scripts / "workflow-verify-report")]
        for raw, code in (
            ("[]", "input_invalid"),
            ("{}", "input_invalid"),
            ('{"workspace_path":""}', "input_invalid"),
            ('{"workspace_path":3}', "input_invalid"),
            (
                '{"workspace_path":"/tmp","workspace_path":"/tmp"}',
                "input_invalid",
            ),
            ("{\"workspace_path\":", "input_invalid"),
        ):
            result = subprocess.run(
                command,
                cwd=root,
                input=raw,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            report = self.parse_verification_report(json.loads(result.stdout))
            self.assertEqual(report["code"], code)
        empty = subprocess.run(
            command,
            cwd=root,
            input="{}",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(empty.returncode, 0)
        empty_payload = json.loads(empty.stdout)
        self.assertEqual(empty_payload["verification_status"], "blocked")
        self.assertEqual(
            self.parse_verification_report(empty_payload)["code"],
            "input_invalid",
        )
        mismatch = self.run_verify_command(
            root,
            workspace=str(root.parent),
        )
        self.assertEqual(
            self.parse_verification_report(mismatch)["code"],
            "workspace_invalid",
        )
        (root / ".kent" / "scripts" / "workflow-compile-verify").unlink()
        missing = self.run_verify_command(root)
        self.assertEqual(
            self.parse_verification_report(missing)["code"],
            "verifier_missing",
        )
        missing_report = self.parse_verification_report(missing)
        self.assertEqual(missing["verification_status"], "blocked")
        self.assertIsNone(missing_report["log_path"])
        self.assertIsNone(missing_report["log_sha256"])

    def test_verify_rejects_unsafe_verifier_and_content_addressed_log(self) -> None:
        root = self.create_repository()
        scripts = self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
        )
        verifier = scripts / "workflow-compile-verify"
        verifier.chmod(0o644)
        unsafe = self.run_verify_command(root)
        self.assertEqual(
            self.parse_verification_report(unsafe)["code"],
            "verifier_unsafe",
        )
        verifier.chmod(0o755)
        passed = self.run_verify_command(root)
        report = self.parse_verification_report(passed)
        log = root / str(report["log_path"])
        self.assertTrue(log.is_file())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            hashlib.sha256(log.read_bytes()).hexdigest(),
            report["log_sha256"],
        )

    def test_verify_replacement_environment_path_and_private_tmp(self) -> None:
        root = self.create_repository()
        capture_path = root / "verify-env.json"
        optional = {
            name: root / name.lower().replace("_", "-")
            for name in (
                "HOME",
                "ANDROID_HOME",
                "ANDROID_SDK_ROOT",
                "GRADLE_USER_HOME",
                "GOMODCACHE",
                "GOCACHE",
                "XDG_CACHE_HOME",
            )
        }
        java = root / "java"
        goroot = root / "goroot"
        gopath = root / "gopath"
        optional.update(
            {
                "JAVA_HOME": java,
                "GOROOT": goroot,
                "GOPATH": gopath,
            }
        )
        for directory in optional.values():
            directory.mkdir(parents=True)
        for directory in (java / "bin", goroot / "bin", gopath / "bin"):
            directory.mkdir(parents=True)
        invalid_optional_file = root / "android-sdk-file"
        invalid_optional_file.write_text("not a directory")
        invalid_optional = {
            "ANDROID_HOME": "relative-sdk",
            "ANDROID_SDK_ROOT": str(invalid_optional_file),
            "GRADLE_USER_HOME": "",
            "GOMODCACHE": "x" * 4097,
        }
        capture_code = (
            "import json, os; from pathlib import Path; path = Path("
            + repr(str(capture_path))
            + ")"
            + "; path.write_text(json.dumps({\"env\":dict(os.environ),"
            "\"tmp\":os.environ[\"TMPDIR\"]}))"
        )
        self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            + capture_code
            + "\nprint('{\"transition\":\"passed\"}')\n",
        )
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_environment_matrix_test",
        )
        environment = {
            **os.environ,
            "PATH": "/outside/path",
            "TMPDIR": str(root / "outside-tmp"),
            "CI": "override",
            "LANG": "override",
            "LC_ALL": "override",
            "GIT_TERMINAL_PROMPT": "override",
            "GCM_INTERACTIVE": "override",
            **{name: str(path) for name, path in optional.items()},
            **invalid_optional,
            "KENT_SECRET": "forbidden",
            "KENT_FORBIDDEN_SUFFIX": "forbidden",
            "GH_TOKEN": "forbidden",
            "GITHUB_TOKEN": "forbidden",
            "AWS_SECRET_ACCESS_KEY": "forbidden",
            "MY_TOKEN": "forbidden",
            "SECRET_SUFFIX": "forbidden",
            "DOCKER_PASSWORD": "forbidden",
            "PYTHONPATH": str(root),
        }
        payload = self.run_verify_command(root, environment=environment)
        report = self.parse_verification_report(payload)
        self.assertEqual(report["code"], "passed")
        captured = json.loads(capture_path.read_text())
        self.assertFalse((REPO_ROOT / "verify-env.json").exists())
        self.assertTrue(captured["tmp"].startswith("/dev/fd/"))
        expected_keys = {
            "CI",
            "LANG",
            "LC_ALL",
            "GIT_TERMINAL_PROMPT",
            "GCM_INTERACTIVE",
            "TMPDIR",
            "PATH",
            *(set(optional) - set(invalid_optional)),
        }
        interpreter_keys = {"__CF_USER_TEXT_ENCODING"}
        self.assertEqual(
            set(captured["env"]) - interpreter_keys,
            expected_keys,
        )
        self.assertEqual(captured["env"]["CI"], "1")
        self.assertEqual(captured["env"]["LANG"], "C")
        self.assertEqual(captured["env"]["LC_ALL"], "C")
        for name in (
            "GIT_TERMINAL_PROMPT",
            "GCM_INTERACTIVE",
        ):
            self.assertEqual(captured["env"][name], "0" if name == "GIT_TERMINAL_PROMPT" else "never")
        for name in (
            *invalid_optional,
            "KENT_SECRET",
            "KENT_FORBIDDEN_SUFFIX",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "MY_TOKEN",
            "SECRET_SUFFIX",
            "DOCKER_PASSWORD",
            "PYTHONPATH",
        ):
            self.assertNotIn(name, captured["env"])
        expected_path = [
            str(java / "bin"),
            str(goroot / "bin"),
            str(gopath / "bin"),
            *(
                path
                for path in module.FIXED_PATHS
                if Path(path).is_dir()
            ),
        ]
        self.assertEqual(
            captured["env"]["PATH"].split(os.pathsep)[:3],
            [str(java / "bin"), str(goroot / "bin"), str(gopath / "bin")],
        )
        self.assertEqual(
            captured["env"]["PATH"].split(os.pathsep),
            list(dict.fromkeys(expected_path)),
        )

    def test_verify_replacement_environment_map_boundary(self) -> None:
        root = self.create_repository()
        home = root / "home"
        java = root / "java"
        home.mkdir()
        (java / "bin").mkdir(parents=True)
        module = load_template_module(
            VERIFY_REPORT,
            "verify_report_environment_boundary_test",
        )
        original_os = module.os
        original_fd_path = module._fd_path
        module.os = types.SimpleNamespace(
            environ={
                "HOME": str(home),
                "JAVA_HOME": str(java),
                "PATH": "/attacker/path",
                "KENT_SECRET": "forbidden",
                "PYTHONHOME": "/attacker/python",
            },
            pathsep=os.pathsep,
        )
        module._fd_path = lambda _descriptor: "/private/verify-tmp"
        try:
            environment = module.replacement_environment(99)
        finally:
            module.os = original_os
            module._fd_path = original_fd_path
        expected_keys = {
            "CI",
            "LANG",
            "LC_ALL",
            "GIT_TERMINAL_PROMPT",
            "GCM_INTERACTIVE",
            "TMPDIR",
            "PATH",
            "HOME",
            "JAVA_HOME",
        }
        self.assertEqual(set(environment), expected_keys)
        self.assertEqual(environment["TMPDIR"], "/private/verify-tmp")
        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(java / "bin"))
        self.assertNotIn("KENT_SECRET", environment)
        self.assertNotIn("PYTHONHOME", environment)
        self.assertNotIn("__CF_USER_TEXT_ENCODING", environment)

    def test_verify_ignores_legacy_script_and_log_overrides(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        root = self.create_repository()
        fixed_marker = root / "fixed-verifier-ran"
        child_capture = root / "fixed-verifier-env.json"
        self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            "from pathlib import Path\n"
            + f"Path({str(fixed_marker)!r}).write_text('fixed')\n"
            + f"Path({str(child_capture)!r}).write_text(json.dumps(dict(os.environ)))\n"
            "print('{\"transition\":\"passed\"}')\n",
        )
        override_marker = outside / "override-ran"
        override_script = outside / "override-verifier.py"
        override_log = outside / "override-log.sentinel"
        override_script.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            + f"Path({str(override_marker)!r}).write_text('override')\n"
            "print('{\"transition\":\"passed\"}')\n"
        )
        override_script.chmod(0o755)
        environment = {
            **os.environ,
            "KENT_WORKFLOW_VERIFY_SCRIPT": str(override_script),
            "KENT_WORKFLOW_VERIFY_LOG": str(override_log),
        }
        payload = self.run_verify_command(root, environment=environment)
        report = self.parse_verification_report(payload)
        self.assertEqual(report["code"], "passed")
        self.assertTrue(fixed_marker.is_file())
        self.assertFalse(override_marker.exists())
        self.assertFalse(override_log.exists())
        self.assertTrue(
            str(report["log_path"]).startswith(
                "build/kent-workflow/verification-report-"
            )
        )
        child_output = child_capture.read_text()
        outer_output = json.dumps(payload)
        for value in (
            str(override_script),
            str(override_log),
        ):
            self.assertNotIn(value, child_output)
            self.assertNotIn(value, outer_output)

    def test_verify_child_output_and_exit_codes_are_distinct(self) -> None:
        cases = (
            (
                "printf '%s\\n' '{\"transition\":\"other\"}'",
                "child_output_invalid",
                "blocked",
                None,
            ),
            (
                "printf '%s\\n' '{\"transition\":\"passed\",\"x\":1}'",
                "child_output_invalid",
                "blocked",
                None,
            ),
            (
                "printf '%s\\n' '{\"transition\":\"passed\",\"transition\":\"failed\"}'",
                "child_output_invalid",
                "blocked",
                None,
            ),
            ("printf '%s\\n' 'not-json'", "child_output_invalid", "blocked", None),
            (
                "printf '%s\\n' '{\"transition\":\"passed\"}'; exit 7",
                "child_exit_nonzero",
                "needs_changes",
                7,
            ),
        )
        for body, code, status, exit_code in cases:
            with self.subTest(code=code, body=body):
                root = self.create_repository()
                self.install_verify_fixture(root, "#!/bin/sh\n" + body + "\n")
                payload = self.run_verify_command(root)
                report = self.parse_verification_report(
                    payload
                )
                self.assertEqual(report["code"], code)
                self.assertEqual(payload["verification_status"], status)
                self.assertEqual(report["exit_code"], exit_code)

    def test_verify_timeout_and_output_limit_are_bounded(self) -> None:
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/bin/sh\nsleep 5\n",
        )
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_timeout_test",
        )
        module.TIMEOUT_SECONDS = 0.05
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "child_timeout")
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/bin/sh\npython3 -c 'print(\"x\" * 10000)'\n",
        )
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_limit_test",
        )
        module.OUTPUT_LIMIT = 128
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "log_limit_exceeded")

    def test_verify_framing_is_bounded_and_parseable(self) -> None:
        module = load_template_module(
            VERIFY_REPORT,
            "verify_report_framing_test",
        )
        limit = 4 * 1024 * 1024
        for stdout, stderr, overflow in (
            (b"passed", b"", False),
            (b"x" * (limit + 1024), b"stderr", True),
            (b"stdout", b"y" * (limit + 1024), True),
        ):
            with self.subTest(
                stdout_length=len(stdout),
                stderr_length=len(stderr),
            ):
                framed, actual_overflow = module._frame(
                    stdout,
                    stderr,
                    limit,
                )
                self.assertLessEqual(len(framed), limit)
                self.assertEqual(actual_overflow, overflow)
                parsed_stdout, parsed_stderr = self.parse_framed_output(framed)
                if overflow:
                    self.assertLess(
                        len(parsed_stdout) + len(parsed_stderr),
                        len(stdout) + len(stderr),
                    )
                else:
                    self.assertEqual(parsed_stdout, stdout)
                    self.assertEqual(parsed_stderr, stderr)

    def test_verify_timeout_and_output_limit_kill_child_process_groups(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)

        ready = outside / "timeout-grandchild-ready"
        marker = outside / "timeout-grandchild-marker"
        grandchild_code = (
            "import pathlib, time; "
            + f"pathlib.Path({str(ready)!r}).write_text('ready'); "
            + "time.sleep(0.4); "
            + f"pathlib.Path({str(marker)!r}).write_text('leaked')"
        )
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "import pathlib, subprocess, sys, time\n"
            + f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}])\n"
            + f"deadline = time.monotonic() + 1; ready = pathlib.Path({str(ready)!r})\n"
            + "while not ready.exists() and time.monotonic() < deadline: time.sleep(0.001)\n"
            + "assert ready.exists()\n"
            "time.sleep(5)\n",
        )
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_timeout_group_test",
        )
        module.TIMEOUT_SECONDS = 0.05
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "child_timeout")
        self.assertTrue(self.wait_for_path(ready))
        self.assert_no_path_for_duration(marker, 0.7)
        self.assertFalse(marker.exists())

        ready = outside / "output-grandchild-ready"
        marker = outside / "output-grandchild-marker"
        grandchild_code = (
            "import pathlib, time; "
            + f"pathlib.Path({str(ready)!r}).write_text('ready'); "
            + "time.sleep(0.4); "
            + f"pathlib.Path({str(marker)!r}).write_text('leaked')"
        )
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "import pathlib, subprocess, sys, time\n"
            + f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}])\n"
            + f"deadline = time.monotonic() + 1; ready = pathlib.Path({str(ready)!r})\n"
            + "while not ready.exists() and time.monotonic() < deadline: time.sleep(0.001)\n"
            + "assert ready.exists()\n"
            "sys.stdout.write('x' * (4 * 1024 * 1024 + 1024))\n"
            "sys.stdout.flush()\n"
            "time.sleep(5)\n",
        )
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_output_group_test",
        )
        module.OUTPUT_LIMIT = 4 * 1024 * 1024
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "log_limit_exceeded")
        self.assertTrue(self.wait_for_path(ready))
        self.assert_no_path_for_duration(marker, 0.7)
        self.assertFalse(marker.exists())

    def test_verify_phase_hooks_reject_child_path_swap_and_temp_parent_swap(
        self,
    ) -> None:
        root = self.create_repository()
        scripts = self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
        )
        module = load_template_module(
            scripts / "workflow-verify-report",
            "verify_report_production_dir_hook_test",
        )
        phases: list[str] = []

        def swap_build(phase: str) -> None:
            phases.append(phase)
            if phase == "after_log_dir_open_before_revalidation":
                build = root / "build"
                build.rename(root / "build-moved")
                build.mkdir(mode=0o700)

        report = self.run_module_in_workspace(module, root, hook=swap_build)
        self.assertEqual(report["code"], "log_path_unsafe")
        self.assertNotIn("passed", report.values())
        self.assertIn("after_log_dir_open_before_revalidation", phases)

        root = self.create_repository()
        scripts = self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
        )
        module = load_template_module(
            scripts / "workflow-verify-report",
            "verify_report_path_swap_test",
        )
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        replacement = outside / "replacement"
        replacement.write_text("#!/bin/sh\nexit 9\n")
        replacement.chmod(0o755)

        def swap_child(phase: str) -> None:
            if phase == "after_verifier_open_before_child":
                original = scripts / "workflow-compile-verify"
                original.rename(scripts / "original")
                shutil.copyfile(replacement, scripts / "workflow-compile-verify")

        report = self.run_module_in_workspace(module, root, hook=swap_child)
        self.assertEqual(report["code"], "verifier_unsafe")

        root = self.create_repository()
        scripts = self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
        )
        module = load_template_module(
            scripts / "workflow-verify-report",
            "verify_report_temp_parent_swap_test",
        )
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)

        def swap_parent(phase: str) -> None:
            if phase == "after_log_temp_open_before_child":
                workflow = root / "build" / "kent-workflow"
                moved = root / "build" / "moved-workflow"
                workflow.rename(moved)
                workflow.symlink_to(outside, target_is_directory=True)

        report = self.run_module_in_workspace(module, root, hook=swap_parent)
        self.assertEqual(report["code"], "log_path_unsafe")
        self.assertEqual(list(outside.iterdir()), [])

    def test_verify_child_completion_revalidates_log_chain(self) -> None:
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "root = Path.cwd()\n"
            "(root / 'build' / 'kent-workflow').rename(\n"
            "    root / 'build' / 'moved-workflow'\n"
            ")\n"
            "(root / 'build' / 'kent-workflow').mkdir(mode=0o700)\n"
            "print('{\"transition\":\"passed\"}')\n",
        )
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_child_completion_chain_test",
        )
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "log_path_unsafe")
        self.assertNotEqual(report["code"], "passed")
        self.assertIsNone(report["log_path"])
        self.assertIsNone(report["log_sha256"])
        self.assertEqual(
            list(
                (root / "build" / "kent-workflow").glob(
                    "verification-report-*.log"
                )
            ),
            [],
        )
        self.assertEqual(
            list(
                (root / "build" / "moved-workflow").glob(
                    "verification-report-*.log"
                )
            ),
            [],
        )
        self.assertEqual(list(outside.iterdir()), [])

    def test_verify_log_gate_and_unsafe_existing_logs(self) -> None:
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
            ignored=False,
        )
        report = self.parse_verification_report(self.run_verify_command(root))
        self.assertEqual(report["code"], "log_path_unsafe")

        root = self.create_repository()
        marker = root / "verifier-ran"
        self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            + f"Path({str(marker)!r}).write_text('started')\n"
            "print('{\"transition\":\"passed\"}')\n",
        )
        tracked = root / "build" / "kent-workflow" / "force-tracked.txt"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("force tracked\n")
        self.run_git(root, "add", "-f", "build/kent-workflow/force-tracked.txt")
        self.run_git(root, "commit", "-q", "-m", "Force tracked ignored path")
        payload = self.run_verify_command(root)
        report = self.parse_verification_report(payload)
        self.assertEqual(report["code"], "log_path_unsafe")
        self.assertEqual(payload["verification_status"], "blocked")
        self.assertIsNone(report["log_path"])
        self.assertIsNone(report["log_sha256"])
        self.assertIsNone(report["exit_code"])
        self.assertFalse(marker.exists())

        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
        )
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_existing_log_test",
        )
        content = b"stdout:0\nstderr:0\n"
        digest = hashlib.sha256(content).hexdigest()
        log = root / "build" / "kent-workflow"
        log.mkdir(parents=True)
        final = log / f"verification-report-{digest}.log"
        final.write_bytes(content)
        final.chmod(0o644)
        with self.assertRaises(module.VerificationFailure) as raised:
            module.write_log(root, content)
        self.assertEqual(raised.exception.code, "log_path_unsafe")

    def test_verify_unsafe_log_paths_have_exact_code(self) -> None:
        content = b"stdout:0\nstderr:0\n"
        digest = hashlib.sha256(content).hexdigest()

        for kind in ("build_file", "workflow_file", "build_mode"):
            with self.subTest(kind=kind):
                root = self.create_repository()
                self.install_verify_fixture(
                    root,
                    "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
                )
                build = root / "build"
                if kind == "build_file":
                    build.write_text("not a directory")
                else:
                    build.mkdir()
                    if kind == "workflow_file":
                        (build / "kent-workflow").write_text(
                            "not a directory"
                        )
                    else:
                        build.chmod(0o777)
                result = self.run_verify_command(root)
                report = self.parse_verification_report(result)
                self.assertEqual(report["code"], "log_path_unsafe")

        for kind in ("mode", "hardlink", "symlink", "fifo", "content"):
            with self.subTest(existing=kind):
                root = self.create_repository()
                self.install_verify_fixture(
                    root,
                    "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
                )
                log = root / "build" / "kent-workflow"
                log.mkdir(parents=True)
                final = log / f"verification-report-{digest}.log"
                outside = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, outside)
                if kind == "mode":
                    final.write_bytes(content)
                    final.chmod(0o644)
                elif kind == "hardlink":
                    source = log / "source"
                    source.write_bytes(content)
                    os.link(source, final)
                elif kind == "symlink":
                    outside_file = outside / "outside.log"
                    outside_file.write_bytes(content)
                    final.symlink_to(outside_file)
                elif kind == "fifo":
                    os.mkfifo(final, 0o600)
                else:
                    final.write_bytes(b"wrong")
                    final.chmod(0o600)
                module = load_template_module(
                    root / ".kent" / "scripts" / "workflow-verify-report",
                    f"verify_report_existing_{kind}_test",
                )
                with self.assertRaises(module.VerificationFailure) as raised:
                    module.write_log(root, content)
                self.assertEqual(raised.exception.code, "log_path_unsafe")
                if kind == "symlink":
                    self.assertEqual(list(outside.iterdir()), [outside / "outside.log"])

        for kind in ("mode", "inode"):
            with self.subTest(temp=kind):
                root = self.create_repository()
                scripts = self.install_verify_fixture(
                    root,
                    "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
                )
                module = load_template_module(
                    scripts / "workflow-verify-report",
                    f"verify_report_temp_{kind}_test",
                )

                def tamper_temp(phase: str) -> None:
                    if phase != "after_log_temp_open_before_child":
                        return
                    workflow = root / "build" / "kent-workflow"
                    temporary = next(
                        workflow.glob(".verification-report-*.tmp")
                    )
                    if kind == "mode":
                        temporary.chmod(0o644)
                    else:
                        temporary.rename(workflow / "moved-temp")
                        temporary.write_bytes(b"replacement")
                        temporary.chmod(0o600)

                report = self.run_module_in_workspace(
                    module,
                    root,
                    hook=tamper_temp,
                )
                self.assertEqual(report["code"], "log_path_unsafe")
                self.assertIsNone(report["log_path"])

        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
        )
        log = root / "build" / "kent-workflow"
        log.mkdir(parents=True)
        final = log / f"verification-report-{digest}.log"
        final.write_bytes(content)
        final.chmod(0o600)
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_existing_safe_log_test",
        )
        path, actual_digest = module.write_log(root, content)
        self.assertEqual(path, f"build/kent-workflow/{final.name}")
        self.assertEqual(actual_digest, digest)
        self.assertEqual(final.read_bytes(), content)

    def test_verify_output_log_contains_secrets_but_outer_report_does_not(self) -> None:
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/bin/sh\n"
            "printf '%s\\n' '{\"transition\":\"passed\"}'\n"
            "printf '%s\\n' 'SECRET_SENTINEL' >&2\n",
        )
        result = subprocess.run(
            [
                str(root / ".kent" / "scripts" / "workflow-verify-report")
            ],
            cwd=root,
            input=json.dumps({"workspace_path": str(root)}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("SECRET_SENTINEL", result.stdout)
        report = self.parse_verification_report(json.loads(result.stdout))
        self.assertIn("SECRET_SENTINEL", (root / str(report["log_path"])).read_text())

    def test_log_write_rejects_directory_symlink_swap(self) -> None:
        root = self.create_repository()
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        module = load_template_module(
            REPO_ROOT / "templates" / "project" / "workflow-verify-report",
            "verify_report_symlink_test",
        )

        def swap_directory(_phase: str) -> None:
            build = root / "build"
            moved = root / "build-original"
            build.rename(moved)
            build.symlink_to(outside, target_is_directory=True)

        with self.assertRaises((OSError, ValueError)):
            module.write_log(root, b"must stay inside", _phase_hook=swap_directory)
        self.assertEqual(list(outside.iterdir()), [])

    def test_child_uses_replacement_environment_and_private_tmp_fd(self) -> None:
        root = self.create_repository()
        child = root / "child.py"
        child.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            "print(json.dumps({k: os.environ[k] for k in ('TMPDIR', 'PWD') if k in os.environ}))\n"
        )
        child.chmod(0o755)
        module = load_template_module(
            REPO_ROOT / "templates" / "project" / "workflow-verify-report",
            "verify_report_environment_test",
        )
        exit_code, stdout, stderr, failure = module.bounded_child(
            child,
            root,
            b"{}",
        )
        self.assertEqual(exit_code, 0, stderr.decode("utf-8", "replace"))
        self.assertIsNone(failure)
        environment = json.loads(stdout)
        self.assertTrue(environment["TMPDIR"].startswith("/dev/fd/"))
        self.assertNotIn("PWD", environment)


class GitHubPrFeedbackTest(GitRepositoryTest):
    def test_feedback_subprocess_materializes_all_item_variants(self) -> None:
        root = self.create_repository()
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        watcher = scripts / "workflow-wait-github-pr"
        shutil.copyfile(PR_WATCH, watcher)
        watcher.chmod(0o755)
        support = scripts / "workflow_runtime_contracts.py"
        shutil.copyfile(REPO_ROOT / "workflowkit" / "runtime.py", support)
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *issues/*/comments*) printf \'[{"id":1,"body":"issue body","created_at":"'
            '2026-08-20T00:00:00Z","updated_at":"2026-08-20T00:00:00Z",'
            '"user":{"login":"alice"}}]\\n\' ;;\n'
            '  *pulls/*/reviews*) printf \'[{"id":2,"body":"review body","state":"commented",'
            '"submitted_at":"2026-08-20T00:01:00Z","user":{"login":"bob"},'
            '"commit_id":"0000000000000000000000000000000000000000"}]\\n\' ;;\n'
            '  *pulls/*/comments*) printf \'[{"id":3,"body":"inline body","created_at":"'
            '2026-08-20T00:02:00Z","updated_at":"2026-08-20T00:02:00Z",'
            '"user":{"login":"carol"},"commit_id":"0000000000000000000000000000000000000000",'
            '"original_commit_id":"0000000000000000000000000000000000000000",'
            '"pull_request_review_thread_id":"thread-1"}]\\n\' ;;\n'
            '  *graphql*) printf \'[{"data":{"repository":{"pullRequest":{"reviewThreads":'
            '{"nodes":[{"id":"thread-1","isResolved":true,"isOutdated":false,'
            '"path":"src/app.py","line":12,"startLine":10,"originalLine":20,'
            '"originalStartLine":18,"subjectType":"LINE","comments":{"nodes":'
            '[{"id":"thread-comment-1"}],"pageInfo":{"hasNextPage":false,'
            '"endCursor":null}}}],"pageInfo":{"hasNextPage":false,'
            '"endCursor":null}}}}}}]\\n\' ;;\n'
            '  *) printf \'[]\\n\' ;;\n'
            "esac\n"
        )
        fake_gh.chmod(0o755)
        module = load_template_module(watcher, "pr_feedback_materialize_test")
        items = module.read_feedback(
            root,
            "https://github.com/example/repo/pull/1",
            str(fake_gh),
            timeout=5,
        )
        self.assertEqual(
            [(item["kind"], item["id"]) for item in items],
            [
                ("issue_comment", "1"),
                ("review", "2"),
                ("review_comment", "3"),
                ("review_thread", "thread-1"),
            ],
        )
        self.assertTrue(all("body" not in item for item in items))
        self.assertEqual(items[-1]["comment_ids"], ["thread-comment-1"])
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "pr_feedback_materialize_runtime",
        )
        for item in items:
            runtime.validate_pr_feedback_item(item)

    def test_feedback_cursor_identity_matrix_and_limits(self) -> None:
        root = self.create_repository()
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        watcher = scripts / "workflow-wait-github-pr"
        shutil.copyfile(PR_WATCH, watcher)
        watcher.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        module = load_template_module(watcher, "pr_feedback_cursor_matrix")
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "pr_feedback_cursor_runtime_matrix",
        )
        commit = "0" * 40
        issue = module._feedback_item(
            "issue_comment",
            {
                "id": "issue",
                "body": "é",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "user": {"login": None},
            },
        )
        review = module._feedback_item(
            "review",
            {
                "id": "review",
                "body": None,
                "state": "COMMENTED",
                "submitted_at": None,
                "updated_at": "2026-08-20T00:01:00Z",
                "user": {"login": None},
                "commit_id": commit,
            },
        )
        comment = module._feedback_item(
            "review_comment",
            {
                "id": "comment",
                "body": "inline",
                "created_at": "2026-08-20T00:02:00Z",
                "updated_at": "2026-08-20T00:02:00Z",
                "user": None,
                "commit_id": commit,
                "original_commit_id": commit,
                "pull_request_review_thread_id": "thread-1",
            },
        )
        thread = {
            "kind": "review_thread",
            "id": "thread-1",
            "resolved": False,
            "outdated": False,
            "path": "src/app.py",
            "current_line": 12,
            "current_start_line": 10,
            "original_line": 20,
            "original_start_line": 18,
            "subject_type": "LINE",
            "comment_ids": ["thread-comment-1"],
        }
        items = sorted(
            [issue, review, comment, thread],
            key=lambda item: (item["kind"], item["id"]),
        )
        checks = [
            {
                "workflow_name": "PR",
                "check_name": "unit",
                "bucket": "pass",
                "state": "SUCCESS",
                "link": None,
            }
        ]

        def cursor(
            *,
            item_rows: list[dict[str, object]] = items,
            check_rows: list[dict[str, object]] = checks,
            **overrides: object,
        ) -> str | dict[str, object]:
            state: dict[str, object] = {
                "repository": "example/repo",
                "pull_number": 1,
                "head_oid": commit,
                "base_oid": commit,
                "pr_state": "OPEN",
                "review_decision": "",
                "merge_state_status": "CLEAN",
            }
            state.update(overrides)
            return runtime.make_pr_feedback_cursor(
                **state,
                checks=check_rows,
                items=sorted(
                    item_rows,
                    key=lambda item: (item["kind"], item["id"]),
                ),
            )

        complete = cursor()
        self.assertEqual(complete["mode"], "complete")
        self.assertEqual(
            runtime.classify_pr_feedback("uninitialized", complete)["transition"],
            "state_changed",
        )
        self.assertEqual(issue["body_bytes"], len("é".encode("utf-8")))
        self.assertEqual(
            issue["body_sha256"],
            hashlib.sha256("é".encode("utf-8")).hexdigest(),
        )
        mutations = [
            ("add", [*items, {**issue, "id": "issue-added"}], checks, {}),
            ("delete", items[:-1], checks, {}),
            ("issue-author", [{**issue, "author_login": "alice"}, *items[1:]], checks, {}),
            ("issue-created", [{**issue, "created_at": "2026-08-21T00:00:00Z"}, *items[1:]], checks, {}),
            ("issue-updated", [{**issue, "updated_at": "2026-08-21T00:00:00Z"}, *items[1:]], checks, {}),
            ("issue-body", [{**issue, "body_sha256": hashlib.sha256(b"edited").hexdigest()}, *items[1:]], checks, {}),
            ("review-state", [items[0], {**review, "state": "APPROVED"}, *items[2:]], checks, {}),
            ("thread-resolved", [*items[:3], {**thread, "resolved": True}], checks, {}),
            ("thread-outdated", [*items[:3], {**thread, "outdated": True}], checks, {}),
            ("thread-path", [*items[:3], {**thread, "path": "src/other.py"}], checks, {}),
            ("thread-current-line", [*items[:3], {**thread, "current_line": 13}], checks, {}),
            ("thread-current-start", [*items[:3], {**thread, "current_start_line": 11}], checks, {}),
            ("thread-original-line", [*items[:3], {**thread, "original_line": 21}], checks, {}),
            ("thread-original-start", [*items[:3], {**thread, "original_start_line": 19}], checks, {}),
            ("thread-subject", [*items[:3], {**thread, "subject_type": "FILE"}], checks, {}),
            ("thread-comments", [*items[:3], {**thread, "comment_ids": ["thread-comment-2"]}], checks, {}),
            ("comment-thread", [items[0], items[1], {**comment, "thread_id": "thread-2"}, items[3]], checks, {}),
            ("comment-author", [items[0], items[1], {**comment, "author_login": "alice"}, items[3]], checks, {}),
            (
                "comment-created",
                [
                    items[0],
                    items[1],
                    {**comment, "created_at": "2026-08-21T00:00:00Z"},
                    items[3],
                ],
                checks,
                {},
            ),
            (
                "comment-updated",
                [
                    items[0],
                    items[1],
                    {**comment, "updated_at": "2026-08-21T00:00:00Z"},
                    items[3],
                ],
                checks,
                {},
            ),
            (
                "comment-current-commit",
                [
                    items[0],
                    items[1],
                    {**comment, "current_commit_oid": "1" * 40},
                    items[3],
                ],
                checks,
                {},
            ),
            (
                "comment-original-commit",
                [
                    items[0],
                    items[1],
                    {**comment, "original_commit_oid": "1" * 40},
                    items[3],
                ],
                checks,
                {},
            ),
            ("check-add", items, [*checks, {**checks[0], "check_name": "lint"}], {}),
            ("check-delete", items, [], {}),
            ("check-edit", items, [{**checks[0], "state": "FAILURE", "bucket": "fail"}], {}),
            ("head", items, checks, {"head_oid": "1" * 40}),
            ("base", items, checks, {"base_oid": "1" * 40}),
            ("repository", items, checks, {"repository": "other/repo"}),
            ("pull-number", items, checks, {"pull_number": 2}),
            ("pr-state", items, checks, {"pr_state": "CLOSED"}),
            ("review-decision", items, checks, {"review_decision": "APPROVED"}),
            ("merge-state", items, checks, {"merge_state_status": "BLOCKED"}),
        ]
        for label, item_rows, check_rows, overrides in mutations:
            with self.subTest(label=label):
                changed = cursor(
                    item_rows=item_rows,
                    check_rows=check_rows,
                    **overrides,
                )
                self.assertEqual(
                    runtime.classify_pr_feedback(complete, changed)["transition"],
                    "state_changed",
                )

        many = [
            {**issue, "id": "issue-{:03d}".format(index)}
            for index in range(101)
        ]
        digest_only = cursor(item_rows=many)
        self.assertEqual(digest_only["mode"], "digest_only")
        self.assertEqual(
            runtime.classify_pr_feedback(digest_only, digest_only)["transition"],
            "still_waiting",
        )
        changed_digest = [
            {**item, "body_sha256": hashlib.sha256(b"changed").hexdigest()}
            if index == 0
            else item
            for index, item in enumerate(many)
        ]
        self.assertEqual(
            runtime.classify_pr_feedback(
                digest_only,
                cursor(item_rows=changed_digest),
            )["transition"],
            "state_changed",
        )
        with self.assertRaises(ValueError):
            cursor(item_rows=[
                {**issue, "id": "issue-{:04d}".format(index)}
                for index in range(1001)
            ])

    def test_feedback_queries_cover_pagination_timeout_and_safe_digests(self) -> None:
        root = self.create_repository()
        module = load_template_module(PR_WATCH, "pr_feedback_query_safety")
        paged = root / "gh-paged"
        paged.write_text(
            "#!/bin/sh\n"
            "printf '[[{\"id\":\"one\"}],[{\"id\":\"two\"}]]\\n'\n"
        )
        paged.chmod(0o755)
        rows = module._paginate_feedback(
            str(paged),
            root,
            "repos/example/repo/issues/1/comments",
            timeout=5,
        )
        self.assertEqual([row["id"] for row in rows], ["one", "two"])

        failing = root / "gh-failing"
        failing.write_text("#!/bin/sh\nprintf out\nprintf err >&2\nexit 7\n")
        failing.chmod(0o755)
        with self.assertRaises(module.QueryError) as failure:
            module.read_state(
                root,
                "https://github.com/example/repo/pull/1",
                str(failing),
                timeout=5,
            )
        self.assertEqual(failure.exception.code, "github_query_failed")
        self.assertEqual(
            hashlib.sha256(failure.exception.stdout).hexdigest(),
            hashlib.sha256(b"out").hexdigest(),
        )
        self.assertEqual(
            hashlib.sha256(failure.exception.stderr).hexdigest(),
            hashlib.sha256(b"err").hexdigest(),
        )
        hung = root / "gh-hung"
        hung.write_text("#!/bin/sh\nsleep 2\n")
        hung.chmod(0o755)
        with self.assertRaises(module.QueryError) as timeout_error:
            module.read_state(
                root,
                "https://github.com/example/repo/pull/1",
                str(hung),
                timeout=1,
            )
        self.assertEqual(timeout_error.exception.code, "github_query_failed")

        malformed = root / "gh-malformed"
        malformed.write_text("#!/bin/sh\nprintf '[[{}], [\"bad\"]]\\n'\n")
        malformed.chmod(0o755)
        with self.assertRaises(module.QueryError) as output:
            module._paginate_feedback(
                str(malformed),
                root,
                "repos/example/repo/issues/1/comments",
                timeout=5,
            )
        self.assertEqual(output.exception.code, "github_output_invalid")


class GitHubCiWatchTest(GitRepositoryTest):
    def expected_contract(
        self,
        *,
        commit: str = "1" * 40,
        checks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "schema": "github-ci-expected-checks-v1",
            "repository": "example/repo",
            "project_commit": commit,
            "runtime_source_envelope_digest": "a" * 64,
            "checks": checks
            or [
                {
                    "workflow_name": "PR",
                    "check_name": "unit",
                    "allow_skipped": False,
                }
            ],
        }

    def observed_check(
        self,
        *,
        name: str = "unit",
        workflow: str = "PR",
        bucket: str = "pass",
        state: str = "SUCCESS",
    ) -> dict[str, object]:
        return {
            "name": name,
            "workflow": workflow,
            "bucket": bucket,
            "state": state,
            "link": None,
        }

    def test_v2_unicode_feedback_cursor_round_trips_through_ci_consumer(
        self,
    ) -> None:
        watcher = load_template_module(
            CI_WATCH,
            "ci_unicode_feedback_cursor_consumer",
        )
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_unicode_feedback_cursor_runtime",
        )
        cursor = runtime.make_pr_feedback_cursor(
            repository="example/repo",
            pull_number=1,
            head_oid="0" * 40,
            base_oid="1" * 40,
            pr_state="OPEN",
            review_decision="",
            merge_state_status="CLEAN",
            checks=[],
            items=[
                {
                    "kind": "review_thread",
                    "id": "thread-1",
                    "resolved": False,
                    "outdated": False,
                    "path": "src/é.kt",
                    "current_line": 12,
                    "current_start_line": 10,
                    "original_line": 20,
                    "original_start_line": 18,
                    "subject_type": "LINE",
                    "comment_ids": ["thread-comment-1"],
                }
            ],
        )
        raw = runtime.canonical_bytes(cursor).decode("utf-8")
        self.assertIn("src/é.kt", raw)
        with mock.patch.object(
            watcher,
            "runtime_contracts",
            return_value=runtime,
        ):
            carrier = watcher.pr_feedback_cursor_values(
                {"pr_feedback_cursor": raw}
            )
        self.assertEqual(carrier, {"pr_feedback_cursor": raw})
        self.assertEqual(
            runtime.parse_canonical_json(
                carrier["pr_feedback_cursor"],
                label="PR feedback cursor",
                max_bytes=runtime.MAX_FEEDBACK_BYTES,
            ),
            cursor,
        )

    def test_v2_runtime_contracts_rejects_symlinked_support_module(self) -> None:
        root = self.create_repository()
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        watcher = scripts / "workflow-wait-github-ci"
        shutil.copyfile(CI_WATCH, watcher)
        watcher.chmod(0o755)
        marker = root / "attacker-marker"
        attacker = root / "attacker.py"
        attacker.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
        )
        (scripts / "workflow_runtime_contracts.py").symlink_to(attacker)
        module = load_template_module(
            watcher,
            "ci_symlinked_runtime_contracts",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "runtime contract support module is unsafe",
        ):
            module.runtime_contracts()
        self.assertFalse(marker.exists())

    def watch(
        self,
        root: Path,
        *,
        pr_state: dict[str, object],
        checks: list[dict[str, object]],
        watch_exit: int = 0,
        prior_report: dict[str, object] | None = None,
        pr_feedback_cursor: str | None = None,
        expected: dict[str, object] | None = None,
        gh_script: str | None = None,
        max_polls: int = 1,
        max_errors: int = 5,
        query_timeout: int = 60,
        ready_file: Path | None = None,
        pid_file: Path | None = None,
    ) -> dict[str, object]:
        (root / "pr-state.json").write_text(json.dumps(pr_state))
        (root / "checks.json").write_text(json.dumps(checks))
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (root / ".kent" / "workflow-profile.toml").write_text(
            "schema_version = {}\n".format(4 if expected is not None else 3)
        )
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        watcher = scripts / "workflow-wait-github-ci"
        shutil.copyfile(CI_WATCH, watcher)
        watcher.chmod(0o755)
        fake_gh = root / "gh"
        fake_gh.write_text(
            gh_script
            or (
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
        if pr_feedback_cursor is not None:
            workflow_input["pr_feedback_cursor"] = pr_feedback_cursor
        if expected is not None:
            runtime = load_template_module(
                REPO_ROOT / "workflowkit" / "runtime.py",
                "runtime_for_ci_watch_helper",
            )
            workflow_input.update(
                {
                    "expected_ci_checks": json.dumps(
                        expected,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "expected_ci_checks_sha256": (
                        runtime.expected_ci_checks_sha256(expected)
                    ),
                    "runtime_source_envelope_digest": (
                        expected["runtime_source_envelope_digest"]
                    ),
                }
            )
        environment = {
            **os.environ,
            "PATH": "{}:{}".format(root, os.environ.get("PATH", "")),
            "KENT_TEST_PR_STATE": str(root / "pr-state.json"),
            "KENT_TEST_CHECKS": str(root / "checks.json"),
            "KENT_TEST_WATCH_EXIT": str(watch_exit),
            "KENT_CI_WATCH_TEST_MODE": "1",
            "KENT_CI_WATCH_INTERVAL_SECONDS": "0",
            "KENT_CI_WATCH_MAX_POLLS": str(max_polls),
            "KENT_CI_WATCH_MAX_ERRORS": str(max_errors),
            "KENT_CI_WATCH_QUERY_TIMEOUT_SECONDS": str(query_timeout),
        }
        if ready_file is not None:
            environment["KENT_READY_FILE"] = str(ready_file)
        if pid_file is not None:
            environment["KENT_PID_FILE"] = str(pid_file)
        if ready_file is None:
            result = subprocess.run(
                [str(watcher)],
                cwd=root,
                input=json.dumps(workflow_input),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
            )
        else:
            process = subprocess.Popen(
                [str(watcher)],
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            process.stdin.write(json.dumps(workflow_input))
            process.stdin.flush()
            process.stdin.close()
            process.stdin = None
            ready_deadline = time.monotonic() + 5
            while (
                not ready_file.exists()
                and process.poll() is None
                and time.monotonic() < ready_deadline
            ):
                time.sleep(0.01)
            self.assertTrue(ready_file.exists())
            stdout, stderr = process.communicate()
            result = subprocess.CompletedProcess(
                process.args,
                process.returncode,
                stdout,
                stderr,
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

    def test_schema3_carrier_requires_absent_report_for_legacy_mode(self) -> None:
        root = self.create_repository()
        pr_state = {
            "state": "OPEN",
            "headRefName": "TASK-5",
            "headRefOid": "head",
            "baseRefName": "main",
            "baseRefOid": "base",
            "url": "https://github.com/example/repo/pull/1",
        }
        legacy = self.watch(
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
        )
        self.assertEqual(legacy["transition"], "ci_watch_failed")
        self.assertEqual(
            json.loads(legacy["ci_report"])["reason"],
            "terminal_check_failure",
        )
        self.assertNotIn("mode", json.loads(legacy["ci_report"]))
        self.assertNotIn("schema", json.loads(legacy["ci_report"]))

        nonempty_legacy = self.watch(
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
            prior_report={"reason": "legacy"},
        )
        self.assertEqual(nonempty_legacy["transition"], "ci_watch_failed")
        self.assertEqual(
            json.loads(nonempty_legacy["ci_report"])["attempts"][-1][
                "safe_error"
            ]["code"],
            "report_invalid",
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

    def test_v2_ci_accepts_canonical_expected_checks_string(self) -> None:
        root = self.create_repository()
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        watcher = scripts / "workflow-wait-github-ci"
        shutil.copyfile(CI_WATCH, watcher)
        watcher.chmod(0o755)
        support = scripts / "workflow_runtime_contracts.py"
        shutil.copyfile(REPO_ROOT / "workflowkit" / "runtime.py", support)
        commit = "1" * 40
        envelope = "a" * 64
        expected = {
            "schema": "github-ci-expected-checks-v1",
            "repository": "example/repo",
            "project_commit": commit,
            "runtime_source_envelope_digest": envelope,
            "checks": [
                {
                    "workflow_name": "PR",
                    "check_name": "unit",
                    "allow_skipped": False,
                }
            ],
        }
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_for_ci_string_test",
        )
        (root / "pr-state.json").write_text(
            json.dumps(
                {
                    "state": "OPEN",
                    "headRefOid": commit,
                    "baseRefOid": "2" * 40,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        (root / "checks.json").write_text(
            json.dumps(
                [
                    {
                        "name": "unit",
                        "workflow": "PR",
                        "bucket": "pass",
                        "state": "SUCCESS",
                        "link": None,
                    }
                ]
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            'if [ "$1 $2" = "pr view" ]; then cat "$KENT_TEST_PR_STATE"; exit 0; fi\n'
            'if [ "$1 $2" = "pr checks" ]; then\n'
            '  case " $* " in *" --watch "*) exit 0;; esac\n'
            '  cat "$KENT_TEST_CHECKS"; exit 0\n'
            "fi\n"
            "exit 2\n"
        )
        fake_gh.chmod(0o755)
        payload = {
            "workspace_path": str(root),
            "pr_url": "https://github.com/example/repo/pull/1",
            "branch_name": "TASK-5",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
            "expected_ci_checks": json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "expected_ci_checks_sha256": runtime.expected_ci_checks_sha256(expected),
            "runtime_source_envelope_digest": envelope,
        }
        result = subprocess.run(
            [str(watcher)],
            cwd=root,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_TEST_PR_STATE": str(root / "pr-state.json"),
                "KENT_TEST_CHECKS": str(root / "checks.json"),
                "KENT_CI_WATCH_TEST_MODE": "1",
                "KENT_CI_WATCH_INTERVAL_SECONDS": "0",
                "KENT_CI_WATCH_MAX_POLLS": "1",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["transition"], "ci_watch_passed", output)
        self.assertEqual(
            json.loads(output["ci_report"])["mode"],
            "expected-v1",
        )
        self.assertEqual(output["pr_feedback_cursor"], "uninitialized")

    def test_v2_ci_overdepth_expected_checks_fails_closed_without_child(self) -> None:
        root = self.create_repository()
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        (root / ".kent" / "workflow-profile.toml").write_text(
            "schema_version = 4\n"
        )
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        watcher = scripts / "workflow-wait-github-ci"
        shutil.copyfile(CI_WATCH, watcher)
        watcher.chmod(0o755)
        marker = root / "github-child-invoked"
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            f"echo invoked > {str(marker)!r}\n"
            "exit 0\n"
        )
        fake_gh.chmod(0o755)
        overdepth = "[" * 101 + "0" + "]" * 101
        payload = {
            "workspace_path": str(root),
            "pr_url": "https://github.com/example/repo/pull/1",
            "branch_name": "TASK-5",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
            "expected_ci_checks": overdepth,
            "expected_ci_checks_sha256": "a" * 64,
            "runtime_source_envelope_digest": "b" * 64,
        }
        result = subprocess.run(
            [str(watcher)],
            cwd=root,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_CI_WATCH_TEST_MODE": "1",
                "KENT_CI_WATCH_INTERVAL_SECONDS": "0",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["transition"], "ci_watch_failed")
        self.assertEqual(
            json.loads(output["ci_report"])["attempts"][-1]["safe_error"]["code"],
            "expected_contract_invalid",
        )
        self.assertEqual(output["pr_feedback_cursor"], "uninitialized")
        self.assertFalse(marker.exists())
        self.assertNotIn(overdepth, result.stdout + result.stderr)
        self.assertNotIn("RecursionError", result.stdout + result.stderr)

    def test_v2_source_change_has_no_fabricated_ci_report(self) -> None:
        root = self.create_repository()
        expected = {
            "schema": "github-ci-expected-checks-v1",
            "repository": "example/repo",
            "project_commit": "0" * 40,
            "runtime_source_envelope_digest": "a" * 64,
            "checks": [
                {
                    "workflow_name": "PR",
                    "check_name": "unit",
                    "allow_skipped": False,
                }
            ],
        }
        result = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[
                {
                    "name": "unit",
                    "workflow": "PR",
                    "bucket": "pass",
                    "state": "SUCCESS",
                    "link": None,
                }
            ],
            expected=expected,
            pr_feedback_cursor="uninitialized",
        )
        self.assertEqual(result["transition"], "ci_watch_source_changed")
        self.assertNotIn("ci_report", result)
        self.assertEqual(result["pr_feedback_cursor"], "uninitialized")

    def test_observation_limit_boundary_is_runtime_contract_blocker(self) -> None:
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_observation_limit_boundary",
        )
        expected = {
            "schema": "github-ci-expected-checks-v1",
            "repository": "example/repo",
            "project_commit": "0" * 40,
            "runtime_source_envelope_digest": "a" * 64,
            "checks": [
                {
                    "workflow_name": "PR",
                    "check_name": "unit",
                    "allow_skipped": False,
                }
            ],
        }
        normalized = []

        def validate_observed(_item: object, _label: str) -> dict[str, object]:
            index = len(normalized)
            value = {
                "workflow_name": "W",
                "check_name": "unexpected-{}".format(index),
                "bucket": "pass",
                "state": "SUCCESS",
                "link": None,
            }
            normalized.append(value)
            return value

        observed = range(10001)
        metadata = {
            "expected_checks": [],
            "unexpected_check_count": 10001,
            "unexpected_checks_sha256": "b" * 64,
        }
        with mock.patch.object(
            runtime,
            "_validate_observed_check",
            side_effect=validate_observed,
        ), mock.patch.object(
            runtime,
            "_expected_check_observation_metadata",
            return_value=metadata,
        ):
            classification = runtime.classify_expected_ci_checks(
                expected,
                observed,
                current_repository="example/repo",
                current_head_oid="0" * 40,
                runtime_source_envelope_digest="a" * 64,
                expected_checks_digest=runtime.expected_ci_checks_sha256(expected),
            )
        self.assertEqual(classification["transition"], "report_invalid")
        self.assertEqual(classification["reason"], "observation_limit")
        self.assertEqual(classification["unexpected_check_count"], 10001)

        class OversizedObservations(list):
            def __len__(self) -> int:
                return 10001

        with self.assertRaises(ValueError):
            runtime.make_report_invalid_attempt(
                sequence=1,
                head_oid="0" * 40,
                base_oid="1" * 40,
                raw_observations=OversizedObservations(),
            )

    def test_observation_limit_production_path_remains_blocked(self) -> None:
        watcher = load_template_module(CI_WATCH, "ci_observation_limit_path")
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_observation_limit_path_runtime",
        )
        expected = self.expected_contract(commit="0" * 40)
        checks = [
            self.observed_check(name="unexpected-{}".format(index))
            for index in range(10001)
        ]
        payload = {
            "pr_url": "https://github.com/example/repo/pull/1",
            "expected_ci_checks": json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "expected_ci_checks_sha256": runtime.expected_ci_checks_sha256(expected),
            "runtime_source_envelope_digest": "a" * 64,
        }
        receipt = runtime.RejectedObservationReceipt(
            "unexpected_rows",
            10001,
            "b" * 64,
        )
        classification = runtime.ExpectedCiClassification(
            "observation_limit",
            {
                "transition": "report_invalid",
                "reason": "observation_limit",
                "expected_checks": [],
                "unexpected_check_count": 10001,
                "unexpected_checks_sha256": "b" * 64,
            },
            runtime.RejectedObservationReceipt(
                "projected_rows",
                10002,
                "c" * 64,
            ),
            receipt,
        )
        with mock.patch.object(
            watcher,
            "runtime_contracts",
            return_value=runtime,
        ), mock.patch.object(
            runtime,
            "classify_expected_ci_checks_with_receipt",
            return_value=classification,
        ):
            transition, report, reason = watcher.v2_ci_report(
                payload,
                {"headRefOid": "0" * 40, "baseRefOid": "1" * 40},
                checks,
                watch_exit=1,
            )
        self.assertEqual((transition, reason), ("ci_watch_failed", "report_invalid"))
        attempt = json.loads(report)["attempts"][-1]
        self.assertEqual(attempt["safe_error"]["code"], "observation_limit")
        self.assertEqual(attempt["unexpected_check_count"], 10001)
        self.assertEqual(
            attempt["unexpected_checks_sha256"],
            "b" * 64,
        )

    def test_v2_observation_limit_initial_append_is_typed_and_stable(self) -> None:
        watcher = load_template_module(
            CI_WATCH,
            "ci_observation_limit_initial_append",
        )
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_observation_limit_initial_append_runtime",
        )
        expected = self.expected_contract()
        checks = [
            self.observed_check(),
            *[
                self.observed_check(name="unexpected-{:05d}".format(index))
                for index in range(10001)
            ],
        ]
        payload = {
            "pr_url": "https://github.com/example/repo/pull/1",
            "expected_ci_checks": json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "expected_ci_checks_sha256": runtime.expected_ci_checks_sha256(expected),
            "runtime_source_envelope_digest": expected[
                "runtime_source_envelope_digest"
            ],
        }
        with mock.patch.object(
            watcher,
            "runtime_contracts",
            return_value=runtime,
        ), mock.patch.object(
            runtime,
            "make_report_invalid_attempt",
            side_effect=AssertionError("legacy builder was called"),
        ):
            transition, report_json, reason = watcher.v2_ci_report(
                payload,
                {"headRefOid": "1" * 40, "baseRefOid": "2" * 40},
                checks,
                watch_exit=1,
            )
        self.assertEqual((transition, reason), ("ci_watch_failed", "report_invalid"))
        report = json.loads(report_json)
        attempt = report["attempts"][-1]
        normalized = watcher.v2_ci_observation(checks)
        unexpected = sorted(
            normalized[1:],
            key=runtime._observed_check_sort_key,
        )
        expected_digest = runtime.canonical_sha256(unexpected)
        self.assertEqual(attempt["unexpected_check_count"], 10001)
        self.assertEqual(
            attempt["unexpected_checks_sha256"],
            expected_digest,
        )
        runtime.validate_ci_report(report)
        payload["ci_report"] = report_json
        with mock.patch.object(
            watcher,
            "runtime_contracts",
            return_value=runtime,
        ):
            _, appended_json, _ = watcher.v2_ci_report(
                payload,
                {"headRefOid": "1" * 40, "baseRefOid": "2" * 40},
                list(reversed(checks)),
                watch_exit=1,
            )
        appended = json.loads(appended_json)
        self.assertEqual(len(appended["attempts"]), 2)
        self.assertEqual(
            appended["attempts"][-1]["unexpected_checks_sha256"],
            expected_digest,
        )
        runtime.validate_ci_report(appended)

    def test_v2_attempt_wire_boundary_initial_and_append_are_size_only(self) -> None:
        watcher = load_template_module(
            CI_WATCH,
            "ci_attempt_wire_boundary_production_path",
        )
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_attempt_wire_boundary_runtime",
        )
        expected_rows = [
            {
                "workflow_name": "W",
                "check_name": "{:03d}".format(index),
                "allow_skipped": False,
            }
            for index in range(30)
        ]
        expected = self.expected_contract(checks=expected_rows)
        payload = {
            "pr_url": "https://github.com/example/repo/pull/1",
            "expected_ci_checks": json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "expected_ci_checks_sha256": runtime.expected_ci_checks_sha256(
                expected
            ),
            "runtime_source_envelope_digest": "a" * 64,
        }
        pr = {"headRefOid": "1" * 40, "baseRefOid": "2" * 40}
        link_prefix = "https://github.com/" + "x" * 1522

        def checks(extra: int) -> list[dict[str, object]]:
            result = [
                self.observed_check(
                    name="{:03d}".format(index),
                    workflow="W",
                )
                for index in range(30)
            ]
            for item in result:
                item["link"] = link_prefix
            result[-1]["link"] = link_prefix + "x" * extra
            return result

        def candidate_size(extra: int) -> int:
            observations = watcher.v2_ci_observation(checks(extra))
            classification = runtime.classify_expected_ci_checks_with_receipt(
                expected,
                observations,
                current_repository="example/repo",
                current_head_oid="1" * 40,
                runtime_source_envelope_digest="a" * 64,
                expected_checks_digest=runtime.expected_ci_checks_sha256(
                    expected
                ),
            )
            value = classification.materialize_value()
            candidate = {
                "sequence": 1,
                "head_oid": "1" * 40,
                "base_oid": "2" * 40,
                "reason": value["transition"],
                "watcher_exit_code": 0,
                "expected_checks": value["expected_checks"],
                "unexpected_check_count": value["unexpected_check_count"],
                "unexpected_checks_sha256": value[
                    "unexpected_checks_sha256"
                ],
                "retry": None,
                "safe_error": None,
            }
            return len(runtime.canonical_bytes(candidate))

        limit = runtime.MAX_CI_ATTEMPT_BYTES
        exact_extra = next(
            extra
            for extra in range(508)
            if candidate_size(extra) == limit
        )
        over_extra = exact_extra + 1
        projected = runtime._project_observed_rows(
            watcher.v2_ci_observation(checks(over_extra))
        )
        projected_digest = runtime.canonical_sha256(projected)
        projected_count = len(projected)

        def report(
            selected_checks: list[dict[str, object]],
            previous: str | None = None,
        ) -> dict[str, object]:
            current = dict(payload)
            if previous is not None:
                current["ci_report"] = previous
            with mock.patch.object(
                watcher,
                "runtime_contracts",
                return_value=runtime,
            ), mock.patch.object(
                runtime,
                "make_report_invalid_attempt",
                side_effect=AssertionError("legacy builder was called"),
            ):
                transition, report_json, reason = watcher.v2_ci_report(
                    current,
                    pr,
                    selected_checks,
                    watch_exit=0,
                )
            self.assertEqual((transition, reason), (
                "ci_watch_passed",
                "all_expected_checks_terminal_green",
            ))
            assert report_json is not None
            return json.loads(report_json)

        exact_initial = report(checks(exact_extra))
        exact_attempt = exact_initial["attempts"][-1]
        self.assertEqual(
            len(runtime.canonical_bytes(exact_attempt)),
            limit,
        )
        self.assertIsNone(exact_attempt["safe_error"])

        exact_append = report(
            checks(exact_extra),
            json.dumps(
                exact_initial,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(
            len(runtime.canonical_bytes(exact_append["attempts"][-1])),
            limit,
        )
        self.assertEqual(exact_append["attempts"][-1]["sequence"], 2)

        for previous in (None, exact_initial):
            with self.subTest(previous=previous is not None):
                converted = report(
                    checks(over_extra),
                    None
                    if previous is None
                    else json.dumps(
                        previous,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                attempt = converted["attempts"][-1]
                self.assertEqual(
                    attempt["safe_error"]["code"],
                    "observation_limit",
                )
                self.assertEqual(
                    attempt["unexpected_check_count"],
                    projected_count,
                )
                self.assertEqual(
                    attempt["unexpected_checks_sha256"],
                    projected_digest,
                )
                self.assertEqual(attempt["expected_checks"], [])
                self.assertIsNone(attempt["retry"])
                runtime.validate_ci_report(converted)

    def test_v2_ordinary_path_materializes_typed_classification_value(self) -> None:
        watcher = load_template_module(
            CI_WATCH,
            "ci_typed_classification_materialization",
        )
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_typed_classification_materialization_runtime",
        )
        expected = self.expected_contract()
        payload = {
            "pr_url": "https://github.com/example/repo/pull/1",
            "expected_ci_checks": json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "expected_ci_checks_sha256": runtime.expected_ci_checks_sha256(expected),
            "runtime_source_envelope_digest": "a" * 64,
        }
        receipt = runtime.RejectedObservationReceipt(
            "projected_rows",
            1,
            runtime.canonical_sha256([]),
        )
        calls = 0
        self_check = self.observed_check()
        normalized_check = watcher.v2_ci_observation([self_check])[0]

        class TypedClassification:
            state = "ordinary"
            value = None
            projected_observations = receipt

            def materialize_value(self) -> dict[str, object]:
                nonlocal calls
                calls += 1
                return {
                    "transition": "all_expected_checks_terminal_green",
                    "expected_checks": [normalized_check],
                    "unexpected_check_count": 0,
                    "unexpected_checks_sha256": runtime.canonical_sha256([]),
                }

        classification = TypedClassification()
        with mock.patch.object(
            watcher,
            "runtime_contracts",
            return_value=runtime,
        ), mock.patch.object(
            runtime,
            "classify_expected_ci_checks_with_receipt",
            return_value=classification,
        ):
            transition, report_json, reason = watcher.v2_ci_report(
                payload,
                {"headRefOid": "1" * 40, "baseRefOid": "2" * 40},
                [self_check],
                watch_exit=0,
            )
        self.assertEqual(calls, 1)
        self.assertEqual(
            (transition, reason),
            ("ci_watch_passed", "all_expected_checks_terminal_green"),
        )
        assert report_json is not None
        report = json.loads(report_json)
        self.assertEqual(
            report["attempts"][-1]["expected_checks"],
            [normalized_check],
        )
        runtime.validate_ci_report(report)

    def test_v2_grammar_and_projected_hard_paths_use_typed_builders(self) -> None:
        watcher = load_template_module(
            CI_WATCH,
            "ci_typed_grammar_hard_paths",
        )
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_typed_grammar_hard_paths_runtime",
        )
        expected = self.expected_contract()
        payload = {
            "pr_url": "https://github.com/example/repo/pull/1",
            "expected_ci_checks": json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "expected_ci_checks_sha256": runtime.expected_ci_checks_sha256(expected),
            "runtime_source_envelope_digest": expected[
                "runtime_source_envelope_digest"
            ],
        }
        malformed = [{"workflow_name": "bad"} for _ in range(10001)]
        projected = runtime._project_observed_rows(malformed)
        with mock.patch.object(
            watcher,
            "runtime_contracts",
            return_value=runtime,
        ), mock.patch.object(
            watcher,
            "v2_ci_observation",
            return_value=malformed,
        ), mock.patch.object(
            runtime,
            "make_report_invalid_attempt",
            side_effect=AssertionError("legacy builder was called"),
        ):
            transition, report_json, reason = watcher.v2_ci_report(
                payload,
                {"headRefOid": "1" * 40, "baseRefOid": "2" * 40},
                [],
                watch_exit=1,
            )
        self.assertEqual((transition, reason), ("ci_watch_failed", "report_invalid"))
        grammar_report = json.loads(report_json)
        grammar_attempt = grammar_report["attempts"][-1]
        self.assertEqual(grammar_attempt["unexpected_check_count"], 10001)
        self.assertEqual(
            grammar_attempt["unexpected_checks_sha256"],
            runtime.canonical_sha256(projected),
        )
        runtime.validate_ci_report(grammar_report)

        wide = [
            {
                "workflow_name": "W" * 256,
                "check_name": "C" * 256,
                "bucket": "pass",
                "state": "SUCCESS",
                "link": None,
            }
            for _ in range(10000)
        ]
        with mock.patch.object(
            watcher,
            "runtime_contracts",
            return_value=runtime,
        ), mock.patch.object(
            watcher,
            "v2_ci_observation",
            return_value=wide,
        ), mock.patch.object(
            runtime,
            "make_report_invalid_attempt",
            side_effect=AssertionError("legacy builder was called"),
        ):
            transition, report_json, reason = watcher.v2_ci_report(
                payload,
                {"headRefOid": "1" * 40, "baseRefOid": "2" * 40},
                [],
                watch_exit=1,
            )
        self.assertEqual((transition, reason), ("ci_watch_failed", "report_invalid"))
        hard_report = json.loads(report_json)
        hard_attempt = hard_report["attempts"][-1]
        self.assertEqual(hard_attempt["safe_error"]["code"], "hard_limit")
        self.assertEqual(
            hard_attempt["unexpected_checks_sha256"],
            runtime.canonical_sha256([]),
        )
        runtime.validate_ci_report(hard_report)

    def test_expected_ci_classification_matrix_and_history_bounds(self) -> None:
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_classification_matrix",
        )
        expected = {
            "schema": "github-ci-expected-checks-v1",
            "repository": "example/repo",
            "project_commit": "0" * 40,
            "runtime_source_envelope_digest": "a" * 64,
            "checks": [
                {
                    "workflow_name": "PR",
                    "check_name": "unit",
                    "allow_skipped": False,
                }
            ],
        }
        digest = runtime.expected_ci_checks_sha256(expected)
        base = {
            "workflow_name": "PR",
            "check_name": "unit",
            "bucket": "pass",
            "state": "SUCCESS",
            "link": None,
        }
        cases = {
            "green": ([base], "all_expected_checks_terminal_green"),
            "missing": ([], "no_checks_reported"),
            "failed": ([{**base, "bucket": "fail", "state": "FAILURE"}], "expected_check_failed"),
            "skipped": ([{**base, "bucket": "skipping", "state": "SKIPPED"}], "expected_check_skipped"),
            "pending": ([{**base, "bucket": "pending", "state": "QUEUED"}], "pending_limit"),
            "duplicate": ([base, base], "duplicate_observed_check"),
            "unexpected": ([{**base, "check_name": "other"}], "expected_check_missing"),
        }
        for label, (observed, transition) in cases.items():
            with self.subTest(label=label):
                result = runtime.classify_expected_ci_checks(
                    expected,
                    observed,
                    current_repository="example/repo",
                    current_head_oid="0" * 40,
                    runtime_source_envelope_digest="a" * 64,
                    expected_checks_digest=digest,
                )
                self.assertEqual(result["transition"], transition)
        self.assertEqual(
            runtime.classify_expected_ci_checks(
                expected,
                [base],
                current_repository="other/repo",
                current_head_oid="0" * 40,
                runtime_source_envelope_digest="a" * 64,
                expected_checks_digest=digest,
            )["transition"],
            "expected_contract_invalid",
        )
        self.assertEqual(
            runtime.classify_expected_ci_checks(
                expected,
                [base],
                current_repository="example/repo",
                current_head_oid="1" * 40,
                runtime_source_envelope_digest="a" * 64,
                expected_checks_digest=digest,
            )["transition"],
            "source_changed",
        )
        self.assertEqual(
            runtime.classify_expected_ci_checks(
                expected,
                [base],
                current_repository="example/repo",
                current_head_oid="0" * 40,
                runtime_source_envelope_digest="b" * 64,
                expected_checks_digest=digest,
            )["transition"],
            "expected_contract_invalid",
        )
        self.assertEqual(
            runtime.classify_expected_ci_checks(
                expected,
                [base],
                current_repository="example/repo",
                current_head_oid="0" * 40,
                runtime_source_envelope_digest="a" * 64,
                expected_checks_digest="b" * 64,
            )["transition"],
            "expected_contract_invalid",
        )
        attempt = runtime.make_report_invalid_attempt(
            sequence=1,
            head_oid="0" * 40,
            base_oid="1" * 40,
            raw_observations=[base],
        )
        report = runtime.build_ci_report(
            mode="expected-v1",
            repository="example/repo",
            pull_number=1,
            runtime_source_envelope_digest="a" * 64,
            expected_ci_checks_sha256=digest,
            attempts=[attempt],
        )
        for sequence in range(2, 10):
            report = runtime.append_ci_report_attempt(
                report,
                {
                    **attempt,
                    "sequence": sequence,
                },
            )
        self.assertEqual(len(report["attempts"]), 8)
        self.assertEqual(report["discarded_attempt_count"], 1)
        runtime.validate_ci_report(report)

    def test_v2_query_failure_preserves_cursor_and_output_digests(self) -> None:
        root = self.create_repository()
        expected = self.expected_contract()
        result = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check()],
            expected=expected,
            pr_feedback_cursor="uninitialized",
            gh_script="#!/bin/sh\nprintf out\nprintf err >&2\nexit 7\n",
            max_errors=1,
        )
        self.assertEqual(result["transition"], "ci_watch_failed")
        self.assertEqual(result["pr_feedback_cursor"], "uninitialized")
        report = json.loads(result["ci_report"])
        error = report["attempts"][-1]["safe_error"]
        self.assertEqual(error["code"], "github_query_failed")
        self.assertEqual(error["exit_code"], 7)
        self.assertEqual(error["stdout_sha256"], hashlib.sha256(b"out").hexdigest())
        self.assertEqual(error["stderr_sha256"], hashlib.sha256(b"err").hexdigest())
        timed_out = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check()],
            expected=expected,
            pr_feedback_cursor="uninitialized",
            gh_script="#!/bin/sh\nsleep 2\n",
            max_errors=1,
            query_timeout=1,
        )
        timeout_error = json.loads(timed_out["ci_report"])["attempts"][-1][
            "safe_error"
        ]
        self.assertEqual(timeout_error["code"], "github_query_failed")
        self.assertIsNone(timeout_error["exit_code"])

    def test_v2_github_query_output_cap_terminates_the_process_group(self) -> None:
        root = self.create_repository()
        ready_file = root / "ready"
        pid_file = root / "pids"
        result = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check()],
            expected=self.expected_contract(),
            pr_feedback_cursor="uninitialized",
            gh_script=(
                "#!/bin/sh\n"
                "(trap '' TERM; sleep 30) &\n"
                "child=$!\n"
                "printf '%s %s\\n' \"$$\" \"$child\" > \"$KENT_PID_FILE\"\n"
                "printf ready > \"$KENT_READY_FILE\"\n"
                "head -c 4194305 /dev/zero\n"
                "wait\n"
            ),
            max_errors=1,
            query_timeout=10,
            ready_file=ready_file,
            pid_file=pid_file,
        )
        self.assertTrue(ready_file.exists())
        assert_pids_gone(self, pid_file)
        self.assertEqual(result["transition"], "ci_watch_failed")
        error = json.loads(result["ci_report"])["attempts"][-1]["safe_error"]
        self.assertEqual(error["code"], "hard_limit")
        self.assertEqual(
            error["stdout_sha256"],
            hashlib.sha256(b"\0" * (4 * 1024 * 1024 + 1)).hexdigest(),
        )

    def test_v2_closed_query_pipes_still_enforce_deadline(self) -> None:
        root = self.create_repository()
        ready_file = root / "ready"
        pid_file = root / "pids"
        started = time.monotonic()
        result = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check()],
            expected=self.expected_contract(),
            pr_feedback_cursor="uninitialized",
            gh_script=(
                "#!/bin/sh\n"
                "(sleep 30) >/dev/null 2>&1 &\n"
                "child=$!\n"
                "printf '%s %s\\n' \"$$\" \"$child\" > \"$KENT_PID_FILE\"\n"
                "printf ready > \"$KENT_READY_FILE\"\n"
                "exec >/dev/null 2>&1\n"
                "sleep 2\n"
            ),
            max_errors=1,
            query_timeout=1,
            ready_file=ready_file,
            pid_file=pid_file,
        )
        self.assertLess(time.monotonic() - started, 2.0)
        assert_pids_gone(self, pid_file)
        self.assertEqual(result["transition"], "ci_watch_failed")
        report = json.loads(result["ci_report"])
        self.assertEqual(
            report["attempts"][-1]["safe_error"]["code"],
            "github_query_failed",
        )

    def test_v2_orphaned_query_group_is_reaped_after_parent_exit(self) -> None:
        root = self.create_repository()
        ready_file = root / "ready"
        pid_file = root / "pids"
        result = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check()],
            expected=self.expected_contract(),
            gh_script=(
                "#!/bin/sh\n"
                "(trap '' TERM; sleep 30) &\n"
                "child=$!\n"
                "printf '%s\\n' \"$child\" > \"$KENT_PID_FILE\"\n"
                "printf ready > \"$KENT_READY_FILE\"\n"
                "exit 0\n"
            ),
            max_errors=1,
            query_timeout=1,
            ready_file=ready_file,
            pid_file=pid_file,
        )
        self.assertEqual(result["transition"], "ci_watch_failed")
        self.assertEqual(
            json.loads(result["ci_report"])["attempts"][-1]["safe_error"][
                "code"
            ],
            "github_query_failed",
        )
        assert_pids_gone(self, pid_file)

    def test_v2_success_orphan_with_closed_pipes_is_rejected_and_reaped(
        self,
    ) -> None:
        root = self.create_repository()
        ready_file = root / "ready"
        pid_file = root / "pids"
        result = self.watch(
            root,
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check()],
            expected=self.expected_contract(),
            gh_script=(
                "#!/bin/sh\n"
                "(trap '' TERM; "
                "exec >/dev/null 2>&1; "
                "printf ready > \"$KENT_READY_FILE\"; "
                "while :; do sleep 1; done) &\n"
                "child=$!\n"
                "printf '%s %s\\n' \"$$\" \"$child\" > \"$KENT_PID_FILE\"\n"
                "while [ ! -f \"$KENT_READY_FILE\" ]; do sleep 0.01; done\n"
                "exit 0\n"
            ),
            max_errors=1,
            query_timeout=1,
            ready_file=ready_file,
            pid_file=pid_file,
        )
        self.assertEqual(result["transition"], "ci_watch_failed")
        self.assertEqual(
            json.loads(result["ci_report"])["attempts"][-1]["safe_error"][
                "code"
            ],
            "github_query_failed",
        )
        assert_pids_gone(self, pid_file)

    def test_v2_cursor_prevalidation_allowed_skipped_and_subset_missing(self) -> None:
        malformed = self.watch(
            self.create_repository(),
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check()],
            expected=self.expected_contract(),
            pr_feedback_cursor="{not-json",
        )
        self.assertEqual(malformed["transition"], "ci_watch_failed")
        self.assertEqual(
            json.loads(malformed["ci_report"])["attempts"][-1]["safe_error"][
                "code"
            ],
            "expected_contract_invalid",
        )

        skipped = self.watch(
            self.create_repository(),
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check(bucket="skipping", state="SKIPPED")],
            expected=self.expected_contract(
                checks=[
                    {
                        "workflow_name": "PR",
                        "check_name": "unit",
                        "allow_skipped": True,
                    }
                ]
            ),
            pr_feedback_cursor="uninitialized",
        )
        self.assertEqual(skipped["transition"], "ci_watch_passed")
        self.assertEqual(
            json.loads(skipped["ci_report"])["attempts"][-1]["reason"],
            "all_expected_checks_terminal_green",
        )

        subset = self.watch(
            self.create_repository(),
            pr_state={
                "state": "OPEN",
                "headRefOid": "1" * 40,
                "baseRefOid": "2" * 40,
                "url": "https://github.com/example/repo/pull/1",
            },
            checks=[self.observed_check()],
            expected=self.expected_contract(
                checks=[
                    {
                        "workflow_name": "PR",
                        "check_name": "lint",
                        "allow_skipped": False,
                    },
                    {
                        "workflow_name": "PR",
                        "check_name": "unit",
                        "allow_skipped": False,
                    },
                ]
            ),
            pr_feedback_cursor="uninitialized",
        )
        self.assertEqual(subset["transition"], "ci_watch_failed")
        self.assertEqual(
            [item["reason"] for item in json.loads(subset["ci_report"])["attempts"]],
            ["expected_check_missing", "pending_limit"],
        )

    def test_v2_history_rejects_malformed_and_proves_discarded_digest(self) -> None:
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_history_contract_matrix",
        )
        observed = {
            "workflow_name": "PR",
            "check_name": "unit",
            "bucket": "pass",
            "state": "SUCCESS",
            "link": None,
        }
        discarded = runtime.make_report_invalid_attempt(
            sequence=1,
            head_oid="0" * 40,
            base_oid="1" * 40,
            raw_observations=[observed],
        )
        current = {
            **discarded,
            "sequence": 2,
            "reason": "all_expected_checks_terminal_green",
            "expected_checks": [observed],
            "unexpected_check_count": 0,
            "unexpected_checks_sha256": runtime.canonical_sha256([]),
            "safe_error": None,
        }
        report = runtime.build_ci_report(
            mode="expected-v1",
            repository="example/repo",
            pull_number=1,
            runtime_source_envelope_digest="a" * 64,
            expected_ci_checks_sha256="b" * 64,
            attempts=[current],
            discarded_attempts=[discarded],
        )
        self.assertEqual(
            report["discarded_attempts_sha256"],
            runtime.discarded_attempt_digest("0" * 64, discarded),
        )
        runtime.validate_ci_report_history(report, [discarded])
        invalid = {
            "unknown": {**report, "unknown": True},
            "noncontiguous": {
                **report,
                "attempts": [{**report["attempts"][0], "sequence": 3}],
            },
            "count": {**report, "discarded_attempt_count": 0},
            "digest": {**report, "discarded_attempts_sha256": "c" * 64},
        }
        for label, value in invalid.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    runtime.validate_ci_report_history(value, [discarded])

    def test_v2_attempt_and_report_byte_limits_are_enforced(self) -> None:
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "ci_report_size_limits",
        )

        def attempt(sequence: int, rows: list[dict[str, object]]) -> dict[str, object]:
            return {
                "sequence": sequence,
                "head_oid": "0" * 40,
                "base_oid": "1" * 40,
                "reason": "all_expected_checks_terminal_green",
                "watcher_exit_code": 0,
                "expected_checks": rows,
                "unexpected_check_count": 0,
                "unexpected_checks_sha256": runtime.canonical_sha256([]),
                "retry": None,
                "safe_error": None,
            }

        base = [self.observed_check()]
        bad_field = [{**base[0], "link": "https://e.invalid/" + "x" * 2040}]
        with self.assertRaises(ValueError):
            runtime.validate_ci_report(
                {
                    "schema": "github-ci-report-v2",
                    "mode": "expected-v1",
                    "repository": "example/repo",
                    "pull_number": 1,
                    "runtime_source_envelope_digest": "a" * 64,
                    "expected_ci_checks_sha256": "b" * 64,
                    "discarded_attempt_count": 0,
                    "discarded_attempts_sha256": "0" * 64,
                    "attempts": [attempt(1, bad_field)],
                }
            )
        wide_attempt = [
            {
                **self.observed_check(name="check-{:03d}".format(index)),
                "link": "https://e.invalid/" + "x" * 2030,
            }
            for index in range(30)
        ]
        with self.assertRaises(ValueError):
            runtime.validate_ci_report(
                {
                    "schema": "github-ci-report-v2",
                    "mode": "expected-v1",
                    "repository": "example/repo",
                    "pull_number": 1,
                    "runtime_source_envelope_digest": "a" * 64,
                    "expected_ci_checks_sha256": "b" * 64,
                    "discarded_attempt_count": 0,
                    "discarded_attempts_sha256": "0" * 64,
                    "attempts": [attempt(1, wide_attempt)],
                }
            )
        wide_report_rows = [
            {
                **self.observed_check(
                    name="check-{:03d}{}".format(index, "x" * 245)
                ),
                "link": None,
            }
            for index in range(100)
        ]
        oversized = {
            "schema": "github-ci-report-v2",
            "mode": "expected-v1",
            "repository": "example/repo",
            "pull_number": 1,
            "runtime_source_envelope_digest": "a" * 64,
            "expected_ci_checks_sha256": "b" * 64,
            "discarded_attempt_count": 0,
            "discarded_attempts_sha256": "0" * 64,
            "attempts": [
                attempt(1, wide_report_rows),
                attempt(2, wide_report_rows),
                attempt(3, wide_report_rows),
            ],
        }
        self.assertGreater(
            len(runtime.canonical_bytes(oversized)),
            runtime.MAX_CI_REPORT_BYTES,
        )
        with self.assertRaises(ValueError):
            runtime.validate_ci_report(oversized)


class GitHubPrWatchTest(GitRepositoryTest):
    def fake_gh(
        self,
        root: Path,
        payload: dict[str, object],
        *,
        feedback_script: str | None = None,
    ) -> Path:
        payload_path = root / "pr-state.json"
        payload_path.write_text(json.dumps(payload))
        executable = root / "gh"
        executable.write_text(
            feedback_script
            or (
                "#!/bin/sh\n"
                'case "$1 $2" in\n'
                '  "pr view") cat "$KENT_TEST_PR_STATE" ;;\n'
                '  "api graphql") cat "$KENT_TEST_GRAPHQL" ;;\n'
                '  "api "*)\n'
                '    case "$*" in\n'
                '      *issues/*/comments*) cat "$KENT_TEST_ISSUES" ;;\n'
                '      *pulls/*/reviews*) cat "$KENT_TEST_REVIEWS" ;;\n'
                '      *pulls/*/comments*) cat "$KENT_TEST_COMMENTS" ;;\n'
                "    esac ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n"
            )
        )
        executable.chmod(0o755)
        return executable

    def watch(
        self,
        root: Path,
        state: dict[str, object],
        *,
        head: str = "abc123",
        base: str = "base123",
        cursor: str | None = None,
        feedback: dict[str, object] | None = None,
        feedback_script: str | None = None,
        max_polls: int = 1,
        max_errors: int = 5,
        query_timeout: int = 60,
        ready_file: Path | None = None,
        pid_file: Path | None = None,
    ) -> dict[str, object]:
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        watcher = scripts / "workflow-wait-github-pr"
        shutil.copyfile(PR_WATCH, watcher)
        watcher.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        feedback = feedback or {}
        (root / "issues.json").write_text(
            json.dumps(feedback.get("issue_comments", []))
        )
        (root / "reviews.json").write_text(
            json.dumps(feedback.get("reviews", []))
        )
        (root / "comments.json").write_text(
            json.dumps(feedback.get("review_comments", []))
        )
        (root / "graphql.json").write_text(
            json.dumps(
                feedback.get(
                    "graphql",
                    [
                        {
                            "data": {
                                "repository": {
                                    "pullRequest": {
                                        "reviewThreads": {
                                            "nodes": [],
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    ],
                )
            )
        )
        fake_gh = self.fake_gh(
            root,
            state,
            feedback_script=feedback_script,
        )
        workflow_input = {
            "workspace_path": str(root),
            "pr_url": "https://github.com/example/repo/pull/1",
            "branch_name": "TASK-1",
            "merge_strategy": "rebase",
            "pr_head_oid": head,
            "pr_base_oid": base,
            **(
                {"pr_feedback_cursor": cursor}
                if cursor is not None
                else {}
            ),
        }
        environment = {
            **os.environ,
            "PATH": f"{root}:{os.environ.get('PATH', '')}",
            "KENT_TEST_PR_STATE": str(root / "pr-state.json"),
            "KENT_TEST_ISSUES": str(root / "issues.json"),
            "KENT_TEST_REVIEWS": str(root / "reviews.json"),
            "KENT_TEST_COMMENTS": str(root / "comments.json"),
            "KENT_TEST_GRAPHQL": str(root / "graphql.json"),
            "KENT_PR_WATCH_TEST_MODE": "1",
            "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
            "KENT_PR_WATCH_MAX_POLLS": str(max_polls),
            "KENT_PR_WATCH_MAX_ERRORS": str(max_errors),
            "KENT_PR_WATCH_QUERY_TIMEOUT_SECONDS": str(query_timeout),
        }
        if ready_file is not None:
            environment["KENT_READY_FILE"] = str(ready_file)
        if pid_file is not None:
            environment["KENT_PID_FILE"] = str(pid_file)
        if ready_file is None:
            result = subprocess.run(
                [str(watcher)],
                cwd=root,
                input=json.dumps(workflow_input),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
            )
        else:
            process = subprocess.Popen(
                [str(watcher)],
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            ready_deadline = time.monotonic() + 5
            while (
                not ready_file.exists()
                and process.poll() is None
                and time.monotonic() < ready_deadline
            ):
                time.sleep(0.01)
            self.assertTrue(ready_file.exists())
            stdout, stderr = process.communicate(json.dumps(workflow_input))
            result = subprocess.CompletedProcess(
                process.args,
                process.returncode,
                stdout,
                stderr,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_unicode_feedback_cursor_round_trips_through_pr_consumer(
        self,
    ) -> None:
        watcher = load_template_module(
            PR_WATCH,
            "pr_unicode_feedback_cursor_consumer",
        )
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "pr_unicode_feedback_cursor_runtime",
        )
        cursor = runtime.make_pr_feedback_cursor(
            repository="example/repo",
            pull_number=1,
            head_oid="0" * 40,
            base_oid="1" * 40,
            pr_state="OPEN",
            review_decision="",
            merge_state_status="CLEAN",
            checks=[],
            items=[
                {
                    "kind": "review_thread",
                    "id": "thread-1",
                    "resolved": False,
                    "outdated": False,
                    "path": "src/é.kt",
                    "current_line": 12,
                    "current_start_line": 10,
                    "original_line": 20,
                    "original_start_line": 18,
                    "subject_type": "LINE",
                    "comment_ids": ["thread-comment-1"],
                }
            ],
        )
        raw = runtime.canonical_bytes(cursor).decode("utf-8")
        self.assertIn("src/é.kt", raw)
        with mock.patch.object(
            watcher,
            "runtime_contracts",
            return_value=runtime,
        ):
            carrier = watcher.cursor_values({"pr_feedback_cursor": raw})
        self.assertEqual(carrier, {"pr_feedback_cursor": raw})
        self.assertEqual(
            runtime.parse_canonical_json(
                carrier["pr_feedback_cursor"],
                label="PR feedback cursor",
                max_bytes=runtime.MAX_FEEDBACK_BYTES,
            ),
            cursor,
        )

    def test_v2_overdepth_feedback_cursor_fails_closed_without_child(self) -> None:
        root = self.create_repository()
        marker = root / "github-child-invoked"
        overdepth = "[" * 101 + "0" + "]" * 101
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
            cursor=overdepth,
            feedback_script=(
                "#!/bin/sh\n"
                f"echo invoked > {str(marker)!r}\n"
                "exit 0\n"
            ),
        )
        self.assertEqual(result["transition"], "merge_watch_state_changed")
        report = json.loads(result["pr_report"])
        self.assertEqual(report["reason"], "pull_request_feedback_invalid")
        self.assertEqual(result["pr_feedback_cursor"], "uninitialized")
        self.assertFalse(marker.exists())
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(overdepth, serialized)
        self.assertNotIn("RecursionError", serialized)

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

    def test_v2_feedback_watcher_materializes_deduplicates_and_bounds(self) -> None:
        state = {
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
            "headRefName": "TASK-1",
            "headRefOid": "0" * 40,
            "baseRefName": "main",
            "baseRefOid": "1" * 40,
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
            "url": "https://github.com/example/repo/pull/1",
        }
        empty = {"issue_comments": [], "reviews": [], "review_comments": []}
        root = self.create_repository()
        baseline = self.watch(
            root,
            state,
            head="0" * 40,
            base="1" * 40,
            cursor="uninitialized",
            feedback=empty,
        )
        self.assertEqual(baseline["transition"], "merge_watch_still_waiting")
        empty_cursor = baseline["pr_feedback_cursor"]
        self.assertEqual(json.loads(empty_cursor)["item_count"], 0)

        issue = {
            "id": "issue-1",
            "body": "hello",
            "created_at": "2026-08-20T00:00:00Z",
            "updated_at": "2026-08-20T00:00:00Z",
            "user": {"login": "alice"},
        }
        changed = self.watch(
            root,
            state,
            head="0" * 40,
            base="1" * 40,
            cursor=empty_cursor,
            feedback={"issue_comments": [issue]},
        )
        self.assertEqual(changed["transition"], "merge_watch_state_changed")
        self.assertIn("pull_request_feedback_changed", changed["pr_report"])
        materialized = changed["pr_feedback_cursor"]
        unchanged = self.watch(
            root,
            state,
            head="0" * 40,
            base="1" * 40,
            cursor=materialized,
            feedback={"issue_comments": [issue]},
        )
        self.assertEqual(unchanged["transition"], "merge_watch_still_waiting")

        many = [
            {
                **issue,
                "id": "issue-{:03d}".format(index),
            }
            for index in range(101)
        ]
        digest = self.watch(
            root,
            state,
            head="0" * 40,
            base="1" * 40,
            cursor="uninitialized",
            feedback={"issue_comments": many},
        )
        self.assertEqual(json.loads(digest["pr_feedback_cursor"])["mode"], "digest_only")
        digest_same = self.watch(
            root,
            state,
            head="0" * 40,
            base="1" * 40,
            cursor=digest["pr_feedback_cursor"],
            feedback={"issue_comments": many},
        )
        self.assertEqual(digest_same["transition"], "merge_watch_still_waiting")
        edited = [{**item, "body": "edited"} if item["id"] == "issue-000" else item for item in many]
        digest_changed = self.watch(
            root,
            state,
            head="0" * 40,
            base="1" * 40,
            cursor=digest["pr_feedback_cursor"],
            feedback={"issue_comments": edited},
        )
        self.assertEqual(digest_changed["transition"], "merge_watch_state_changed")

        failed = self.watch(
            root,
            state,
            head="0" * 40,
            base="1" * 40,
            cursor=materialized,
            feedback_script=(
                "#!/bin/sh\n"
                'if [ "$1 $2" = "pr view" ]; then cat "$KENT_TEST_PR_STATE"; exit 0; fi\n'
                "printf out\nprintf err >&2\nexit 7\n"
            ),
            max_errors=1,
        )
        self.assertEqual(failed["transition"], "merge_watch_state_changed")
        failed_report = json.loads(failed["pr_report"])
        self.assertEqual(failed_report["safe_error"]["code"], "github_query_failed")

        hard_limit = self.watch(
            root,
            state,
            head="0" * 40,
            base="1" * 40,
            cursor="uninitialized",
            feedback={"issue_comments": [
                {**issue, "id": "issue-{:04d}".format(index)}
                for index in range(1001)
            ]},
        )
        self.assertEqual(hard_limit["transition"], "merge_watch_state_changed")
        self.assertIn(
            "pull_request_feedback_hard_limit",
            hard_limit["pr_report"],
        )

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
        report = json.loads(payload["pr_report"])
        self.assertEqual(report["safe_error"]["code"], "github_query_failed")
        self.assertEqual(
            report["safe_error"]["stdout_sha256"],
            hashlib.sha256(b"").hexdigest(),
        )

    def test_github_query_output_cap_terminates_the_process_group(self) -> None:
        root = self.create_repository()
        ready_file = root / "ready"
        pid_file = root / "pids"
        fake_gh = root / "gh-cap"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "(trap '' TERM; sleep 30) &\n"
            "child=$!\n"
            "printf '%s %s\\n' \"$$\" \"$child\" > \"$KENT_PID_FILE\"\n"
            "printf ready > \"$KENT_READY_FILE\"\n"
            "head -c 4194305 /dev/zero\n"
            "wait\n"
        )
        fake_gh.chmod(0o755)
        process = subprocess.Popen(
            [str(PR_WATCH)],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_READY_FILE": str(ready_file),
                "KENT_PID_FILE": str(pid_file),
                "KENT_PR_WATCH_TEST_MODE": "1",
                "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
                "KENT_PR_WATCH_MAX_POLLS": "1",
                "KENT_PR_WATCH_MAX_ERRORS": "1",
                "KENT_PR_WATCH_QUERY_TIMEOUT_SECONDS": "10",
            },
        )
        process.stdin.write(
            json.dumps(
                {
                    "workspace_path": str(root),
                    "pr_url": "https://github.com/example/repo/pull/1",
                    "branch_name": "TASK-1",
                    "merge_strategy": "rebase",
                    "pr_head_oid": "abc123",
                    "pr_base_oid": "base123",
                }
            )
        )
        process.stdin.close()
        process.stdin = None
        ready_deadline = time.monotonic() + 5
        while (
            not ready_file.exists()
            and process.poll() is None
            and time.monotonic() < ready_deadline
        ):
            time.sleep(0.01)
        self.assertTrue(ready_file.exists())
        stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        payload = json.loads(stdout)
        report = json.loads(payload["pr_report"])
        assert_pids_gone(self, pid_file)
        self.assertEqual(report["safe_error"]["code"], "hard_limit")
        self.assertEqual(
            report["safe_error"]["stdout_sha256"],
            hashlib.sha256(b"\0" * (4 * 1024 * 1024 + 1)).hexdigest(),
        )

    def test_closed_query_pipes_still_enforce_deadline(self) -> None:
        root = self.create_repository()
        ready_file = root / "ready"
        pid_file = root / "pids"
        fake_gh = root / "gh-closed"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "(sleep 30) >/dev/null 2>&1 &\n"
            "child=$!\n"
            "printf '%s %s\\n' \"$$\" \"$child\" > \"$KENT_PID_FILE\"\n"
            "printf ready > \"$KENT_READY_FILE\"\n"
            "exec >/dev/null 2>&1\n"
            "sleep 2\n"
        )
        fake_gh.chmod(0o755)
        started = time.monotonic()
        process = subprocess.Popen(
            [str(PR_WATCH)],
            cwd=root,
            stdin=subprocess.PIPE,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_READY_FILE": str(ready_file),
                "KENT_PID_FILE": str(pid_file),
                "KENT_PR_WATCH_TEST_MODE": "1",
                "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
                "KENT_PR_WATCH_MAX_POLLS": "1",
                "KENT_PR_WATCH_MAX_ERRORS": "1",
                "KENT_PR_WATCH_QUERY_TIMEOUT_SECONDS": "1",
            },
        )
        process.stdin.write(
            json.dumps(
                {
                    "workspace_path": str(root),
                    "pr_url": "https://github.com/example/repo/pull/1",
                    "branch_name": "TASK-1",
                    "merge_strategy": "rebase",
                    "pr_head_oid": "abc123",
                    "pr_base_oid": "base123",
                }
            )
        )
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
        ready_deadline = time.monotonic() + 5
        while (
            not ready_file.exists()
            and process.poll() is None
            and time.monotonic() < ready_deadline
        ):
            time.sleep(0.01)
        self.assertTrue(ready_file.exists())
        stdout, stderr = process.communicate()
        result = subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout,
            stderr,
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "merge_watch_state_changed")
        report = json.loads(payload["pr_report"])
        self.assertEqual(report["safe_error"]["code"], "github_query_failed")
        assert_pids_gone(self, pid_file)

    def test_orphaned_query_group_is_reaped_after_parent_exit(self) -> None:
        root = self.create_repository()
        ready_file = root / "ready"
        pid_file = root / "pids"
        fake_gh = root / "gh-orphan"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "(trap '' TERM; sleep 30) &\n"
            "child=$!\n"
            "printf '%s\\n' \"$child\" > \"$KENT_PID_FILE\"\n"
            "printf ready > \"$KENT_READY_FILE\"\n"
            "exit 0\n"
        )
        fake_gh.chmod(0o755)
        process = subprocess.Popen(
            [str(PR_WATCH)],
            cwd=root,
            stdin=subprocess.PIPE,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_READY_FILE": str(ready_file),
                "KENT_PID_FILE": str(pid_file),
                "KENT_PR_WATCH_TEST_MODE": "1",
                "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
                "KENT_PR_WATCH_MAX_POLLS": "1",
                "KENT_PR_WATCH_MAX_ERRORS": "1",
                "KENT_PR_WATCH_QUERY_TIMEOUT_SECONDS": "1",
            },
        )
        process.stdin.write(
            json.dumps(
                {
                    "workspace_path": str(root),
                    "pr_url": "https://github.com/example/repo/pull/1",
                    "branch_name": "TASK-1",
                    "merge_strategy": "rebase",
                    "pr_head_oid": "abc123",
                    "pr_base_oid": "base123",
                }
            )
        )
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
        ready_deadline = time.monotonic() + 5
        while (
            not ready_file.exists()
            and process.poll() is None
            and time.monotonic() < ready_deadline
        ):
            time.sleep(0.01)
        self.assertTrue(ready_file.exists())
        stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["transition"], "merge_watch_state_changed")
        self.assertEqual(
            json.loads(payload["pr_report"])["safe_error"]["code"],
            "github_query_failed",
        )
        assert_pids_gone(self, pid_file)

    def test_success_orphan_with_closed_pipes_is_rejected_and_reaped(
        self,
    ) -> None:
        root = self.create_repository()
        ready_file = root / "ready"
        pid_file = root / "pids"
        fake_gh = root / "gh-orphan"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "(trap '' TERM; "
            "exec >/dev/null 2>&1; "
            "printf ready > \"$KENT_READY_FILE\"; "
            "while :; do sleep 1; done) &\n"
            "child=$!\n"
            "printf '%s %s\\n' \"$$\" \"$child\" > \"$KENT_PID_FILE\"\n"
            "while [ ! -f \"$KENT_READY_FILE\" ]; do sleep 0.01; done\n"
            "exit 0\n"
        )
        fake_gh.chmod(0o755)
        process = subprocess.Popen(
            [str(PR_WATCH)],
            cwd=root,
            stdin=subprocess.PIPE,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_GH_BIN": str(fake_gh),
                "KENT_READY_FILE": str(ready_file),
                "KENT_PID_FILE": str(pid_file),
                "KENT_PR_WATCH_TEST_MODE": "1",
                "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
                "KENT_PR_WATCH_MAX_POLLS": "1",
                "KENT_PR_WATCH_MAX_ERRORS": "1",
                "KENT_PR_WATCH_QUERY_TIMEOUT_SECONDS": "1",
            },
        )
        process.stdin.write(
            json.dumps(
                {
                    "workspace_path": str(root),
                    "pr_url": "https://github.com/example/repo/pull/1",
                    "branch_name": "TASK-1",
                    "merge_strategy": "rebase",
                    "pr_head_oid": "abc123",
                    "pr_base_oid": "base123",
                }
            )
        )
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
        ready_deadline = time.monotonic() + 5
        while (
            not ready_file.exists()
            and process.poll() is None
            and time.monotonic() < ready_deadline
        ):
            time.sleep(0.01)
        self.assertTrue(ready_file.exists())
        stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["transition"], "merge_watch_state_changed")
        self.assertEqual(
            json.loads(payload["pr_report"])["safe_error"]["code"],
            "github_query_failed",
        )
        assert_pids_gone(self, pid_file)

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
    def install_v2_runtime_commands(self, root: Path) -> Path:
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("workflow-evidence-ledger", "workflow-task-janitor"):
            target = scripts / name
            shutil.copyfile(REPO_ROOT / "templates" / "project" / name, target)
            target.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        return scripts

    def make_v2_terminal_state(self, root: Path) -> tuple[Path, str]:
        context = root / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        scripts = self.install_v2_runtime_commands(root)
        evidence = scripts / "workflow-evidence-ledger"
        append = subprocess.run(
            [str(evidence), "append", "--task", "TASK-1", "--workspace", str(root)],
            input=json.dumps(
                {
                    "node_key": "implement",
                    "evidence_type": "implementation",
                    "summary": "terminal",
                    "artifacts": [],
                    "checks": [],
                    "decisions": [],
                    "context": {
                        "manifest_path": ".kent/context/implement.md",
                        "files_read": [],
                    },
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(append.returncode, 0, append.stderr)
        seal = subprocess.run(
            [str(evidence), "seal", "--task", "TASK-1", "--workspace", str(root)],
            input=json.dumps(
                {
                    "schema": "terminal-evidence-seal-request-v1",
                    "operation_report_digests": [],
                    "redaction": {
                        "status": "passed",
                        "report_sha256": "a" * 64,
                    },
                    "retention_class": "cleanup_report_only",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(seal.returncode, 0, seal.stderr)
        return scripts, json.loads(seal.stdout)["terminal_marker"]

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

    def make_managed_worktree(
        self,
        *,
        with_v2: bool,
    ) -> tuple[Path, Path, Path, str]:
        root = self.create_repository()
        (root / ".gitignore").write_text(
            "/.kent/runtime/\n/.kent/scripts/\n"
        )
        self.run_git(root, "add", ".gitignore")
        self.run_git(root, "commit", "-q", "-m", "ignore scripts")
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        if with_v2:
            context = worktree / ".kent" / "context"
            context.mkdir(parents=True)
            (context / "implement.md").write_text("manifest\n")
            self.run_git(worktree, "add", ".kent/context/implement.md")
            self.run_git(worktree, "commit", "-q", "-m", "context")
        scripts = self.install_v2_runtime_commands(worktree)
        marker = ""
        if with_v2:
            evidence = scripts / "workflow-evidence-ledger"
            append = subprocess.run(
                [
                    str(evidence),
                    "append",
                    "--task",
                    "TASK-1",
                    "--workspace",
                    str(worktree),
                ],
                input=json.dumps(
                    {
                        "node_key": "implement",
                        "evidence_type": "implementation",
                        "summary": "managed",
                        "artifacts": [],
                        "checks": [],
                        "decisions": [],
                        "context": {
                            "manifest_path": ".kent/context/implement.md",
                            "files_read": [],
                        },
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(append.returncode, 0, append.stderr)
            seal = subprocess.run(
                [
                    str(evidence),
                    "seal",
                    "--task",
                    "TASK-1",
                    "--workspace",
                    str(worktree),
                ],
                input=json.dumps(
                    {
                        "schema": "terminal-evidence-seal-request-v1",
                        "operation_report_digests": [],
                        "redaction": {
                            "status": "passed",
                            "report_sha256": "a" * 64,
                        },
                        "retention_class": "cleanup_report_only",
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(seal.returncode, 0, seal.stderr)
            marker = json.loads(seal.stdout)["terminal_marker"]
        return root, worktree, scripts, marker

    def managed_pr_environment(
        self,
        root: Path,
        worktree: Path,
        *,
        wrapper: Path,
    ) -> dict[str, str]:
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
        return {
            **os.environ,
            "KENT_GH_BIN": str(fake_gh),
            "KENT_TEST_PR_STATE": str(pr_state),
            "KENT_WORKTREE_WRAPPER": str(wrapper),
            "KENT_TEST_PRIMARY": str(root),
        }

    def no_op_completed_wrapper(self, root: Path) -> Path:
        wrapper = root / "kent-worktree-noop"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then\n"
            "  python3 -c 'import json, os; root = "
            "os.environ[\"KENT_TEST_PRIMARY\"]; print(json.dumps({"
            "\"target\": {\"EffectiveWorkdir\": root}, "
            "\"worktree\": {\"recorded_root\": root, "
            "\"observed_root\": root}}))'\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s\\n' '{\"kind\":\"completed\"}'\n"
        )
        wrapper.chmod(0o755)
        return wrapper

    def fake_tombstone_completed_wrapper(self, root: Path) -> Path:
        wrapper = root / "kent-worktree-fake-tombstone"
        wrapper.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "if [ \"$1\" = status ]; then\n"
            "  python3 -c 'import json, os; root = "
            "os.environ[\"KENT_TEST_PRIMARY\"]; print(json.dumps({"
            "\"target\": {\"EffectiveWorkdir\": root}, "
            "\"worktree\": {\"recorded_root\": root, "
            "\"observed_root\": root}}))'\n"
            "  exit 0\n"
            "fi\n"
            "for argument in \"$@\"; do target=\"$argument\"; done\n"
            "if [ -n \"${KENT_TEST_EXACT_TOMBSTONE:-}\" ]; then\n"
            "  mv \"$target/.kent/runtime/$KENT_TEST_EXACT_TOMBSTONE\" "
            "\"$target/.kent/runtime/.evidence-cleanup-fake\"\n"
            "fi\n"
            "printf '%s\\n' '{\"kind\":\"completed\"}'\n"
        )
        wrapper.chmod(0o755)
        return wrapper

    def test_managed_completed_wrapper_leaves_tombstone_and_blocks(self) -> None:
        root, worktree, scripts, marker = self.make_managed_worktree(with_v2=True)
        wrapper = self.no_op_completed_wrapper(root)
        environment = self.managed_pr_environment(
            root,
            worktree,
            wrapper=wrapper,
        )
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="TASK-1",
                pr_url="https://github.com/example/repo/pull/1",
                merge_report="merged",
                cleanup_mode="merged",
                cleanup_report=marker,
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("tombstone remains", payload["cleanup_report"])
        runtime_contracts = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "managed_exact_tombstone_test",
        )
        marker_value = json.loads(
            marker.removeprefix("TERMINAL_EVIDENCE_V1 ")
        )
        runtime = worktree / ".kent" / "runtime"
        self.assertTrue(
            (runtime / (
                ".evidence-cleanup-"
                + runtime_contracts.canonical_sha256(marker_value)
            )).exists()
        )
        self.assertTrue(worktree.exists())

    def test_managed_completed_wrapper_without_tombstone_reports_ambiguous_evidence(
        self,
    ) -> None:
        root, worktree, scripts, marker = self.make_managed_worktree(with_v2=True)
        wrapper = self.fake_tombstone_completed_wrapper(root)
        environment = self.managed_pr_environment(
            root,
            worktree,
            wrapper=wrapper,
        )
        runtime_contracts = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "managed_fake_tombstone_test",
        )
        marker_value = json.loads(
            marker.removeprefix("TERMINAL_EVIDENCE_V1 ")
        )
        environment["KENT_TEST_EXACT_TOMBSTONE"] = (
            ".evidence-cleanup-" + runtime_contracts.canonical_sha256(marker_value)
        )
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="TASK-1",
                pr_url="https://github.com/example/repo/pull/1",
                merge_report="merged",
                cleanup_mode="merged",
                cleanup_report=marker,
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("evidence loss is ambiguous", payload["cleanup_report"])
        self.assertTrue(worktree.exists())

    def test_managed_completed_wrapper_absent_workspace_acknowledges_cleanup(
        self,
    ) -> None:
        root, worktree, scripts, marker = self.make_managed_worktree(with_v2=True)
        wrapper = self.completed_wrapper(root)
        environment = self.managed_pr_environment(
            root,
            worktree,
            wrapper=wrapper,
        )
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            cwd=worktree,
            input=self.janitor_input(
                worktree,
                branch_name="TASK-1",
                pr_url="https://github.com/example/repo/pull/1",
                merge_report="merged",
                cleanup_mode="merged",
                cleanup_report=marker,
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["transition"],
            "task_janitor_done",
        )
        self.assertFalse(worktree.exists())

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
        self.assertEqual(payload["transition"], "task_janitor_blocked")
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

    def test_v2_cleanup_rejects_marker_for_a_different_task_before_mutation(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        wrong_marker = marker_line.replace(
            '"task_short_id":"TASK-1"',
            '"task_short_id":"TASK-2"',
        )
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(
                root,
                cleanup_report=wrong_marker,
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertTrue((root / ".kent/runtime/TASK-1").exists())
        self.assertFalse(
            (
                root
                / (
                    ".kent/runtime/.evidence-terminal-"
                    + hashlib.sha256(b"TASK-1").hexdigest()
                )
            ).exists()
        )

    def test_v2_empty_tombstone_retry_removes_only_empty_tombstone(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        active = runtime_dir / "TASK-1"
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        digest = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_empty_tombstone_test",
        ).canonical_sha256(marker)
        tombstone = runtime_dir / f".evidence-cleanup-{digest}"
        active.rename(tombstone)
        (tombstone / "evidence-ledger.jsonl").unlink()
        (runtime_dir / f".evidence-terminal-{hashlib.sha256(b'TASK-1').hexdigest()}").write_bytes(b"")
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root, cleanup_report=marker_line),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["transition"], "task_janitor_done")
        self.assertFalse(tombstone.exists())
        self.assertTrue(
            (runtime_dir / f".evidence-terminal-{hashlib.sha256(b'TASK-1').hexdigest()}").exists()
        )

    def test_v2_checkpoint_cleanup_uses_fix_smoke_ledger_order(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        active = runtime_dir / "TASK-1"
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_checkpoint_order_test",
        )
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        active.rename(tombstone)
        for name in ("fix-checkpoint.json", "smoke-checkpoint.json"):
            (tombstone / name).write_text("{}\n")
        sentinel = runtime_dir / f".evidence-terminal-{hashlib.sha256(b'TASK-1').hexdigest()}"
        sentinel.write_bytes(b"")
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_checkpoint_order_test",
        )
        phases: list[str] = []
        outcome = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
            _phase_hook=phases.append,
        )
        self.assertEqual(outcome[0], True)
        self.assertLess(
            phases.index("after_fix_checkpoint_unlink_fsync"),
            phases.index("after_smoke_checkpoint_unlink_fsync"),
        )
        self.assertIn("after_ledger_unlink_fsync", phases)
        self.assertTrue(sentinel.exists())

    def test_v2_checkpoint_subsets_cleanup_in_fixed_order(self) -> None:
        subsets = (
            (),
            ("fix-checkpoint.json",),
            ("smoke-checkpoint.json",),
            ("fix-checkpoint.json", "smoke-checkpoint.json"),
        )
        for subset in subsets:
            with self.subTest(subset=subset):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                runtime = load_template_module(
                    REPO_ROOT / "workflowkit" / "runtime.py",
                    "runtime_checkpoint_subset_test",
                )
                marker = json.loads(
                    marker_line.removeprefix("TERMINAL_EVIDENCE_V1 ")
                )
                runtime_dir = root / ".kent" / "runtime"
                tombstone = runtime_dir / (
                    ".evidence-cleanup-" + runtime.canonical_sha256(marker)
                )
                (runtime_dir / "TASK-1").rename(tombstone)
                for entry in subset:
                    (tombstone / entry).write_text("{}\n")
                sentinel = runtime_dir / (
                    ".evidence-terminal-"
                    + hashlib.sha256(b"TASK-1").hexdigest()
                )
                sentinel.write_bytes(b"")
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    "janitor_checkpoint_subset_test",
                )
                phases: list[str] = []
                outcome = janitor._remove_v2_runtime_state(
                    root.resolve(),
                    "TASK-1",
                    cleanup_report=marker_line,
                    _phase_hook=phases.append,
                )
                self.assertTrue(outcome[0])
                self.assertFalse(tombstone.exists())
                self.assertTrue(sentinel.exists())
                ordered = [
                    phase
                    for phase in phases
                    if phase.endswith("_checkpoint_unlink_fsync")
                    or phase == "after_ledger_unlink_fsync"
                ]
                self.assertEqual(
                    ordered,
                    [
                        phase
                        for phase in (
                            "after_fix_checkpoint_unlink_fsync",
                            "after_smoke_checkpoint_unlink_fsync",
                            "after_ledger_unlink_fsync",
                        )
                        if (
                            phase == "after_ledger_unlink_fsync"
                            or (
                                phase == "after_fix_checkpoint_unlink_fsync"
                                and "fix-checkpoint.json" in subset
                            )
                            or (
                                phase == "after_smoke_checkpoint_unlink_fsync"
                                and "smoke-checkpoint.json" in subset
                            )
                        )
                    ],
                )

    def test_v2_fsync_failure_blocks_and_preserves_runtime(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_fsync_failure_test",
        )
        original_fsync = janitor.os.fsync
        janitor.os.fsync = lambda _fd: (_ for _ in ()).throw(OSError("fsync injected"))
        try:
            outcome = janitor._remove_v2_runtime_state(
                root.resolve(),
                "TASK-1",
                cleanup_report=marker_line,
            )
        finally:
            janitor.os.fsync = original_fsync
        self.assertFalse(outcome[0])
        self.assertTrue(
            (root / ".kent/runtime/TASK-1").exists()
            or any(
                item.name.startswith(".evidence-cleanup-")
                for item in (root / ".kent/runtime").iterdir()
            )
        )

    def test_v2_named_fsync_boundaries_block_without_unauthorized_deletion(self) -> None:
        boundaries = (
            "rename_runtime_parent",
            "sentinel_file",
            "sentinel_runtime_parent",
            "ledger",
            "tombstone_removal_runtime_parent",
        )
        for name in boundaries:
            with self.subTest(boundary=name):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    "janitor_fsync_{}".format(name),
                )
                original_fsync = janitor.os.fsync
                runtime_dir = root / ".kent" / "runtime"
                task = runtime_dir / "TASK-1"
                sentinel = runtime_dir / (
                    ".evidence-terminal-"
                    + hashlib.sha256(b"TASK-1").hexdigest()
                )
                runtime_inode = runtime_dir.stat().st_ino
                tombstone_path = None
                if name == "ledger":
                    (task / "fix-checkpoint.json").write_text("{}\n")
                    (task / "smoke-checkpoint.json").write_text("{}\n")
                fired = False
                runtime_calls = 0

                def injected(fd: int) -> None:
                    nonlocal fired, runtime_calls, tombstone_path
                    metadata = janitor.os.fstat(fd)
                    if name == "rename_runtime_parent":
                        matches = (
                            metadata.st_ino == runtime_inode
                            and not task.exists()
                            and not sentinel.exists()
                            and any(
                                item.name.startswith(".evidence-cleanup-")
                                for item in runtime_dir.iterdir()
                            )
                        )
                    elif name == "sentinel_file":
                        matches = (
                            sentinel.exists()
                            and metadata.st_ino == sentinel.stat().st_ino
                        )
                    elif name == "sentinel_runtime_parent":
                        matches = metadata.st_ino == runtime_inode
                        runtime_calls += matches
                        matches = matches and runtime_calls == 2
                    elif name == "ledger":
                        if tombstone_path is None:
                            tombstones = [
                                item
                                for item in runtime_dir.iterdir()
                                if item.name.startswith(".evidence-cleanup-")
                            ]
                            if tombstones:
                                tombstone_path = tombstones[0]
                        matches = (
                            tombstone_path is not None
                            and metadata.st_ino == tombstone_path.stat().st_ino
                        )
                    else:
                        matches = (
                            metadata.st_ino == runtime_inode
                            and not any(
                                item.name.startswith(".evidence-cleanup-")
                                for item in runtime_dir.iterdir()
                            )
                        )
                    if matches and not fired:
                        fired = True
                        raise OSError(f"{name} fsync injected")
                    original_fsync(fd)

                janitor.os.fsync = injected
                try:
                    outcome = janitor._remove_v2_runtime_state(
                        root.resolve(),
                        "TASK-1",
                        cleanup_report=marker_line,
                    )
                finally:
                    janitor.os.fsync = original_fsync
                self.assertFalse(outcome[0])
                self.assertTrue(fired)
                if name == "tombstone_removal_runtime_parent":
                    self.assertFalse(task.exists())
                    self.assertFalse(
                        any(
                            item.name.startswith(".evidence-cleanup-")
                            for item in runtime_dir.iterdir()
                        )
                    )
                    self.assertTrue(sentinel.exists())
                    retry = janitor._remove_v2_runtime_state(
                        root.resolve(),
                        "TASK-1",
                        cleanup_report=marker_line,
                    )
                    self.assertEqual(retry[0], True)
                else:
                    self.assertTrue(
                        task.exists()
                        or any(
                            item.name.startswith(".evidence-cleanup-")
                            for item in runtime_dir.iterdir()
                        )
                    )

    def test_v2_rollback_runtime_parent_fsync_restores_active_task(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_fsync_rollback_runtime_parent",
        )
        original_fsync = janitor.os.fsync
        runtime_inode = (root / ".kent" / "runtime").stat().st_ino
        post_rename_fired = False
        rollback_fired = False

        def injected(fd: int) -> None:
            nonlocal post_rename_fired, rollback_fired
            if janitor.os.fstat(fd).st_ino == runtime_inode:
                runtime_dir = root / ".kent" / "runtime"
                active = runtime_dir / "TASK-1"
                tombstones = [
                    item
                    for item in runtime_dir.iterdir()
                    if item.name.startswith(".evidence-cleanup-")
                ]
                if not post_rename_fired and not active.exists() and tombstones:
                    post_rename_fired = True
                    raise OSError("post-rename runtime parent fsync injected")
                if post_rename_fired and not rollback_fired and active.exists() and not tombstones:
                    rollback_fired = True
                    raise OSError("rollback runtime parent fsync injected")
            original_fsync(fd)

        janitor.os.fsync = injected
        try:
            outcome = janitor._remove_v2_runtime_state(
                root.resolve(),
                "TASK-1",
                cleanup_report=marker_line,
            )
        finally:
            janitor.os.fsync = original_fsync
        self.assertFalse(outcome[0])
        self.assertTrue(post_rename_fired)
        self.assertTrue(rollback_fired)
        self.assertTrue((root / ".kent/runtime/TASK-1").exists())
        self.assertFalse(
            any(
                item.name.startswith(".evidence-cleanup-")
                for item in (root / ".kent" / "runtime").iterdir()
            )
        )
        self.assertFalse(
            (
                root
                / ".kent"
                / "runtime"
                / (".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest())
            ).exists()
        )

    def test_v2_stable_lock_creation_fsync_failure_blocks(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        lock = runtime_dir / (
            ".evidence-lock-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        lock.unlink()
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_fsync_stable_lock_creation",
        )
        original_fsync = janitor.os.fsync
        runtime_inode = runtime_dir.stat().st_ino
        fired = False

        def injected(fd: int) -> None:
            nonlocal fired
            if (
                not fired
                and janitor.os.fstat(fd).st_ino == runtime_inode
                and lock.exists()
            ):
                fired = True
                raise OSError("stable lock fsync injected")
            original_fsync(fd)

        janitor.os.fsync = injected
        try:
            outcome = janitor._remove_v2_runtime_state(
                root.resolve(),
                "TASK-1",
                cleanup_report=marker_line,
            )
        finally:
            janitor.os.fsync = original_fsync
        self.assertFalse(outcome[0])
        self.assertTrue(fired)
        self.assertTrue((runtime_dir / "TASK-1").exists())

    def test_v2_named_checkpoint_fsync_boundaries_block(self) -> None:
        boundaries = (
            ("fix_checkpoint", ("fix-checkpoint.json",), 1),
            ("smoke_checkpoint", (
                "fix-checkpoint.json",
                "smoke-checkpoint.json",
            ), 2),
            ("ledger", (
                "fix-checkpoint.json",
                "smoke-checkpoint.json",
                "evidence-ledger.jsonl",
            ), 3),
        )
        for name, entries, occurrence in boundaries:
            with self.subTest(boundary=name):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                runtime = load_template_module(
                    REPO_ROOT / "workflowkit" / "runtime.py",
                    "runtime_fsync_{}".format(name),
                )
                marker = json.loads(
                    marker_line.removeprefix("TERMINAL_EVIDENCE_V1 ")
                )
                runtime_dir = root / ".kent" / "runtime"
                tombstone = runtime_dir / (
                    ".evidence-cleanup-" + runtime.canonical_sha256(marker)
                )
                (runtime_dir / "TASK-1").rename(tombstone)
                for entry in entries:
                    if entry != "evidence-ledger.jsonl":
                        (tombstone / entry).write_text("{}\n")
                (
                    runtime_dir
                    / (
                        ".evidence-terminal-"
                        + hashlib.sha256(b"TASK-1").hexdigest()
                    )
                ).write_bytes(b"")
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    "janitor_fsync_{}".format(name),
                )
                original_fsync = janitor.os.fsync
                tombstone_inode = tombstone.stat().st_ino
                calls = 0
                fired = False

                def injected(fd: int) -> None:
                    nonlocal calls, fired
                    if janitor.os.fstat(fd).st_ino == tombstone_inode:
                        calls += 1
                    if calls == occurrence and not fired:
                        fired = True
                        raise OSError(f"{name} fsync injected")
                    original_fsync(fd)

                janitor.os.fsync = injected
                try:
                    outcome = janitor._remove_v2_runtime_state(
                        root.resolve(),
                        "TASK-1",
                        cleanup_report=marker_line,
                    )
                finally:
                    janitor.os.fsync = original_fsync
                self.assertFalse(outcome[0])
                self.assertTrue(fired)
                self.assertTrue(tombstone.exists())

    def test_v2_active_sentinel_conflict_blocks_without_deletion(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        sentinel = runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        sentinel.write_bytes(b"")
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root, cleanup_report=marker_line),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["transition"], "task_janitor_blocked")
        self.assertTrue((runtime_dir / "TASK-1").exists())
        self.assertTrue(sentinel.exists())

    def test_v2_unknown_tombstone_entry_blocks_without_unlink(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_unknown_entry_test",
        )
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        (runtime_dir / "TASK-1").rename(tombstone)
        unknown = tombstone / "unexpected.txt"
        unknown.write_text("preserve\n")
        (runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )).write_bytes(b"")
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root, cleanup_report=marker_line),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["transition"], "task_janitor_blocked")
        self.assertEqual(unknown.read_text(), "preserve\n")

    def test_v2_entry_safety_matrix_blocks_without_unlink(self) -> None:
        for case in ("unknown", "unsafe", "hardlink", "tracked", "not_ignored"):
            with self.subTest(case=case):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                runtime = load_template_module(
                    REPO_ROOT / "workflowkit" / "runtime.py",
                    "runtime_entry_safety_{}".format(case),
                )
                marker = json.loads(
                    marker_line.removeprefix("TERMINAL_EVIDENCE_V1 ")
                )
                runtime_dir = root / ".kent" / "runtime"
                tombstone = runtime_dir / (
                    ".evidence-cleanup-" + runtime.canonical_sha256(marker)
                )
                (runtime_dir / "TASK-1").rename(tombstone)
                (
                    runtime_dir
                    / (
                        ".evidence-terminal-"
                        + hashlib.sha256(b"TASK-1").hexdigest()
                    )
                ).write_bytes(b"")
                if case == "unknown":
                    entry = tombstone / "unexpected.txt"
                    entry.write_text("preserve\n")
                else:
                    entry = tombstone / "fix-checkpoint.json"
                    entry.write_text("{}\n")
                    if case == "unsafe":
                        entry.chmod(0o644)
                    elif case == "hardlink":
                        os.link(entry, root / "checkpoint-hard-link")
                    elif case == "tracked":
                        self.run_git(
                            root,
                            "add",
                            "-f",
                            str(entry.relative_to(root)),
                        )
                        self.run_git(root, "commit", "-q", "-m", "track entry")
                    elif case == "not_ignored":
                        (root / ".gitignore").write_text("")
                        self.run_git(root, "add", ".gitignore")
                        self.run_git(
                            root,
                            "commit",
                            "-q",
                            "-m",
                            "unignore entry",
                        )
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    "janitor_entry_safety_{}".format(case),
                )
                outcome = janitor._remove_v2_runtime_state(
                    root.resolve(),
                    "TASK-1",
                    cleanup_report=marker_line,
                )
                self.assertFalse(outcome[0])
                self.assertTrue(tombstone.exists())
                self.assertTrue(entry.exists())

    def test_v2_retry_after_tombstone_rename_creates_sentinel_and_cleans(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_pre_sentinel_retry_test",
        )
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        (runtime_dir / "TASK-1").rename(tombstone)
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root, cleanup_report=marker_line),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["transition"], "task_janitor_done")
        self.assertFalse(tombstone.exists())
        self.assertTrue(
            (
                runtime_dir
                / (
                    ".evidence-terminal-"
                    + hashlib.sha256(b"TASK-1").hexdigest()
                )
            ).exists()
        )

    def test_v2_tombstone_rename_phase_failure_preserves_ledger_for_retry(
        self,
    ) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_tombstone_rename_phase_test",
        )
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime_dir = root / ".kent" / "runtime"
        task = runtime_dir / "TASK-1"
        sentinel = runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        ledger_before = (task / "evidence-ledger.jsonl").read_bytes()
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_tombstone_rename_phase_test",
        )
        fired = False

        def hook(phase: str) -> None:
            nonlocal fired
            if phase == "after_tombstone_rename":
                fired = True
                raise OSError("tombstone rename phase injected")

        outcome = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
            _phase_hook=hook,
        )
        self.assertFalse(outcome[0])
        self.assertTrue(fired)
        self.assertFalse(task.exists())
        self.assertTrue(tombstone.is_dir())
        self.assertFalse(sentinel.exists())
        self.assertEqual(
            (tombstone / "evidence-ledger.jsonl").read_bytes(),
            ledger_before,
        )
        retry = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertTrue(retry[0])
        self.assertTrue(sentinel.exists())
        self.assertFalse(tombstone.exists())

    def test_v2_sentinel_create_phase_failure_preserves_state_and_blocks_append(
        self,
    ) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_sentinel_create_phase_test",
        )
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime_dir = root / ".kent" / "runtime"
        task = runtime_dir / "TASK-1"
        sentinel = runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_sentinel_create_phase_test",
        )
        fired = False

        def hook(phase: str) -> None:
            nonlocal fired
            if phase == "after_terminal_sentinel_create":
                fired = True
                raise OSError("terminal sentinel phase injected")

        outcome = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
            _phase_hook=hook,
        )
        self.assertFalse(outcome[0])
        self.assertTrue(fired)
        self.assertFalse(task.exists())
        self.assertTrue(tombstone.is_dir())
        self.assertTrue(sentinel.is_file())
        append = subprocess.run(
            [
                str(scripts / "workflow-evidence-ledger"),
                "append",
                "--task",
                "TASK-1",
                "--workspace",
                str(root),
            ],
            input=json.dumps(
                {
                    "node_key": "implement",
                    "evidence_type": "implementation",
                    "summary": "must reject",
                    "artifacts": [],
                    "checks": [],
                    "decisions": [],
                    "context": {
                        "manifest_path": ".kent/context/implement.md",
                        "files_read": [],
                    },
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(append.returncode, 1)
        self.assertFalse(task.exists())
        retry = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertTrue(retry[0])
        self.assertTrue(sentinel.exists())
        self.assertFalse(tombstone.exists())

    def test_v2_empty_tombstone_rejects_same_task_foreign_marker(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_foreign_empty_marker_test",
        )
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        foreign = dict(marker)
        foreign["final_hash"] = "b" * 64
        foreign_line = runtime.terminal_marker_line(foreign)
        runtime_dir = root / ".kent" / "runtime"
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        (runtime_dir / "TASK-1").rename(tombstone)
        (tombstone / "evidence-ledger.jsonl").unlink()
        (
            runtime_dir
            / (".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest())
        ).write_bytes(b"")
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root, cleanup_report=foreign_line),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["transition"],
            "task_janitor_blocked",
        )
        self.assertTrue(tombstone.exists())

    def test_v2_terminal_loss_matrix_blocks_without_deletion(self) -> None:
        cases = (
            "active_sentinel_conflict",
            "missing_sentinel_after_ledger_loss",
            "checkpoint_after_ledger_loss",
            "marker_mismatch",
            "wrong_tombstone",
        )
        for case in cases:
            with self.subTest(case=case):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                runtime = load_template_module(
                    REPO_ROOT / "workflowkit" / "runtime.py",
                    "runtime_loss_{}".format(case),
                )
                marker = json.loads(
                    marker_line.removeprefix("TERMINAL_EVIDENCE_V1 ")
                )
                runtime_dir = root / ".kent" / "runtime"
                if case == "active_sentinel_conflict":
                    (
                        runtime_dir
                        / (
                            ".evidence-terminal-"
                            + hashlib.sha256(b"TASK-1").hexdigest()
                        )
                    ).write_bytes(b"")
                else:
                    tombstone_name = (
                        ".evidence-cleanup-"
                        + runtime.canonical_sha256(marker)
                    )
                    if case == "wrong_tombstone":
                        tombstone_name = ".evidence-cleanup-wrong"
                    tombstone = runtime_dir / tombstone_name
                    (runtime_dir / "TASK-1").rename(tombstone)
                    if case != "missing_sentinel_after_ledger_loss":
                        (
                            runtime_dir
                            / (
                                ".evidence-terminal-"
                                + hashlib.sha256(b"TASK-1").hexdigest()
                            )
                        ).write_bytes(b"")
                    if case in {
                        "missing_sentinel_after_ledger_loss",
                        "checkpoint_after_ledger_loss",
                    }:
                        (tombstone / "evidence-ledger.jsonl").unlink()
                    if case == "checkpoint_after_ledger_loss":
                        (tombstone / "fix-checkpoint.json").write_text("{}\n")
                    if case == "marker_mismatch":
                        foreign = dict(marker)
                        foreign["final_hash"] = "b" * 64
                        marker_line = runtime.terminal_marker_line(foreign)
                result = subprocess.run(
                    [str(scripts / "workflow-task-janitor")],
                    input=self.janitor_input(
                        root,
                        cleanup_report=marker_line,
                    ),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["transition"],
                    "task_janitor_blocked",
                )
                self.assertTrue(
                    any(
                        item.name.startswith(".evidence-cleanup-")
                        for item in runtime_dir.iterdir()
                    )
                    or (runtime_dir / "TASK-1").exists()
                )

    def test_v2_primary_acknowledgement_loss_is_idempotent(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_primary_acknowledgement_loss",
        )
        first = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertTrue(first[0])
        runtime_dir = root / ".kent" / "runtime"
        self.assertFalse((runtime_dir / "TASK-1").exists())
        self.assertFalse(
            any(
                item.name.startswith(".evidence-cleanup-")
                for item in runtime_dir.iterdir()
            )
        )
        sentinel = runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        self.assertTrue(sentinel.exists())
        second = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertTrue(second[0])
        self.assertFalse((runtime_dir / "TASK-1").exists())
        self.assertTrue(sentinel.exists())

    def test_v2_active_state_conflict_matrix_blocks_without_deletion(self) -> None:
        for case in ("active_tombstone", "active_invalid", "active_unsealed"):
            with self.subTest(case=case):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                runtime_dir = root / ".kent" / "runtime"
                task = runtime_dir / "TASK-1"
                if case == "active_tombstone":
                    (runtime_dir / ".evidence-cleanup-conflict").mkdir()
                else:
                    ledger = task / "evidence-ledger.jsonl"
                    if case == "active_invalid":
                        ledger.write_text("{\n")
                    else:
                        records = [
                            json.loads(line)
                            for line in ledger.read_text().splitlines()
                        ]
                        ledger.write_text(json.dumps(records[0]) + "\n")
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    "janitor_active_state_{}".format(case),
                )
                outcome = janitor._remove_v2_runtime_state(
                    root.resolve(),
                    "TASK-1",
                    cleanup_report=marker_line,
                )
                self.assertFalse(outcome[0])
                self.assertTrue(task.exists())

    def test_v2_primary_active_conflict_matrix_blocks_without_deletion(self) -> None:
        for case in ("sentinel", "tombstone", "invalid", "unsealed"):
            with self.subTest(case=case):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                runtime_dir = root / ".kent" / "runtime"
                task = runtime_dir / "TASK-1"
                if case == "sentinel":
                    (
                        runtime_dir
                        / (
                            ".evidence-terminal-"
                            + hashlib.sha256(b"TASK-1").hexdigest()
                        )
                    ).write_bytes(b"")
                elif case == "tombstone":
                    (runtime_dir / ".evidence-cleanup-conflict").mkdir()
                else:
                    ledger = task / "evidence-ledger.jsonl"
                    if case == "invalid":
                        ledger.write_text("{\n")
                    else:
                        records = [
                            json.loads(line)
                            for line in ledger.read_text().splitlines()
                        ]
                        ledger.write_text(json.dumps(records[0]) + "\n")
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    "janitor_primary_active_{}".format(case),
                )
                outcome = janitor._remove_v2_runtime_state(
                    root.resolve(),
                    "TASK-1",
                    cleanup_report=marker_line,
                )
                self.assertFalse(outcome[0])
                self.assertTrue(task.exists())

    def test_v2_primary_missing_ledger_blocks_without_legacy_fallback(self) -> None:
        root = self.create_repository()
        scripts = self.install_v2_runtime_commands(root)
        task = root / ".kent" / "runtime" / "TASK-1"
        task.mkdir(parents=True)
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["transition"],
            "task_janitor_blocked",
        )
        self.assertTrue(task.exists())

    def test_v2_fifo_ledger_read_is_bounded_and_preserves_state(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        ledger = root / ".kent" / "runtime" / "TASK-1" / "evidence-ledger.jsonl"
        ledger.unlink()
        os.mkfifo(ledger, 0o600)
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root, cleanup_report=marker_line),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["transition"],
            "task_janitor_blocked",
        )
        self.assertTrue(stat.S_ISFIFO(ledger.stat().st_mode))

    def test_v2_fifo_sentinel_read_is_bounded_and_preserves_state(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        sentinel = root / ".kent" / "runtime" / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        os.mkfifo(sentinel, 0o600)
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root, cleanup_report=marker_line),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["transition"],
            "task_janitor_blocked",
        )
        self.assertTrue(stat.S_ISFIFO(sentinel.stat().st_mode))

    def test_v2_existing_sentinel_retry_fsync_failure_preserves_tombstone(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_existing_sentinel_retry_test",
        )
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime_dir = root / ".kent" / "runtime"
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        (runtime_dir / "TASK-1").rename(tombstone)
        (tombstone / "evidence-ledger.jsonl").unlink()
        sentinel = runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        sentinel.write_bytes(b"")
        inode = sentinel.stat().st_ino
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_existing_sentinel_retry_test",
        )
        original_fsync = janitor.os.fsync
        fired = False

        def injected(fd: int) -> None:
            nonlocal fired
            if janitor.os.fstat(fd).st_ino == inode:
                fired = True
                raise OSError("existing sentinel fsync injected")
            original_fsync(fd)

        janitor.os.fsync = injected
        try:
            outcome = janitor._remove_v2_runtime_state(
                root.resolve(),
                "TASK-1",
                cleanup_report=marker_line,
            )
        finally:
            janitor.os.fsync = original_fsync
        self.assertFalse(outcome[0])
        self.assertTrue(fired)
        self.assertTrue(tombstone.exists())
        self.assertEqual(sentinel.stat().st_ino, inode)

    def test_v2_existing_sentinel_runtime_fsync_failure_preserves_tombstone(
        self,
    ) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_existing_sentinel_runtime_test",
        )
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime_dir = root / ".kent" / "runtime"
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        (runtime_dir / "TASK-1").rename(tombstone)
        (tombstone / "evidence-ledger.jsonl").unlink()
        sentinel = runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        sentinel.write_bytes(b"")
        runtime_inode = runtime_dir.stat().st_ino
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_existing_sentinel_runtime_test",
        )
        original_fsync = janitor.os.fsync
        fired = False

        def injected(fd: int) -> None:
            nonlocal fired
            if janitor.os.fstat(fd).st_ino == runtime_inode:
                fired = True
                raise OSError("existing sentinel runtime fsync injected")
            original_fsync(fd)

        janitor.os.fsync = injected
        try:
            outcome = janitor._remove_v2_runtime_state(
                root.resolve(),
                "TASK-1",
                cleanup_report=marker_line,
            )
        finally:
            janitor.os.fsync = original_fsync
        self.assertFalse(outcome[0])
        self.assertTrue(fired)
        self.assertTrue(tombstone.exists())
        self.assertTrue(sentinel.exists())

    def test_managed_retention_blocks_active_task_and_terminal_sentinel(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        (
            runtime_dir
            / (".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest())
        ).write_bytes(b"")
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_managed_active_sentinel_test",
        )
        result = janitor._prepare_v2_managed_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertFalse(result[0])
        self.assertTrue((runtime_dir / "TASK-1").exists())

    def test_managed_retention_rejects_wrong_tombstone_and_unknown_entry(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        tombstone = runtime_dir / ".evidence-cleanup-wrong"
        (runtime_dir / "TASK-1").rename(tombstone)
        (tombstone / "unexpected.txt").write_text("preserve\n")
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_managed_wrong_tombstone_test",
        )
        result = janitor._prepare_v2_managed_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertFalse(result[0])
        self.assertTrue(tombstone.exists())
        self.assertEqual(
            (tombstone / "unexpected.txt").read_text(),
            "preserve\n",
        )

    def test_managed_retention_blocks_active_task_and_tombstone(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "managed_active_tombstone_test",
        )
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime_dir = root / ".kent" / "runtime"
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        tombstone.mkdir()
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_managed_active_tombstone_test",
        )
        result = janitor._prepare_v2_managed_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertFalse(result[0])
        self.assertTrue((runtime_dir / "TASK-1").exists())
        self.assertTrue(tombstone.exists())

    def test_managed_retention_blocks_active_missing_invalid_or_unsealed_ledger(
        self,
    ) -> None:
        for state in ("missing", "invalid", "unsealed"):
            with self.subTest(state=state):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                ledger = (
                    root
                    / ".kent"
                    / "runtime"
                    / "TASK-1"
                    / "evidence-ledger.jsonl"
                )
                if state == "missing":
                    ledger.unlink()
                elif state == "invalid":
                    ledger.write_text("{\n")
                else:
                    records = [json.loads(line) for line in ledger.read_text().splitlines()]
                    ledger.write_text(json.dumps(records[0]) + "\n")
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    "janitor_managed_active_{}".format(state),
                )
                result = janitor._prepare_v2_managed_runtime_state(
                    root.resolve(),
                    "TASK-1",
                    cleanup_report=marker_line,
                )
                self.assertFalse(result[0])
                self.assertTrue((root / ".kent/runtime/TASK-1").exists())

    def test_managed_preflight_rejects_unsafe_tracked_and_not_ignored_entries(
        self,
    ) -> None:
        for state in ("unsafe", "tracked", "not_ignored"):
            with self.subTest(state=state):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                runtime = load_template_module(
                    REPO_ROOT / "workflowkit" / "runtime.py",
                    "managed_entry_{}".format(state),
                )
                marker = json.loads(
                    marker_line.removeprefix("TERMINAL_EVIDENCE_V1 ")
                )
                runtime_dir = root / ".kent" / "runtime"
                tombstone = runtime_dir / (
                    ".evidence-cleanup-" + runtime.canonical_sha256(marker)
                )
                (runtime_dir / "TASK-1").rename(tombstone)
                entry = tombstone / "fix-checkpoint.json"
                entry.write_text("{}\n")
                if state == "unsafe":
                    entry.chmod(0o644)
                elif state == "tracked":
                    self.run_git(
                        root,
                        "add",
                        "-f",
                        str(entry.relative_to(root)),
                    )
                    self.run_git(root, "commit", "-q", "-m", "track runtime")
                else:
                    (root / ".gitignore").write_text("")
                    self.run_git(root, "add", ".gitignore")
                    self.run_git(root, "commit", "-q", "-m", "unignore runtime")
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    "janitor_managed_entry_{}".format(state),
                )
                result = janitor._prepare_v2_managed_runtime_state(
                    root.resolve(),
                    "TASK-1",
                    cleanup_report=marker_line,
                )
                self.assertFalse(result[0])
                self.assertTrue(tombstone.exists())

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

    def test_primary_path_replacement_after_wrapper_blocks_cleanup(self) -> None:
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
            "\"observed_root\": root}}))'\n"
            "  exit 0\n"
            "fi\n"
            "for argument in \"$@\"; do target=\"$argument\"; done\n"
            "git -C \"$KENT_TEST_PRIMARY\" worktree remove --force \"$target\"\n"
            "mv \"$KENT_TEST_PRIMARY\" \"$KENT_TEST_PRIMARY.old\"\n"
            "git init -q \"$KENT_TEST_PRIMARY\"\n"
            "printf '%s\\n' '{\"kind\":\"completed\"}'\n"
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
        self.assertIn("primary checkout", payload["cleanup_report"])
        self.assertTrue((root / ".git").is_dir())
        self.assertTrue((root.parent / (root.name + ".old")).is_dir())

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
