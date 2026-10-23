from __future__ import annotations

import json
import errno
import fcntl
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest import mock
import xml.etree.ElementTree as ET


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
VERIFICATION_DISPATCH = (
    REPO_ROOT
    / "templates"
    / "project"
    / "workflow-verification-dispatch"
)
APK_INSTALL = (
    REPO_ROOT / "templates" / "project" / "android-apk-install-preserve"
)
# Test-owned identities only: fixture writers must never depend on the Agent
# environment running unittest (CI deliberately has no inherited Kent owner).
EVIDENCE_FIXTURE_OWNER = {
    "KENT_SESSION_ID": "11111111-1111-4111-8111-111111111111",
    "KENT_STEP_ID": "22222222-2222-4222-8222-222222222222",
}
REAL_GIT = shutil.which("git") or "git"


def load_template_module(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_pids_gone(
    testcase: unittest.TestCase,
    pid_file: Path,
    timeout: float = 2.0,
) -> None:
    pids = [int(value) for value in pid_file.read_text().split()]
    deadline = time.monotonic() + timeout
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


def assert_raw_edge_projection(
    testcase: unittest.TestCase,
    raw_result: subprocess.CompletedProcess[str],
    *,
    spec,
    edge_key: str,
) -> dict[str, object]:
    testcase.assertEqual(raw_result.returncode, 0, raw_result.stderr)
    output = json.loads(raw_result.stdout)
    edge = next((item for item in spec.edges if item.key == edge_key), None)
    testcase.assertIsNotNone(edge, edge_key)
    testcase.assertIsInstance(output.get("transition"), str)
    expected = {parameter.key for parameter in edge.parameters}
    actual = set(output) - {"transition", "commentary"}
    testcase.assertEqual(actual, expected, (edge_key, output, expected))
    if "commentary" in output:
        testcase.assertIsInstance(output["commentary"], str)
    return output


def assert_raw_native_transition_projection(
    testcase: unittest.TestCase,
    raw_result: subprocess.CompletedProcess[str],
    *,
    spec,
    edge_key: str,
) -> dict[str, object]:
    output = assert_raw_edge_projection(
        testcase, raw_result, spec=spec, edge_key=edge_key,
    )
    from workflowkit.graph import plan_workflow_graph

    projected = plan_workflow_graph(
        spec,
        {
            "workflow_id": "44444444-4444-4444-8444-444444444444",
            "expected_version": 1,
            "graph": {
                "node_groups": [],
                "nodes": [],
                "transition_groups": [],
                "edges": [],
            },
        },
        metadata_changed=False,
    ).document["graph"]
    native_edge = next(
        item for item in projected["edges"] if item["key"] == edge_key
    )
    expected = {
        parameter["key"] for parameter in native_edge.get("parameters", [])
    }
    actual = set(output) - {"transition", "commentary"}
    testcase.assertEqual(actual, expected, (edge_key, output, expected))
    native_group = next(
        group for group in projected["transition_groups"]
        if group["id"] == native_edge["transition_group_id"]
    )
    testcase.assertEqual(output["transition"], native_group["transition_id"])
    for key in expected:
        value = output[key]
        testcase.assertIsInstance(value, str, (edge_key, key, output))
        testcase.assertTrue(value.strip(), (edge_key, key, output))
    return output


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
                ("workflow-wait-github-pr", "runtime_contracts"),
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
            [REAL_GIT, "-C", str(root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def run_fixture_git(
        self,
        root: Path,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
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
    def ensure_disabled_complexity_profile(self, root: Path) -> None:
        profile = root / ".kent" / "workflow-profile.toml"
        if not profile.exists():
            profile.write_text(
                "[policies]\n"
                'writer_sessions = "continuous"\n'
                'coder_selection = "disabled"\n'
            )

    def generated_spec(
        self,
        *,
        writer_sessions: str = "continuous",
        coder_selection: str = "disabled",
        branch_identity: str = "task",
        delivery_context: bool = True,
    ):
        from dataclasses import replace

        from workflowkit.delivery import build_delivery_workflow
        from workflowkit.profile import ProjectProfile

        profile = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
            check_files=False,
        )
        policies = {
            **profile.policies,
            "writer_sessions": writer_sessions,
            "coder_selection": coder_selection,
            "branch_identity": branch_identity,
        }
        commands = dict(profile.commands)
        issue_tracker = profile.issue_tracker
        if branch_identity == "jira":
            issue_tracker = "jira"
            commands["branch_identity"] = ".kent/scripts/workflow-branch-identity"
        if not delivery_context:
            profile = replace(profile, delivery_profile="standard")
        profile = replace(
            profile,
            policies=policies,
            commands=commands,
            issue_tracker=issue_tracker,
        )
        return build_delivery_workflow(profile, 1)

    def coder_selection_plan(self, complexity: str = "simple") -> str:
        return (
            "# Plan\n\n- [ ] Implement feature\n\n"
            "```json\n"
            + json.dumps({
                "schema": "coder-selection-v1",
                "task_short_id": "TASK-PLAN",
                "complexity": complexity,
                "rationale": "The accepted scope has a bounded implementation path.",
            }, indent=2)
            + "\n```\n"
        )

    def run_contract(
        self,
        root: Path,
        *,
        mode: str,
        route: str = "continue",
        spoof_mode: str | None = None,
        spoof_route: str | None = None,
        delivery_context: str | None = None,
        review_context: str = "bounded review context",
        accepted_plan_sha256: str | None = None,
        reviewed_normalized_sha256: str | None = None,
        native_task_id: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.ensure_disabled_complexity_profile(root)
        executable = (
            PLAN_CONTRACT_ACCEPT
            if mode == "accept"
            else PLAN_CONTRACT_CHECK[route]
        )
        if delivery_context is not None:
            scripts = root / ".kent" / "scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            for source in (
                PLAN_CONTRACT,
                PLAN_CONTRACT_ACCEPT,
                *PLAN_CONTRACT_CHECK.values(),
            ):
                target = scripts / source.name
                target.write_bytes(source.read_bytes())
                target.chmod(0o755)
            (scripts / "workflow_runtime_contracts.py").write_bytes(
                (REPO_ROOT / "workflowkit" / "runtime.py").read_bytes()
            )
            executable = scripts / executable.name
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
            "review_context": review_context,
            "task_short_id": "TASK-PLAN",
        }
        if route == "fix_continue":
            payload["fix_context"] = "remaining fix bundle"
        if spoof_mode is not None:
            payload["plan_contract_mode"] = spoof_mode
        if delivery_context is not None:
            payload["delivery_context"] = delivery_context
        if accepted_plan_sha256 is not None:
            payload["accepted_plan_sha256"] = accepted_plan_sha256
        if reviewed_normalized_sha256 is not None:
            payload["reviewed_normalized_sha256"] = reviewed_normalized_sha256
        if native_task_id is not None:
            payload["_kent"] = {"task_id": native_task_id}
        return subprocess.run(
            [str(executable)],
            cwd=root,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, **(environment or {})},
        )

    def test_delivery_context_survives_plan_contract_routes(self) -> None:
        context = json.dumps({
            "schema": "workflow-delivery-context-v1",
            "phase": "pre_pr",
        })
        root = self.create_repository()
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")

        for route in ("start", "continue", "verify", "fix_continue"):
            with self.subTest(accepted_route=route):
                accepted = self.run_contract(
                    root,
                    mode="accept",
                    route=route,
                    delivery_context=context,
                )
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                self.assertEqual(
                    json.loads(accepted.stdout)["delivery_context"],
                    context,
                )
        snapshot = json.loads(
            (root / ".kent" / "runtime" / "TASK-PLAN" / "plan-contract.json")
            .read_text()
        )
        self.assertNotIn("delivery_context", snapshot)

        for route in ("continue", "verify", "fix_continue"):
            with self.subTest(route=route):
                checked = self.run_contract(
                    root,
                    mode="check",
                    route=route,
                    delivery_context=context,
                )
                self.assertEqual(checked.returncode, 0, checked.stderr)
                self.assertEqual(
                    json.loads(checked.stdout)["delivery_context"],
                    context,
                )

        plan.write_text("# Plan\n\n- [ ] Changed acceptance\n")
        for route in ("continue", "verify", "fix_continue"):
            with self.subTest(changed_route=route):
                changed = self.run_contract(
                    root,
                    mode="check",
                    route=route,
                    delivery_context=context,
                )
                self.assertEqual(changed.returncode, 0, changed.stderr)
                self.assertEqual(
                    json.loads(changed.stdout)["transition"],
                    f"plan_contract_{route}_changed",
                )
                self.assertEqual(
                    json.loads(changed.stdout)["delivery_context"],
                    context,
                )
        self.assertFalse((root / ".kent/scripts/__pycache__").exists())

    def test_guard_raw_outputs_match_generated_edges_for_selection_and_context_modes(self) -> None:
        context = json.dumps(
            {"schema": "workflow-delivery-context-v1", "phase": "pre_pr"},
            separators=(",", ":"),
        )
        for selection in ("disabled", "complexity"):
            for context_enabled in (False, True):
                with self.subTest(selection=selection, context=context_enabled):
                    root = self.create_repository()
                    (root / ".kent" / "workflow-profile.toml").write_text(
                        "[policies]\n"
                        'writer_sessions = "continuous"\n'
                        f'coder_selection = "{selection}"\n'
                    )
                    plan = root / ".todo" / "task" / "plan.md"
                    plan.parent.mkdir(parents=True)
                    plan.write_text(
                        self.coder_selection_plan()
                        if selection == "complexity"
                        else "# Plan\n\n- [ ] Implement feature\n"
                    )
                    carried_context = context if context_enabled else None
                    normalized_module = load_template_module(
                        PLAN_CONTRACT,
                        "runtime_plan_contract_digest",
                    )
                    normalized_digest = hashlib.sha256(
                        normalized_module.inspection_normalized_plan(
                            plan.read_bytes()
                        ).encode("utf-8")
                    ).hexdigest()
                    spec = self.generated_spec(
                        coder_selection=selection,
                        delivery_context=context_enabled,
                    )
                    session_env: dict[str, str] = {}
                    native_task_id = None
                    accepted_digest_input = None
                    reviewed_digest_input = None
                    session_inventory_path: Path | None = None
                    if selection == "complexity":
                        kent = root / "kent-fixture"
                        session_inventory_path = root / "sessions.json"
                        session_inventory_path.write_text(
                            json.dumps(
                                {
                                    "task_id": "native-task",
                                    "items": [],
                                }
                            )
                        )
                        kent.write_text(
                            f"#!{sys.executable}\n"
                            "import json, os\n"
                            "with open(os.environ['KENT_TEST_SESSIONS'], encoding='utf-8') as source:\n"
                            "    print(json.dumps(json.load(source)))\n"
                        )
                        kent.chmod(0o755)
                        session_env = {
                            "KENT_BIN": str(kent),
                            "KENT_TEST_SESSIONS": str(session_inventory_path),
                        }
                        native_task_id = "native-task"
                        accepted_digest_input = "not-applicable"
                        reviewed_digest_input = normalized_digest
                    accepted = self.run_contract(
                        root,
                        mode="accept",
                        route="start",
                        delivery_context=carried_context,
                        accepted_plan_sha256=accepted_digest_input,
                        reviewed_normalized_sha256=reviewed_digest_input,
                        native_task_id=native_task_id,
                        environment=session_env,
                    )
                    accepted_output = assert_raw_edge_projection(
                        self,
                        accepted,
                        spec=spec,
                        edge_key="plan_contract_implement",
                    )
                    self.assertNotIn("plan_route", accepted_output)
                    self.assertNotIn("plan_route_context", accepted_output)
                    if selection == "complexity":
                        self.assertEqual(
                            accepted_output["target_assignee"],
                            "implementation-simple",
                        )
                        self.assertEqual(accepted_output["target_thinking"], "xhigh")
                        accepted_digest = accepted_output["accepted_plan_sha256"]
                    else:
                        self.assertNotIn("target_assignee", accepted_output)
                        self.assertNotIn("target_thinking", accepted_output)
                        self.assertNotIn("accepted_plan_sha256", accepted_output)
                        accepted_digest = None

                    if session_inventory_path is not None:
                        session_inventory_path.write_text(
                            json.dumps(
                                {
                                    "task_id": "native-task",
                                    "items": [
                                        {
                                            "session_id": "retained-writer",
                                            "agent_role": "implementation-simple",
                                        }
                                    ],
                                }
                            )
                        )
                    stable_continue = self.run_contract(
                        root,
                        mode="check",
                        route="continue",
                        delivery_context=carried_context,
                        accepted_plan_sha256=accepted_digest,
                        native_task_id=native_task_id,
                        environment=session_env,
                    )
                    stable_output = assert_raw_edge_projection(
                        self,
                        stable_continue,
                        spec=spec,
                        edge_key="plan_contract_checked_continue",
                    )
                    for omitted in (
                        "plan_route",
                        "plan_route_context",
                        "target_assignee",
                        "target_thinking",
                    ):
                        self.assertNotIn(omitted, stable_output)

                    stable_verify = self.run_contract(
                        root,
                        mode="check",
                        route="verify",
                        delivery_context=carried_context,
                    )
                    verify_output = assert_raw_edge_projection(
                        self,
                        stable_verify,
                        spec=spec,
                        edge_key="plan_contract_checked_verify",
                    )
                    for omitted in (
                        "plan_route",
                        "plan_route_context",
                        "target_assignee",
                        "target_thinking",
                    ):
                        self.assertNotIn(omitted, verify_output)
                    self.assertNotIn("accepted_plan_sha256", verify_output)

                    plan.write_text(plan.read_text() + "\n- [ ] Changed acceptance\n")
                    changed = self.run_contract(
                        root,
                        mode="check",
                        route="continue",
                        delivery_context=carried_context,
                        accepted_plan_sha256=accepted_digest,
                        native_task_id=native_task_id,
                        environment=session_env,
                    )
                    changed_output = assert_raw_edge_projection(
                        self,
                        changed,
                        spec=spec,
                        edge_key="plan_contract_continue_revalidate",
                    )
                    self.assertEqual(
                        "accepted_plan_sha256" in changed_output,
                        selection == "complexity",
                    )
                    for omitted in ("target_assignee", "target_thinking"):
                        self.assertNotIn(omitted, changed_output)
                    if session_inventory_path is not None:
                        session_inventory_path.write_text(
                            json.dumps(
                                {
                                    "task_id": "native-task",
                                    "items": [],
                                }
                            )
                        )
                    reviewed_after_change = hashlib.sha256(
                        normalized_module.inspection_normalized_plan(
                            plan.read_bytes()
                        ).encode("utf-8")
                    ).hexdigest()
                    reaccepted = self.run_contract(
                        root,
                        mode="accept",
                        route="start",
                        delivery_context=carried_context,
                        accepted_plan_sha256=accepted_digest,
                        reviewed_normalized_sha256=(
                            reviewed_after_change
                            if selection == "complexity"
                            else None
                        ),
                        native_task_id=native_task_id,
                        environment=session_env,
                    )
                    reaccepted_output = assert_raw_edge_projection(
                        self,
                        reaccepted,
                        spec=spec,
                        edge_key="plan_contract_implement",
                    )
                    if selection == "complexity":
                        self.assertEqual(
                            reaccepted_output["target_assignee"],
                            "implementation-simple",
                        )
                        self.assertEqual(
                            reaccepted_output["target_thinking"],
                            "xhigh",
                        )
                        self.assertNotEqual(
                            reaccepted_output["accepted_plan_sha256"],
                            accepted_digest,
                        )

    def test_fix_continue_changed_output_matches_fresh_writer_edge(self) -> None:
        context = json.dumps(
            {"schema": "workflow-delivery-context-v1", "phase": "pre_pr"},
            separators=(",", ":"),
        )
        root = self.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            "[policies]\n"
            'writer_sessions = "fresh_per_slice"\n'
            'coder_selection = "disabled"\n'
        )
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")
        spec = self.generated_spec(
            writer_sessions="fresh_per_slice",
            coder_selection="disabled",
        )
        accepted = self.run_contract(
            root,
            mode="accept",
            route="fix_continue",
            delivery_context=context,
        )
        assert_raw_edge_projection(
            self,
            accepted,
            spec=spec,
            edge_key="plan_contract_fix_continue",
        )
        stable = self.run_contract(
            root,
            mode="check",
            route="fix_continue",
            delivery_context=context,
        )
        assert_raw_edge_projection(
            self,
            stable,
            spec=spec,
            edge_key="plan_contract_checked_fix",
        )
        plan.write_text(plan.read_text() + "\n- [ ] Reconcile changed criterion\n")
        changed = self.run_contract(
            root,
            mode="check",
            route="fix_continue",
            delivery_context=context,
        )
        changed_output = assert_raw_edge_projection(
            self,
            changed,
            spec=spec,
            edge_key="plan_contract_fix_revalidate",
        )
        self.assertNotIn("accepted_plan_sha256", changed_output)
        self.assertNotIn("target_assignee", changed_output)
        self.assertNotIn("target_thinking", changed_output)

    def test_selected_branch_identity_raw_output_matches_generated_writer_edge(self) -> None:
        from tests.test_branch_identity import BranchIdentityTest

        branch_case = BranchIdentityTest(
            "test_complexity_selection_is_revalidated_and_emitted_on_branch_entry"
        )
        branch_case.setUp()
        self.addCleanup(branch_case.doCleanups)
        branch_case.configure("jira", coder_selection=True)
        branch_case.task(
            short_id="TASK-1",
            source_url="https://example.atlassian.net/browse/MBL-826",
        )
        plan = branch_case.root / ".todo" / "canary" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text(
            "# Accepted plan\n\n"
            "- [ ] Implement the reviewed behavior.\n\n"
            "```json\n"
            + json.dumps(
                {
                    "schema": "coder-selection-v1",
                    "task_short_id": "TASK-1",
                    "complexity": "simple",
                    "rationale": "The accepted implementation scope supports this choice.",
                },
                indent=2,
            )
            + "\n```\n"
        )
        (branch_case.root / ".gitignore").write_text("/.kent/runtime/\n")

        scripts = branch_case.root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        for source in (
            PLAN_CONTRACT,
            PLAN_CONTRACT_ACCEPT,
            *PLAN_CONTRACT_CHECK.values(),
        ):
            target = scripts / source.name
            target.write_bytes(source.read_bytes())
            target.chmod(0o755)
        (scripts / "workflow_runtime_contracts.py").write_bytes(
            (REPO_ROOT / "workflowkit" / "runtime.py").read_bytes()
        )
        branch_script = scripts / "workflow-branch-identity"
        branch_script.write_bytes(
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "workflow-branch-identity"
            ).read_bytes()
        )
        branch_script.chmod(0o755)

        normalized_module = load_template_module(
            PLAN_CONTRACT,
            "runtime_branch_plan_contract_digest",
        )
        reviewed_digest = hashlib.sha256(
            normalized_module.inspection_normalized_plan(
                plan.read_bytes()
            ).encode("utf-8")
        ).hexdigest()
        delivery_context = json.dumps(
            {"schema": "workflow-delivery-context-v1", "phase": "pre_pr"},
            separators=(",", ":"),
        )
        spec = self.generated_spec(
            coder_selection="complexity",
            branch_identity="jira",
        )
        plan_input = {
            "workspace_path": str(branch_case.root),
            "plan_path": ".todo/canary/plan.md",
            "work_kind": "test",
            "plan_route": "start",
            "plan_route_context": "not-applicable",
            "review_context": "Reviewed selection with human approval.",
            "task_short_id": "TASK-1",
            "accepted_plan_sha256": "not-applicable",
            "reviewed_normalized_sha256": reviewed_digest,
            "delivery_context": delivery_context,
            "_kent": {"task_id": "task-uuid"},
        }
        environment = os.environ.copy()
        environment.update(
            {
                "KENT_BIN": str(branch_case.kent),
                "KENT_TASK_PAYLOAD": str(branch_case.payload_path),
                "KENT_SESSION_PAYLOAD": str(branch_case.sessions_path),
            }
        )
        accepted = subprocess.run(
            [str(scripts / "workflow-plan-contract-accept")],
            cwd=branch_case.root,
            input=json.dumps(plan_input),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        accepted_output = assert_raw_edge_projection(
            self,
            accepted,
            spec=spec,
            edge_key="plan_contract_branch_identity",
        )
        self.assertEqual(accepted_output["transition"], "plan_contract_start")
        self.assertEqual(accepted_output["plan_route"], "start")
        self.assertEqual(
            accepted_output["plan_route_context"],
            "not-applicable",
        )
        self.assertNotIn("target_assignee", accepted_output)
        self.assertNotIn("target_thinking", accepted_output)

        branch_case.script = branch_script
        branch_case.coder_handoff = {
            key: accepted_output[key]
            for key in (
                "task_short_id",
                "accepted_plan_sha256",
                "plan_route",
                "plan_route_context",
                "review_context",
            )
        }
        branch_case.coder_handoff["delivery_context"] = delivery_context
        result, _ = branch_case.run_script(
            handoff=True,
            coder_selection=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        projected = assert_raw_edge_projection(
            self,
            result,
            spec=spec,
            edge_key="branch_identity_implement",
        )
        self.assertEqual(projected["transition"], "branch_identity_ready")
        self.assertEqual(projected["plan_route"], "start")
        self.assertEqual(projected["plan_route_context"], "not-applicable")
        self.assertEqual(projected["accepted_plan_sha256"],
                         accepted_output["accepted_plan_sha256"])
        self.assertEqual(projected["target_assignee"], "implementation-simple")
        self.assertEqual(projected["target_thinking"], "xhigh")

    def test_complexity_selection_rejects_fresh_writer_profile(self) -> None:
        from workflowkit.profile import ProjectProfile
        from workflowkit.model import SpecError

        profile_text = (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text()
        profile_text = profile_text.replace(
            'writer_sessions = "continuous"',
            'writer_sessions = "fresh_per_slice"',
        ).replace(
            'coder_selection = "disabled"',
            'coder_selection = "complexity"',
        )
        with self.assertRaisesRegex(
            SpecError,
            "coder_selection.*requires.*continuous",
        ):
            ProjectProfile.from_toml(
                REPO_ROOT,
                profile_text,
                check_files=False,
            )

    def test_decision_references_use_durable_plan_and_route_carriers(self) -> None:
        root = self.create_repository()
        plan = root / ".todo/task/plan.md"
        plan.parent.mkdir(parents=True)
        source = "fixture-session/question-1: human approved bounded feature"
        original = f"# Plan\n\nAuthority: {source}\n\n- [ ] Implement\n"
        plan.write_text(original)
        context = f"Read .todo/task/plan.md; source={source}"
        for route in ("start", "continue", "verify", "fix_continue"):
            with self.subTest(route=route):
                result = self.run_contract(
                    root, mode="accept", route=route, review_context=context,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                if route in {"start", "continue"}:
                    self.assertEqual(payload["plan_path"], ".todo/task/plan.md")
                    self.assertNotIn("review_context", payload)
                elif route == "verify":
                    self.assertEqual(payload["review_context"], context)
                else:
                    self.assertEqual(payload["fix_context"], "remaining fix bundle")
                self.assertIn(source, plan.read_text())
        plan.write_text(original.replace("[ ]", "[x]"))
        stable = self.run_contract(root, mode="check", review_context=context)
        self.assertEqual(stable.returncode, 0, stable.stderr)
        self.assertEqual(json.loads(stable.stdout)["transition"],
                         "plan_contract_continue_stable")
        plan.write_text(original + "\nNew acceptance criterion\n")
        changed = self.run_contract(root, mode="check", review_context=context)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertEqual(json.loads(changed.stdout)["review_context"], context)

    def test_invalid_supplied_delivery_context_fails_before_acceptance(self) -> None:
        root = self.create_repository()
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")
        result = self.run_contract(
            root,
            mode="accept",
            delivery_context='{"schema":"wrong","phase":"pre_pr"}',
        )
        self.assertEqual(result.returncode, 1)
        self.assertFalse((root / ".kent/runtime/TASK-PLAN/plan-contract.json").exists())

    def test_plan_contract_uses_materialized_runtime_sibling(self) -> None:
        root = self.create_repository()
        self.ensure_disabled_complexity_profile(root)
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement feature\n")
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        command = scripts / "workflow-plan-contract"
        command.write_bytes(PLAN_CONTRACT.read_bytes())
        command.chmod(0o755)
        (scripts / "workflow_runtime_contracts.py").write_bytes(
            (REPO_ROOT / "workflowkit" / "runtime.py").read_bytes()
        )
        context = json.dumps({
            "schema": "workflow-delivery-context-v1",
            "phase": "pre_pr",
        })
        result = subprocess.run(
            [str(command)],
            cwd=root,
            input=json.dumps({
                "workspace_path": str(root),
                "plan_path": ".todo/task/plan.md",
                "work_kind": "feature",
                "plan_route": "continue",
                "plan_route_context": "not-applicable",
                "review_context": "accepted preview",
                "task_short_id": "TASK-PLAN",
                "delivery_context": context,
            }),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, "KENT_PLAN_CONTRACT_MODE": "accept"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["delivery_context"],
            context,
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


class WorkflowVerificationDispatchTest(GitRepositoryTest):
    def test_dispatch_uses_opted_in_exact_producer_emitter(self) -> None:
        root = self.create_repository()
        kent = root / ".kent"
        scripts = kent / "scripts"
        scripts.mkdir(parents=True)
        (kent / "workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n')
        adapter = scripts / "workflow-checkpoint"
        adapter.write_text(
            "import json\n"
            "def emit_producer_result(root, payload, result, producer_kind):\n"
            "    assert producer_kind == 'verification-dispatch'\n"
            "    assert payload['review_context'] == 'verification evidence'\n"
            "    output = json.loads(result)\n"
            "    output['commentary'] += '\\nEXACT_CAPTURE_TEST'\n"
            "    return json.dumps(output).encode()\n")
        result = self.invoke(root, workspace_path=str(root), delivery_context=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["commentary"].endswith("EXACT_CAPTURE_TEST"))

    def test_dispatch_fails_closed_for_unsupported_delivery_reliability_policy(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v2"\n'
        )
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        checkpoint = scripts / "workflow-checkpoint"
        checkpoint.write_bytes(CHECKPOINT.read_bytes())
        checkpoint.chmod(0o755)

        result = self.invoke(
            root,
            workspace_path=str(root / "tracked.txt"),
            delivery_context=None,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def invoke(
        self,
        root: Path,
        *,
        workspace_path: str,
        delivery_context: str | None,
        runtime_sibling: bool = True,
        native_context: dict[str, str] | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        executable = scripts / "workflow-verification-dispatch"
        executable.write_bytes(VERIFICATION_DISPATCH.read_bytes())
        executable.chmod(0o755)
        support = scripts / "workflow_runtime_contracts.py"
        if runtime_sibling:
            support.write_bytes(
                (REPO_ROOT / "workflowkit" / "runtime.py").read_bytes()
            )
        else:
            support.unlink(missing_ok=True)
        payload = {
            "workspace_path": workspace_path,
            "review_context": "verification evidence",
        }
        if delivery_context is not None:
            payload["delivery_context"] = delivery_context
        if native_context is not None:
            payload["_kent"] = native_context
        return subprocess.run(
            [str(executable)],
            cwd=root,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, **(environment or {})},
        )

    def test_dispatch_preserves_context_on_success_and_invalid_workspace(self) -> None:
        root = self.create_repository()
        context = ' { "schema" : "workflow-delivery-context-v1", "phase" : "pre_pr" } '
        success = self.invoke(
            root,
            workspace_path=str(root),
            delivery_context=context,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        result = json.loads(success.stdout)
        self.assertEqual(result["transition"], "verification_dispatch_fanout_verify")
        self.assertEqual(result["delivery_context"], context)

        invalid = self.invoke(
            root,
            workspace_path=str(root / "tracked.txt"),
            delivery_context=context,
        )
        self.assertEqual(invalid.returncode, 0, invalid.stderr)
        result = json.loads(invalid.stdout)
        self.assertEqual(result["transition"], "verification_dispatch_invalid_workspace")
        self.assertEqual(result["delivery_context"], context)
        self.assertFalse((root / ".kent/scripts/__pycache__").exists())

    def test_dispatch_rejects_malformed_supplied_context_without_fallback(self) -> None:
        root = self.create_repository()
        result = self.invoke(
            root,
            workspace_path=str(root),
            delivery_context='{"schema":"wrong","phase":"pre_pr"}',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_legacy_dispatch_without_context_needs_no_runtime_sibling(self) -> None:
        root = self.create_repository()
        result = self.invoke(
            root,
            workspace_path=str(root),
            delivery_context=None,
            runtime_sibling=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["transition"],
            "verification_dispatch_fanout_verify",
        )

    def test_dispatch_rejects_oversized_input(self) -> None:
        root = self.create_repository()
        result = subprocess.run(
            [str(VERIFICATION_DISPATCH)],
            cwd=root,
            input=" " * (1024 * 1024 + 1),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def generated_spec(
        self,
        *,
        reliability: bool,
        delivery_context: bool,
        coder_selection: str = "disabled",
    ):
        from dataclasses import replace

        from workflowkit.delivery import build_delivery_workflow
        from workflowkit.profile import ProjectProfile

        profile = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
            check_files=False,
        )
        policies = dict(profile.policies)
        policies["coder_selection"] = coder_selection
        if reliability:
            policies["delivery_reliability"] = "v1"
        else:
            policies.pop("delivery_reliability", None)
        profile = replace(
            profile,
            policies=policies,
            delivery_profile=(
                profile.delivery_profile if delivery_context else "standard"
            ),
        )
        return build_delivery_workflow(profile, 1)

    def install_native_script_fixture(self, root: Path, spec):
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        for name in (
            "workflow-verification-dispatch",
            "workflow-checkpoint",
            "workflow-evidence-ledger",
            "workflow-plan-contract",
            "workflow-plan-contract-accept",
            "workflow-plan-contract-verify",
        ):
            source = (
                REPO_ROOT / "templates" / "project" / name
            )
            target = scripts / name
            target.write_bytes(source.read_bytes())
            target.chmod(0o755)
        (scripts / "workflow_runtime_contracts.py").write_bytes(
            (REPO_ROOT / "workflowkit" / "runtime.py").read_bytes()
        )

        canonical_root = root.resolve(strict=True)
        workflow_id = "44444444-4444-4444-8444-444444444444"
        agent_session = "11111111-1111-4111-8111-111111111111"
        agent_step = "22222222-2222-4222-8222-222222222222"
        selected_edge_keys = (
            "dispatch_invalid_workspace",
            "fix_verify",
            "plan_contract_checked_verify",
        )
        selected_edges = {
            key: next(edge for edge in spec.edges if edge.key == key)
            for key in selected_edge_keys
        }
        node_specs = {node.key: node for node in spec.nodes}
        from workflowkit.graph import plan_workflow_graph

        projected = plan_workflow_graph(
            spec,
            {
                "workflow_id": workflow_id,
                "expected_version": 1,
                "graph": {
                    "node_groups": [],
                    "nodes": [],
                    "transition_groups": [],
                    "edges": [],
                },
            },
            metadata_changed=False,
        ).document
        projected_graph = projected["graph"]
        node_ids = {
            node["key"]: node["id"] for node in projected_graph["nodes"]
        }
        detail = {
            "summary": {
                "id": "native-task",
                "short_id": "TASK-DISPATCH",
                "project_id": "native-project",
                "workflow_id": workflow_id,
            },
            "source_workspace": {
                "is_primary": True,
                "workspace_id": "native-primary",
                "root_path": str(canonical_root),
            },
            "project": {"default_workspace_id": "native-primary"},
            "worktree_path": str(canonical_root),
            "current_nodes": [
                {"node_id": node_ids["fix"], "session_id": agent_session},
            ],
            "live_sessions": [],
            "current_scripts": [],
        }
        graph = {
            "workflow_id": workflow_id,
            "expected_version": 1,
            "graph": projected_graph,
        }
        native_path = root / "native-fixture.json"
        native_path.write_text(json.dumps({"detail": detail, "graph": graph}))
        native_bin = root / "native-bin"
        native_bin.mkdir()
        fake_kent = native_bin / "kent"
        fake_kent.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "data = json.loads(Path(os.environ['KENT_TEST_NATIVE']).read_text())\n"
            "if args[0] == 'project':\n"
            "    detail = data['detail']\n"
            "    source = detail['source_workspace']['root_path']\n"
            "    workspace = detail.get('worktree_path', source)\n"
            "    print('native-project' if args[1] == workspace else 'foreign-project')\n"
            "elif args[:2] == ['task', 'show']: print(json.dumps(data['detail']))\n"
            "elif args[:3] == ['workflow', 'graph', 'inspect']:\n"
            "    print(json.dumps(data['graph']))\n"
            "else: sys.exit(19)\n"
        )
        fake_kent.chmod(0o700)
        home = root / "test-home"
        prepare_bin = home / ".kent" / "bin"
        prepare_bin.mkdir(parents=True)
        (prepare_bin / "kent-prepare-github-ci").symlink_to(
            REPO_ROOT / "scripts" / "prepare-github-ci"
        )
        environment = {
            "HOME": str(home),
            "PATH": str(native_bin) + os.pathsep + os.environ.get("PATH", ""),
            "KENT_TEST_NATIVE": str(native_path),
            "KENT_SESSION_ID": agent_session,
            "KENT_STEP_ID": agent_step,
        }

        def current_script(node_key: str) -> dict[str, str]:
            data = json.loads(native_path.read_text())
            node_id = node_ids[node_key]
            script_name = Path(node_specs[node_key].script_path).name
            data["detail"]["current_scripts"] = [
                {
                    "current_node": {"node_id": node_id},
                    "path": f".kent/scripts/{script_name}",
                },
            ]
            native_path.write_text(json.dumps(data))
            return {"task_id": "native-task", "node_id": node_id}

        def native_transition(edge_key: str) -> str:
            edge = selected_edges[edge_key]
            source_id = node_ids[edge.source]
            matches = [
                group for group in graph["graph"]["transition_groups"]
                if group["source_node_id"] == source_id
                and group["transition_id"] == edge.transition
            ]
            if len(matches) != 1:
                raise AssertionError(f"native group missing for {edge_key}")
            return matches[0]["transition_id"]

        return {
            "scripts": scripts,
            "native_path": native_path,
            "detail": detail,
            "graph": graph,
            "environment": environment,
            "canonical_root": canonical_root,
            "agent_identity": {
                "task_id": "native-task",
                "task_short_id": "TASK-DISPATCH",
                "project_id": "native-project",
                "workspace_path": str(canonical_root),
                "session_id": agent_session,
                "step_id": agent_step,
            },
            "current_script": current_script,
            "native_transition": native_transition,
        }

    def test_invalid_workspace_projection_keeps_v1_and_legacy_profiles_separate(self) -> None:
        context = (
            ' { "schema" : "workflow-delivery-context-v1", '
            '"phase" : "pre_pr" }\n'
        )
        for reliability in (False, True):
            for context_enabled in (False, True):
                with self.subTest(
                    reliability=reliability, context=context_enabled,
                ):
                    root = self.create_repository()
                    if reliability:
                        (root / ".kent" / "workflow-profile.toml").write_text(
                            '[policies]\ndelivery_reliability = "v1"\n'
                        )
                        adapter = root / ".kent" / "scripts" / "workflow-checkpoint"
                        adapter.parent.mkdir(parents=True)
                        adapter.write_text(
                            "def emit_producer_result(root, payload, result, producer_kind):\n"
                            "    assert producer_kind == 'verification-dispatch'\n"
                            "    return result\n"
                        )
                    else:
                        (root / ".kent" / "workflow-profile.toml").write_text(
                            "[policies]\n"
                        )
                    carried = context if context_enabled else None
                    result = self.invoke(
                        root,
                        workspace_path=str(root / "tracked.txt"),
                        delivery_context=carried,
                        runtime_sibling=context_enabled,
                    )
                    spec = self.generated_spec(
                        reliability=reliability,
                        delivery_context=context_enabled,
                    )
                    output = assert_raw_native_transition_projection(
                        self,
                        result,
                        spec=spec,
                        edge_key="dispatch_invalid_workspace",
                    )
                    self.assertEqual(output["reported_workspace_path"], str(root / "tracked.txt"))
                    if reliability:
                        self.assertEqual(
                            output["workspace_path"],
                            str(root.resolve(strict=True)),
                        )
                    else:
                        self.assertNotIn("workspace_path", output)
                    if context_enabled:
                        self.assertEqual(output["delivery_context"], context)
                    else:
                        self.assertNotIn("delivery_context", output)
                    self.assertNotIn("review_context", output)

    def test_v1_dispatch_capture_rejects_unqualified_or_foreign_native_identity(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
        )
        spec = self.generated_spec(reliability=True, delivery_context=True)
        fixture = self.install_native_script_fixture(root, spec)
        cases = (
            "missing native context",
            "mismatched _kent task",
            "foreign Task readback",
            "foreign project readback",
            "foreign source workspace",
            "foreign execution root",
            "foreign Script path",
        )
        for case in cases:
            with self.subTest(native_identity_case=case):
                context = fixture["current_script"]("verification_dispatch")
                data = json.loads(fixture["native_path"].read_text())
                data["detail"] = json.loads(json.dumps(fixture["detail"]))
                data["detail"]["current_scripts"] = [
                    {
                        "current_node": {"node_id": context["node_id"]},
                        "path": ".kent/scripts/workflow-verification-dispatch",
                    },
                ]
                selected_context: dict[str, str] | None = context
                if case == "missing native context":
                    selected_context = None
                elif case == "mismatched _kent task":
                    selected_context = {
                        **context,
                        "task_id": "foreign-task",
                    }
                elif case == "foreign Task readback":
                    data["detail"]["summary"]["id"] = "foreign-task"
                elif case == "foreign project readback":
                    data["detail"]["summary"]["project_id"] = "foreign-project"
                elif case == "foreign source workspace":
                    data["detail"]["source_workspace"]["root_path"] = str(
                        root.parent / "foreign-source"
                    )
                elif case == "foreign execution root":
                    data["detail"]["worktree_path"] = str(
                        root.parent / "foreign-worktree"
                    )
                elif case == "foreign Script path":
                    data["detail"]["current_scripts"][0]["path"] = (
                        ".kent/scripts/another-script"
                    )
                fixture["native_path"].write_text(json.dumps(data))
                result = self.invoke(
                    root,
                    workspace_path=str(root / "tracked.txt"),
                    delivery_context=None,
                    native_context=selected_context,
                    environment=fixture["environment"],
                )
                self.assertNotEqual(result.returncode, 0, case)
                self.assertEqual(result.stdout, "", case)

    def test_v1_invalid_dispatch_capture_packs_fix_verify_and_runs_plan_guard(self) -> None:
        context = (
            ' { "schema" : "workflow-delivery-context-v1", '
            '"phase" : "pre_pr" }\n'
        )
        root = self.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            '[policies]\n'
            'coder_selection = "disabled"\n'
            'delivery_reliability = "v1"\n'
        )
        spec = self.generated_spec(reliability=True, delivery_context=True)
        fixture = self.install_native_script_fixture(root, spec)
        scripts = fixture["scripts"]
        native_context = fixture["current_script"]("verification_dispatch")
        rejected_workspace = str(root / "tracked.txt")
        dispatched = self.invoke(
            root,
            workspace_path=rejected_workspace,
            delivery_context=context,
            native_context=native_context,
            environment=fixture["environment"],
        )
        dispatch_output = assert_raw_native_transition_projection(
            self,
            dispatched,
            spec=spec,
            edge_key="dispatch_invalid_workspace",
        )
        self.assertEqual(
            dispatch_output["transition"],
            fixture["native_transition"]("dispatch_invalid_workspace"),
        )
        self.assertNotEqual(
            dispatch_output["transition"],
            "dispatch_invalid_workspace",
        )
        self.assertEqual(
            dispatch_output["workspace_path"],
            str(fixture["canonical_root"]),
        )
        self.assertEqual(dispatch_output["reported_workspace_path"], rejected_workspace)
        self.assertEqual(dispatch_output["delivery_context"], context)

        from workflowkit import delivery

        dispatch_locator = delivery.producer_locator(
            dispatch_output["commentary"],
        )
        self.assertIsNotNone(dispatch_locator)
        adapter = load_template_module(
            scripts / "workflow-checkpoint",
            "dispatch_fixture_checkpoint_reader",
        )
        with mock.patch.dict(os.environ, fixture["environment"]):
            script_identity = delivery.observe_script_identity(
                fixture["canonical_root"],
                None,
                native_context,
                native_read=adapter.delivery_native_read,
            )
        dispatch_packet = delivery.reconstruct_producer(
            fixture["canonical_root"],
            dispatch_locator,
            identity=script_identity,
        )
        self.assertEqual(
            dispatch_packet["inputs"]["workspace_path"],
            rejected_workspace,
        )
        self.assertEqual(
            dispatch_packet["result"]["reported_workspace_path"],
            rejected_workspace,
        )
        self.assertEqual(
            dispatch_packet["result"]["workspace_path"],
            str(fixture["canonical_root"]),
        )
        self.assertEqual(dispatch_packet["inputs"]["delivery_context"], context)
        self.assertEqual(dispatch_packet["result"]["delivery_context"], context)

        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Verify workspace recovery\n")
        plan_context = fixture["current_script"]("plan_contract")
        accept_payload = {
            "workspace_path": str(fixture["canonical_root"]),
            "plan_path": ".todo/task/plan.md",
            "work_kind": "feature",
            "plan_route": "start",
            "plan_route_context": "not-applicable",
            "review_context": "Accepted test plan",
            "task_short_id": "TASK-DISPATCH",
            "delivery_context": context,
            "_kent": plan_context,
        }
        accepted = subprocess.run(
            [str(scripts / "workflow-plan-contract-accept")],
            cwd=root,
            input=json.dumps(accept_payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={
                **os.environ,
                **fixture["environment"],
                "KENT_PLAN_CONTRACT_MODE": "accept",
            },
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(
            json.loads(accepted.stdout)["transition"],
            "plan_contract_start",
        )

        pack_identity = fixture["agent_identity"]
        pack_transition = next(
            edge for edge in spec.edges if edge.key == "fix_verify"
        ).transition
        pack_request = {
            "schema": "workflow-pack-delivery-v1",
            "transition": pack_transition,
            "values": {
                "review_context": "Workspace rejection handled by the Fix owner",
                "task_short_id": "TASK-DISPATCH",
            },
            "transition_contract_ref": delivery.transition_contract_reference(
                pack_identity,
                fixture["detail"],
                fixture["graph"],
                pack_transition,
            ),
            "delivery_mode": "preserve",
            "producer_refs": [dispatch_locator],
        }
        packed = subprocess.run(
            [
                sys.executable,
                str(scripts / "workflow-checkpoint"),
                "pack-delivery",
                "--task",
                "TASK-DISPATCH",
                "--workspace",
                str(fixture["canonical_root"]),
            ],
            cwd=root,
            input=json.dumps(pack_request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, **fixture["environment"]},
        )
        packed_output = assert_raw_native_transition_projection(
            self,
            packed,
            spec=spec,
            edge_key="fix_verify",
        )
        self.assertEqual(
            packed_output["transition"],
            fixture["native_transition"]("fix_verify"),
        )
        self.assertEqual(
            packed_output["workspace_path"],
            str(fixture["canonical_root"]),
        )
        self.assertEqual(packed_output["delivery_context"], context)
        self.assertNotIn("reported_workspace_path", packed_output)

        def invoke_pack(request: dict[str, object]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    str(scripts / "workflow-checkpoint"),
                    "pack-delivery",
                    "--task",
                    "TASK-DISPATCH",
                    "--workspace",
                    str(fixture["canonical_root"]),
                ],
                cwd=root,
                input=json.dumps(request),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={**os.environ, **fixture["environment"]},
            )

        for label, values in (
            (
                "foreign completion workspace",
                {
                    **pack_request["values"],
                    "workspace_path": str(root / "tracked.txt"),
                },
            ),
            (
                "rewritten delivery context",
                {
                    **pack_request["values"],
                    "delivery_context": "rewritten context",
                },
            ),
        ):
            with self.subTest(negative_control=label):
                denied = invoke_pack({**pack_request, "values": values})
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(denied.stdout, "")

        stale_locator, _ = delivery.capture_producer(
            fixture["canonical_root"],
            identity=script_identity,
            producer_kind="historical-dispatch",
            inputs={"workspace_path": rejected_workspace},
            result={
                "transition": dispatch_output["transition"],
                "commentary": "immutable historical producer",
            },
        )
        conflicting_refs = invoke_pack({
            **pack_request,
            "producer_refs": [dispatch_locator, stale_locator],
        })
        self.assertNotEqual(conflicting_refs.returncode, 0)
        self.assertEqual(conflicting_refs.stdout, "")

        verify_payload = {
            key: value
            for key, value in packed_output.items()
            if key not in {"transition", "commentary"}
        }
        verify_payload["_kent"] = fixture["current_script"](
            "plan_contract_verify",
        )
        verified = subprocess.run(
            [str(scripts / "workflow-plan-contract-verify")],
            cwd=root,
            input=json.dumps(verify_payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, **fixture["environment"]},
        )
        verify_output = assert_raw_native_transition_projection(
            self,
            verified,
            spec=spec,
            edge_key="plan_contract_checked_verify",
        )
        self.assertEqual(
            verify_output["transition"],
            fixture["native_transition"]("plan_contract_checked_verify"),
        )
        self.assertEqual(verify_output["delivery_context"], context)
        self.assertEqual(
            verify_output["workspace_path"],
            str(fixture["canonical_root"]),
        )

    def test_r2_missing_authority_references_project_to_revalidation_edges(self) -> None:
        import contextlib
        import io

        from workflowkit.project_memory import ProjectMemory

        context = (
            ' { "schema" : "workflow-delivery-context-v1", '
            '"phase" : "pre_pr" }\n'
        )
        for selection_policy, prior_acceptance in (
            ("disabled", False),
            ("complexity", True),
        ):
            with self.subTest(
                selection_policy=selection_policy,
                prior_acceptance=prior_acceptance,
            ):
                root = self.create_repository()
                (root / ".kent" / "workflow-profile.toml").write_text(
                    "[policies]\n"
                    f'coder_selection = "{selection_policy}"\n'
                    "[project_memory]\n"
                    'mode = "r2"\n'
                )
                scripts = root / ".kent" / "scripts"
                scripts.mkdir(parents=True)
                plan_script = scripts / "workflow-plan-contract"
                plan_script.write_bytes(PLAN_CONTRACT.read_bytes())
                (scripts / "workflow_runtime_contracts.py").write_bytes(
                    (REPO_ROOT / "workflowkit" / "runtime.py").read_bytes()
                )
                module = load_template_module(
                    plan_script,
                    f"r2_missing_refs_{selection_policy}",
                )
                store = ProjectMemory.initialize(
                    root.resolve(strict=True).parent
                    / f"memory-{root.name}-{selection_policy}",
                    "kit",
                    f"project-{selection_policy}",
                )
                task = "TASK-B3"
                task_record = store.initialize_task(task, "task-binding", [])
                selection = {
                    "schema": "coder-selection-v1",
                    "task_short_id": task,
                    "complexity": "simple",
                    "rationale": "The accepted scope has a bounded implementation path.",
                }
                selection_block = (
                    "\n```json\n" + json.dumps(selection) + "\n```\n"
                    if selection_policy == "complexity"
                    else ""
                )
                current_plan = (
                    "Current plan contains no original source locator.\n"
                    + selection_block
                )
                previous_digest = None
                expected_head = None
                if prior_acceptance:
                    previous_revision = store.publish(
                        f"tasks/{task}/plan",
                        None,
                        "Previously reviewed plan.\n"
                        "session_message(project/session/event/digest)\n"
                        + selection_block,
                    )
                    accepted = store.accept_plan(
                        task,
                        previous_revision.token,
                        None,
                        {
                            "work_kind": "feature",
                            "authority_refs": [
                                "session_message(project/session/event/digest)"
                            ],
                            "plan_kind": "markdown",
                            "coder_selection": selection,
                        },
                    )
                    previous_digest = json.loads(
                        accepted.path.read_bytes()
                    )["normalized_sha256"]
                    working = store.publish(
                        f"tasks/{task}/plan",
                        previous_revision.token,
                        current_plan,
                    )
                    expected_head = store.accepted_head(task)
                else:
                    working = store.publish(
                        f"tasks/{task}/plan",
                        None,
                        current_plan,
                    )

                normalized_digest = hashlib.sha256(
                    module.normalized_plan(working.path).encode("utf-8")
                ).hexdigest()
                task_binding = hashlib.sha256(
                    (store.path / "tasks" / task / "task.json").read_bytes()
                ).hexdigest()
                call_log: list[str] = []

                def memory_call(_workspace, _payload, operation, **fields):
                    call_log.append(operation)
                    if operation == "resolve-task":
                        return {"mode": "r2"}
                    if operation == "accepted-head":
                        return {"accepted_snapshot": store.accepted_head(task)}
                    if operation == "resolve-plan":
                        current = store.read(f"tasks/{task}/plan")
                        supplied = (
                            current
                            if fields["plan_path"] == "not-applicable"
                            else store.plan_revision(task, fields["plan_path"])
                        )
                        comparison = store.compare_plan_revisions(
                            task, supplied.token, current.token,
                        )
                        return {
                            "supplied_revision": supplied.token,
                            "supplied_sha256": comparison["supplied_sha256"],
                            "supplied_comparison": comparison["comparison"],
                            "working_path": str(current.path),
                            "working_revision": current.token,
                            "working_sha256": comparison["working_sha256"],
                        }
                    if operation == "check-plan":
                        status = store.check_plan(
                            task,
                            coder_selection=fields["coder_selection"],
                        )
                        status["cache_ref"] = {
                            "schema": "r2-plan-contract-cache-v1",
                            "task_short_id": task,
                            "project_uid": f"project-{selection_policy}",
                            "task_uid": task_record["task_uid"],
                            "task_binding_sha256": task_binding,
                            "accepted_snapshot_path": status["accepted_path"],
                            "accepted_snapshot_sha256": status["accepted_snapshot"],
                        }
                        return status
                    raise AssertionError(f"unexpected memory operation: {operation}")

                payload = {
                    "workspace_path": str(root.resolve(strict=True)),
                    "task_short_id": task,
                    "plan_path": str(working.path),
                    "work_kind": "feature",
                    "review_context": "Review context has no original source locator.",
                    "plan_route": "verify",
                    "plan_route_context": "not-applicable",
                    "delivery_context": context,
                }
                if selection_policy == "complexity":
                    payload["accepted_plan_sha256"] = previous_digest
                    payload["reviewed_normalized_sha256"] = normalized_digest

                raw_stdout = io.StringIO()
                with (
                    mock.patch.dict(
                        module.execute.__globals__,
                        {"memory_call": memory_call},
                    ),
                    mock.patch.dict(
                        os.environ,
                        {"KENT_PLAN_CONTRACT_MODE": "accept"},
                    ),
                    mock.patch("pathlib.Path.cwd", return_value=root),
                    mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
                    contextlib.redirect_stdout(raw_stdout),
                ):
                    result_code = module.main()
                raw_result = subprocess.CompletedProcess(
                    ["workflow-plan-contract"],
                    result_code,
                    raw_stdout.getvalue(),
                    "",
                )
                spec = self.generated_spec(
                    reliability=False,
                    delivery_context=True,
                    coder_selection=selection_policy,
                )
                output = assert_raw_native_transition_projection(
                    self,
                    raw_result,
                    spec=spec,
                    edge_key="plan_contract_revalidate",
                )
                self.assertEqual(output["delivery_context"], context)
                if selection_policy == "complexity":
                    self.assertEqual(
                        output["accepted_plan_sha256"],
                        previous_digest,
                    )
                    self.assertNotEqual(
                        output["accepted_plan_sha256"],
                        normalized_digest,
                    )
                else:
                    self.assertNotIn("accepted_plan_sha256", output)
                self.assertEqual(store.accepted_head(task), expected_head)
                self.assertNotIn("accept-plan", call_log)
                self.assertFalse(
                    (root / ".kent" / "runtime" / task / "plan-contract.json").exists()
                )
                self.assertFalse((root / ".todo").exists())

    def test_r2_acceptance_wrapper_projects_initial_and_reacceptance_sources(self) -> None:
        import contextlib
        import io

        from workflowkit.project_memory import ProjectMemory

        context = (
            ' { "schema" : "workflow-delivery-context-v1", '
            '"phase" : "pre_pr" }\n'
        )
        locators = {
            "plan": "session_message(project/plan-session/plan-event/plan-digest)",
            "review_context": (
                "question_answer(project/review-session/review-event/review-digest)"
            ),
            "validated_ledger": (
                "task_comment(project/ledger-task/comment-event/comment-digest)"
            ),
        }
        cases = (
            ("disabled", "markdown", False, "plan"),
            ("disabled", "markdown", True, "review_context"),
            ("disabled", "markdown", False, "validated_ledger"),
            ("disabled", "report_only", False, "review_context"),
            ("disabled", "report_only", True, "validated_ledger"),
            ("complexity", "markdown", False, "plan"),
            ("complexity", "markdown", True, "review_context"),
            ("complexity", "markdown", False, "validated_ledger"),
        )
        source = REPO_ROOT / "templates" / "project" / "workflow-plan-contract"

        for index, (selection_policy, plan_kind, reaccept, reference_source) in enumerate(cases):
            with self.subTest(
                selection_policy=selection_policy,
                plan_kind=plan_kind,
                reaccept=reaccept,
                reference_source=reference_source,
            ):
                root = self.create_repository()
                (root / ".kent" / "workflow-profile.toml").write_text(
                    "[policies]\n"
                    f'coder_selection = "{selection_policy}"\n'
                    + (
                        'writer_sessions = "continuous"\n'
                        if selection_policy == "complexity"
                        else ""
                    )
                    + "[project_memory]\nmode = \"r2\"\n"
                )
                scripts = root / ".kent" / "scripts"
                scripts.mkdir(parents=True)
                plan_script = scripts / "workflow-plan-contract"
                plan_script.write_bytes(source.read_bytes())
                plan_script.chmod(0o755)
                (scripts / "workflow_runtime_contracts.py").write_bytes(
                    (REPO_ROOT / "workflowkit" / "runtime.py").read_bytes()
                )
                evidence_script = scripts / "workflow-evidence-ledger"
                evidence_script.write_bytes(EVIDENCE.read_bytes())
                evidence_script.chmod(0o755)

                module = load_template_module(
                    plan_script,
                    f"r2_acceptance_wrapper_{selection_policy}_{index}",
                )
                store = ProjectMemory.initialize(
                    root.resolve(strict=True).parent
                    / f"memory-{root.name}-{index}",
                    "kit",
                    f"project-{selection_policy}-{index}",
                )
                task = f"TASK-B{index + 10}"
                task_record = store.initialize_task(task, "task-binding", [])
                selection = {
                    "schema": "coder-selection-v1",
                    "task_short_id": task,
                    "complexity": "simple",
                    "rationale": "The reviewed scope has a bounded implementation path.",
                }
                selection_block = (
                    "\n```json\n" + json.dumps(selection) + "\n```\n"
                    if selection_policy == "complexity"
                    else ""
                )
                locator = locators[reference_source]

                def plan_content(reference: str | None) -> str:
                    if plan_kind == "report_only":
                        return "not-applicable\n"
                    return (
                        "# Accepted plan\n\n"
                        "- [ ] Implement the reviewed change.\n"
                        + (f"\n{reference}\n" if reference is not None else "")
                        + selection_block
                    )

                current_reference = (
                    locator if reference_source == "plan" else None
                )
                previous_head = None
                prior_digest = None
                if reaccept:
                    previous = store.publish(
                        f"tasks/{task}/plan",
                        None,
                        plan_content("session_message(project/prior/session/prior-digest)"),
                    )
                    prior_metadata = {
                        "work_kind": "feature",
                        "authority_refs": [
                            "session_message(project/prior/session/prior-digest)"
                        ],
                        "plan_kind": plan_kind,
                    }
                    if selection_policy == "complexity":
                        prior_metadata["coder_selection"] = selection
                    previous_acceptance = store.accept_plan(
                        task, previous.token, None, prior_metadata,
                    )
                    prior_snapshot = json.loads(
                        previous_acceptance.path.read_bytes()
                    )
                    prior_digest = prior_snapshot["normalized_sha256"]
                    working = store.publish(
                        f"tasks/{task}/plan",
                        previous.token,
                        plan_content(current_reference),
                    )
                    previous_head = store.accepted_head(task)
                elif plan_kind == "report_only":
                    working = None
                else:
                    working = store.publish(
                        f"tasks/{task}/plan",
                        None,
                        plan_content(current_reference),
                    )

                if reference_source == "validated_ledger":
                    ledger = root / ".kent" / "runtime" / task / "evidence-ledger.jsonl"
                    ledger.parent.mkdir(parents=True, exist_ok=True)
                    evidence = load_template_module(
                        evidence_script,
                        f"r2_acceptance_evidence_{index}",
                    )
                    with mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER):
                        evidence.append_entry(
                            root.resolve(strict=True),
                            ledger,
                            task=task,
                            _payload={
                                "node_key": "plan",
                                "evidence_type": "plan_review",
                                "summary": locator,
                                "artifacts": [],
                                "checks": [],
                                "decisions": [],
                                "context": {
                                    "manifest_path": ".kent/workflow-profile.toml",
                                    "files_read": [],
                                },
                            },
                        )

                native_bin = root / "native-bin"
                native_bin.mkdir()
                fake_kent = native_bin / "kent"
                fake_kent.write_text(
                    f"#!{sys.executable}\n"
                    "import json\n"
                    "print(json.dumps({'task_id': 'native-task', 'items': []}))\n"
                )
                fake_kent.chmod(0o700)

                task_binding = hashlib.sha256(
                    (store.path / "tasks" / task / "task.json").read_bytes()
                ).hexdigest()
                call_log: list[str] = []

                def memory_call(_workspace, _payload, operation, **fields):
                    call_log.append(operation)
                    if operation == "resolve-task":
                        return {"mode": "r2"}
                    if operation == "accepted-head":
                        return {"accepted_snapshot": store.accepted_head(task)}
                    if operation == "prepare-report-only":
                        revision = store.publish(
                            f"tasks/{task}/plan",
                            None,
                            "not-applicable\n",
                            operation_id=f"report-only-{index}",
                            provenance=fields["provenance"],
                        )
                        return {
                            "token": revision.token,
                            "path": str(revision.path),
                            "sha256": revision.sha256,
                        }
                    if operation == "resolve-plan":
                        current = store.read(f"tasks/{task}/plan")
                        supplied = (
                            current
                            if fields["plan_path"] == "not-applicable"
                            else store.plan_revision(task, fields["plan_path"])
                        )
                        comparison = store.compare_plan_revisions(
                            task, supplied.token, current.token,
                        )
                        return {
                            "supplied_revision": supplied.token,
                            "supplied_sha256": comparison["supplied_sha256"],
                            "supplied_comparison": comparison["comparison"],
                            "working_path": str(current.path),
                            "working_revision": current.token,
                            "working_sha256": comparison["working_sha256"],
                        }
                    if operation == "check-plan":
                        status = store.check_plan(
                            task,
                            coder_selection=fields["coder_selection"],
                        )
                        status["cache_ref"] = {
                            "schema": "r2-plan-contract-cache-v1",
                            "task_short_id": task,
                            "project_uid": f"project-{selection_policy}-{index}",
                            "task_uid": task_record["task_uid"],
                            "task_binding_sha256": task_binding,
                            "accepted_snapshot_path": status["accepted_path"],
                            "accepted_snapshot_sha256": status["accepted_snapshot"],
                        }
                        return status
                    if operation == "accept-plan":
                        accepted = store.accept_plan(
                            task,
                            fields["expected_revision"],
                            fields["expected_accepted"],
                            fields["metadata"],
                        )
                        return {"token": accepted.token}
                    raise AssertionError(f"unexpected memory operation: {operation}")

                review_context = (
                    "Reviewed authority "
                    + locator
                    if reference_source == "review_context"
                    else "Reviewed implementation scope."
                )
                payload = {
                    "workspace_path": str(root.resolve(strict=True)),
                    "task_short_id": task,
                    "plan_path": (
                        "not-applicable"
                        if plan_kind == "report_only"
                        else str(working.path)
                    ),
                    "work_kind": "feature",
                    "review_context": review_context,
                    "plan_route": "start",
                    "plan_route_context": "not-applicable",
                    "delivery_context": context,
                    "_kent": {"task_id": "native-task"},
                }
                if selection_policy == "complexity":
                    current_revision = (
                        store.read(f"tasks/{task}/plan")
                        if plan_kind == "report_only"
                        else working
                    )
                    normalized_digest = hashlib.sha256(
                        module.normalized_plan(current_revision.path).encode("utf-8")
                    ).hexdigest()
                    payload["accepted_plan_sha256"] = (
                        prior_digest if prior_digest is not None else "not-applicable"
                    )
                    payload["reviewed_normalized_sha256"] = normalized_digest

                spec = self.generated_spec(
                    reliability=False,
                    delivery_context=True,
                    coder_selection=selection_policy,
                )
                revalidate_edges = [
                    edge for edge in spec.edges
                    if edge.key == "plan_contract_revalidate"
                ]
                self.assertEqual(len(revalidate_edges), 1)
                self.assertEqual(revalidate_edges[0].source, "plan_contract")
                self.assertEqual(revalidate_edges[0].target, "plan_revalidation")
                self.assertEqual(revalidate_edges[0].context, "continue_session")
                self.assertEqual(revalidate_edges[0].context_source, "node:plan")
                self.assertFalse(revalidate_edges[0].requires_approval)
                node_keys = {node.key for node in spec.nodes}
                self.assertTrue({"plan", "plan_review", "plan_contract"} <= node_keys)
                plan_approval_edges = [
                    edge for edge in spec.edges
                    if edge.key == "plan_review_accept"
                    and edge.source == "plan_review"
                    and edge.target == "plan_contract"
                ]
                self.assertEqual(len(plan_approval_edges), 1)

                raw_stdout = io.StringIO()
                with (
                    mock.patch.dict(
                        module.execute.__globals__,
                        {"memory_call": memory_call},
                    ),
                    mock.patch.dict(
                        os.environ,
                        {
                            "KENT_PLAN_CONTRACT_MODE": "accept",
                            "KENT_BIN": str(fake_kent),
                        },
                    ),
                    mock.patch("pathlib.Path.cwd", return_value=root),
                    mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
                    contextlib.redirect_stdout(raw_stdout),
                ):
                    result_code = module.main()
                raw_result = subprocess.CompletedProcess(
                    ["workflow-plan-contract"],
                    result_code,
                    raw_stdout.getvalue(),
                    "",
                )
                output = assert_raw_native_transition_projection(
                    self,
                    raw_result,
                    spec=spec,
                    edge_key="plan_contract_implement",
                )
                self.assertEqual(output["transition"], "plan_contract_start")
                self.assertEqual(output["delivery_context"], context)
                self.assertEqual(output["workspace_path"], str(root.resolve(strict=True)))
                self.assertEqual(output["plan_path"], payload["plan_path"])
                if selection_policy == "complexity":
                    self.assertEqual(output["target_assignee"], "implementation-simple")
                    self.assertEqual(output["target_thinking"], "xhigh")
                else:
                    self.assertNotIn("accepted_plan_sha256", output)
                    self.assertNotIn("target_assignee", output)
                    self.assertNotIn("target_thinking", output)

                head = store.accepted_head(task)
                self.assertIsInstance(head, str)
                self.assertNotEqual(head, previous_head)
                accepted_status = store.check_plan(
                    task,
                    coder_selection=(
                        selection if selection_policy == "complexity" else None
                    ),
                )
                accepted_snapshot = json.loads(
                    Path(accepted_status["accepted_path"]).read_bytes()
                )
                if selection_policy == "complexity":
                    self.assertEqual(
                        output["accepted_plan_sha256"],
                        accepted_snapshot["normalized_sha256"],
                    )
                    if prior_digest is not None:
                        self.assertNotEqual(
                            output["accepted_plan_sha256"],
                            prior_digest,
                        )
                self.assertEqual(
                    accepted_snapshot["authority_refs"],
                    [locator],
                )
                self.assertEqual(accepted_snapshot["plan_kind"], plan_kind)
                self.assertEqual(accepted_snapshot["parent"], previous_head)
                self.assertEqual(
                    "prepare-report-only" in call_log,
                    plan_kind == "report_only" and not reaccept,
                )
                self.assertIn("accept-plan", call_log)
                self.assertTrue(
                    (
                        root / ".kent" / "runtime" / task / "plan-contract.json"
                    ).is_file()
                )
                self.assertFalse((root / ".todo").exists())


class AndroidApkInstallPreserveTest(GitRepositoryTest):
    ANDROID = "{http://schemas.android.com/apk/res/android}"

    def candidate_manifest(
        self,
        *,
        version: str | None = "10",
        name: str | None = "1.0",
        major: str | None = None,
        runners: tuple[tuple[str, str], ...] = (),
        test_only: str | None = None,
    ) -> str:
        attributes = {"package": "com.example.app"}
        for attribute, value in (
            ("versionCode", version),
            ("versionName", name),
            ("versionCodeMajor", major),
        ):
            if value is not None:
                attributes[self.ANDROID + attribute] = value
        manifest = ET.Element("manifest", attributes)
        for runner, target in runners:
            ET.SubElement(manifest, "instrumentation", {
                self.ANDROID + "name": runner,
                self.ANDROID + "targetPackage": target,
            })
        application = ET.SubElement(manifest, "application")
        if test_only is not None:
            application.set(self.ANDROID + "testOnly", test_only)
        return ET.tostring(manifest, encoding="unicode")

    def create_fake_tools(
        self,
        root: Path,
        *,
        installed_version: int | None,
        installed_signer: str | None = "aa",
        install_failure: str = "",
        installed_present: bool | None = None,
        candidate_manifest: str | None = None,
        candidate_version: str = "10",
        candidate_name: str = "1.0",
        candidate_signer: str | None = "aa",
    ) -> tuple[Path, dict[str, str]]:
        present = installed_version is not None if installed_present is None else installed_present
        manifest = self.candidate_manifest() if candidate_manifest is None else candidate_manifest
        tools = root / "fake-tools"
        tools.mkdir()
        installed_apk = tools / "installed.apk"
        installed_apk.write_bytes(b"installed-apk")
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
                if present
                else "  'shell pm') exit 1 ;;\n"
            )
            + (
                f"  'shell dumpsys') echo 'versionCode={installed_version}' ;;\n"
                if installed_version is not None
                else "  'shell dumpsys') exit 1 ;;\n"
            )
            + (
                f"  'pull /data/app/base.apk') cp {shlex.quote(str(installed_apk))} \"$3\" ;;\n"
                if present
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
            f"  version-code) echo {shlex.quote(candidate_version)} ;;\n"
            f"  version-name) echo {shlex.quote(candidate_name)} ;;\n"
            f"  print) printf '%s\\n' {shlex.quote(manifest)} ;;\n"
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
            + (
                f"  *) echo 'Signer #1 certificate SHA-256 digest: {candidate_signer}' ;;\n"
                if candidate_signer is not None
                else "  *) exit 1 ;;\n"
            )
            + "esac\n"
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

    def test_compatible_ordinary_replacement_retains_install_arguments(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(root, installed_version=10)
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["classification"], "compatible_replace")
        self.assertEqual(payload["candidate"]["version_code_source"], "declared")
        self.assertIsNone(payload["candidate"]["instrumentation_target_package"])
        self.assertIn("-s emulator-5554 install -r ", log.read_text())
        self.assertNotIn(" -t", log.read_text())

    def test_versionless_instrumentation_uses_platform_defaults(self) -> None:
        for installed_version in (None, 0):
            with self.subTest(installed_version=installed_version):
                root = self.create_repository()
                log, env = self.create_fake_tools(
                    root,
                    installed_version=installed_version,
                    candidate_manifest=self.candidate_manifest(
                        version=None,
                        name=None,
                        runners=(("com.example.Runner", "com.example.target"),),
                    ),
                    candidate_version="UNKNOWN",
                    candidate_name="UNKNOWN",
                )
                result = self.run_installer(root, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["outcome"], "installed")
                self.assertFalse(payload["destructive_action"])
                self.assertEqual(payload["candidate"]["version_code"], 0)
                self.assertEqual(payload["candidate"]["version_code_source"], "platform_default")
                self.assertIsNone(payload["candidate"]["version_name"])
                self.assertEqual(
                    payload["candidate"]["instrumentation_target_package"], "com.example.target"
                )
                commands = log.read_text()
                self.assertIn("-s emulator-5554 install -r -t ", commands)
                self.assertNotIn("uninstall", commands)
                self.assertNotIn(" pm clear", commands)
                self.assertNotIn(" -d", commands)

    def test_versionless_instrumentation_cannot_downgrade(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(
            root,
            installed_version=1,
            candidate_manifest=self.candidate_manifest(
                version=None, name=None,
                runners=(("com.example.Runner", "com.example.target"),),
            ),
            candidate_version="UNKNOWN",
        )
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["classification"], "downgrade_blocked")
        self.assertNotIn(" install ", log.read_text())

    def test_versioned_instrumentation_retains_declared_metadata(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(
            root,
            installed_version=10,
            candidate_manifest=self.candidate_manifest(
                runners=(("com.example.Runner", "com.example.target"),),
            ),
        )
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate = json.loads(result.stdout)["candidate"]
        self.assertEqual(candidate["version_code"], 10)
        self.assertEqual(candidate["version_code_source"], "declared")
        self.assertEqual(candidate["version_name"], "1.0")
        self.assertIn("install -r -t", log.read_text())

    def test_unknown_declared_version_result_is_not_defaulted(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(
            root,
            installed_version=None,
            candidate_manifest=self.candidate_manifest(
                runners=(("com.example.Runner", "com.example.target"),),
            ),
            candidate_version="UNKNOWN",
        )
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(log.exists())

    def test_unknown_installed_version_blocks_all_artifact_kinds(self) -> None:
        for instrumentation in (False, True):
            with self.subTest(instrumentation=instrumentation):
                root = self.create_repository()
                log, env = self.create_fake_tools(
                    root,
                    installed_version=None,
                    installed_present=True,
                    candidate_manifest=self.candidate_manifest(
                        version=None if instrumentation else "10",
                        runners=(("com.example.Runner", "com.example.target"),)
                        if instrumentation else (),
                    ),
                )
                result = self.run_installer(root, env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["classification"], "installed_version_unknown"
                )
                self.assertNotIn(" install ", log.read_text())

    def test_mismatched_installed_signer_blocks_install_attempt(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(root, installed_version=10, installed_signer="bb")
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["classification"], "signer_mismatch")
        self.assertNotIn(" install ", log.read_text())

    def test_invalid_candidate_metadata_never_accesses_adb(self) -> None:
        runner = (("com.example.Runner", "com.example.target"),)
        invalid_manifests = {
            "ordinary missing version": self.candidate_manifest(version=None),
            "declared unknown version": self.candidate_manifest(version="UNKNOWN", runners=runner),
            "declared negative version": self.candidate_manifest(version="-1", runners=runner),
            "declared empty version": self.candidate_manifest(version="", runners=runner),
            "absent minor nonzero major": self.candidate_manifest(version=None, major="1", runners=runner),
            "absent minor malformed major": self.candidate_manifest(
                version=None, major="UNKNOWN", runners=runner
            ),
            "invalid XML": "<manifest",
            "invalid root": "<other/>",
            "package mismatch": self.candidate_manifest().replace("com.example.app", "com.example.other"),
            "empty runner": self.candidate_manifest(version=None, runners=(("", "com.example.target"),)),
            "empty target": self.candidate_manifest(version=None, runners=(("com.example.Runner", ""),)),
            "conflicting targets": self.candidate_manifest(
                version=None,
                runners=runner + (("com.example.OtherRunner", "com.example.other"),),
            ),
            "wrong namespace": self.candidate_manifest(version=None, runners=runner).replace(
                "http://schemas.android.com/apk/res/android", "urn:wrong"
            ),
            "invalid testOnly": self.candidate_manifest(test_only="UNKNOWN"),
            "empty testOnly": self.candidate_manifest(test_only=""),
            "multiple applications": self.candidate_manifest().replace(
                "</manifest>", "<application/></manifest>"
            ),
        }
        for label, manifest in invalid_manifests.items():
            with self.subTest(label=label):
                root = self.create_repository()
                log, env = self.create_fake_tools(
                    root, installed_version=None, candidate_manifest=manifest
                )
                result = self.run_installer(root, env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("android-apk-install-preserve:", result.stderr)
                self.assertFalse(log.exists(), "Rejected metadata must not access a device")

    def test_candidate_signature_failure_never_accesses_adb(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(
            root, installed_version=None, candidate_signer=None
        )
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(log.exists())

    def test_multiple_runners_for_same_target_and_zero_major_are_supported(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(
            root,
            installed_version=0,
            candidate_manifest=self.candidate_manifest(
                version=None,
                major="0",
                runners=(
                    ("com.example.Runner", "com.example.target"),
                    ("com.example.OtherRunner", "com.example.target"),
                ),
            ),
        )
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["candidate"]["instrumentation_target_package"], "com.example.target"
        )
        self.assertIn("install -r -t", log.read_text())

    def test_test_only_flag_is_added_only_when_declared(self) -> None:
        for test_only in ("true", "false", None):
            with self.subTest(test_only=test_only):
                root = self.create_repository()
                log, env = self.create_fake_tools(
                    root,
                    installed_version=None,
                    candidate_manifest=self.candidate_manifest(test_only=test_only),
                )
                result = self.run_installer(root, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("install -r -t" in log.read_text(), test_only == "true")

    def test_install_failure_has_no_destructive_retry(self) -> None:
        root = self.create_repository()
        log, env = self.create_fake_tools(
            root, installed_version=10, install_failure="VERSION_DOWNGRADE"
        )
        result = self.run_installer(root, env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["classification"], "downgrade_blocked")
        commands = log.read_text()
        self.assertEqual(commands.count("install -r"), 1)
        self.assertNotIn("uninstall", commands)
        self.assertNotIn(" pm clear", commands)
        self.assertNotIn(" -d", commands)


class WorkflowEvidenceLedgerTest(GitRepositoryTest):
    def test_historical_retention_selects_original_digest_before_new_receipt(self):
        import workflowkit.runtime as runtime
        for phase in ("after_retained_file_fsync", "before_retention_publish", "after_retention_publish"):
            with self.subTest(phase=phase):
                root, scripts, directory, original = self.recovered_fixture()
                for name in runtime.EVIDENCE_RECOVERY_FILES:
                    (directory / name).unlink()
                (directory / "evidence-ledger.jsonl").write_bytes(original.splitlines(keepends=True)[0])
                plan = root / ".todo/task/plan.md"
                plan.parent.mkdir(parents=True)
                plan.write_text("historical plan\n")
                report = root / "redaction.json"
                report.write_bytes(b'{"status":"passed"}')
                ledger = load_template_module(scripts / "workflow-evidence-ledger", "historical_retention")
                owner = {
                    "session_id": "11111111-1111-4111-8111-111111111111",
                    "step_id": "22222222-2222-4222-8222-222222222222",
                }
                source = self.create_repository().resolve()
                identity = self.native_fixture(root, source, owner)
                lookup = lambda *_: identity
                environment = {"KENT_" + key.upper(): value for key, value in owner.items()}
                with mock.patch.dict(os.environ, environment):
                    ledger.append_entry(root, directory / "evidence-ledger.jsonl", task="TASK-4", _payload={
                        "node_key": "cleanup", "evidence_type": "cleanup", "summary": "final",
                        "artifacts": ["redaction.json"],
                        "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
                    })
                    with mock.patch.object(ledger, "read_json_input", return_value={
                        "schema": runtime.TERMINAL_SEAL_REQUEST_SCHEMA,
                        "operation_report_digests": [],
                        "redaction": {"status": "passed", "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()},
                        "retention_class": "cleanup_report_only",
                    }):
                        ledger.seal_terminal(root, task="TASK-4")
                    original_builder = ledger._retention_receipt

                    def historical(*args, **kwargs):
                        return original_builder(*args, **{**kwargs, "version": 1})

                    with mock.patch.object(ledger, "_retention_receipt", side_effect=historical):
                        old = ledger.retain_evidence(root, task="TASK-4", _native_lookup=lookup)
                    # A caller-only old digest is not a historical operation.
                    with self.assertRaisesRegex(ValueError, "confirmation"):
                        ledger.retain_evidence(root, task="TASK-4", confirm=old["confirm"], _native_lookup=lookup)
                    self.assertFalse((source / ".kent/retained-evidence").exists())

                    def crash(observed):
                        if observed == phase:
                            raise RuntimeError("historical publication crash")

                    with mock.patch.object(ledger, "_retention_receipt", side_effect=historical):
                        with self.assertRaisesRegex(RuntimeError, "historical"):
                            ledger.retain_evidence(root, task="TASK-4", confirm=old["confirm"],
                                                   _native_lookup=lookup, _phase_hook=crash)
                    selected = ledger.retain_evidence(root, task="TASK-4", _native_lookup=lookup)
                    self.assertEqual(selected["receipt"], old["receipt"])
                    self.assertEqual(selected["confirm"], old["confirm"])
                    result = ledger.retain_evidence(root, task="TASK-4", confirm=old["confirm"], _native_lookup=lookup)
                    self.assertEqual(result["receipt"]["schema"], "evidence-retention-v1")
                    self.assertEqual(set(p.name for p in (source / ".kent/retained-evidence/TASK-4").iterdir()), {old["confirm"]})
                    marker = runtime.validate_terminal_chain(
                        ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4"), task_short_id="TASK-4")
                    retained = ledger.find_retained_evidence(root, task="TASK-4", marker=marker, _native_lookup=lookup)
                    task_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        ledger.verify_retained_sources(root, task_fd, retained)
                    finally:
                        os.close(task_fd)
                    self.assertEqual(retained["receipt"], old["receipt"])
                    report.write_bytes(b"changed source")
                    with self.assertRaises(ValueError):
                        ledger.retain_evidence(root, task="TASK-4", confirm=old["confirm"], _native_lookup=lookup)

    def test_historical_v1_recovery_requires_durable_preparation(self):
        import workflowkit.runtime as runtime
        root, scripts, directory, original = self.recovered_fixture()
        for name in runtime.EVIDENCE_RECOVERY_FILES:
            (directory / name).unlink()
        ledger = load_template_module(scripts / "workflow-evidence-ledger", "historical_recovery")
        owner = {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "22222222-2222-4222-8222-222222222222",
        }
        request = runtime.build_evidence_recovery_request(
            original, task_short_id="TASK-4", owner={**owner, "run_id": "historical-run"})
        manifest, continuation = ledger._recovery_documents(runtime, original, request)
        identity = self.native_fixture(root, self.create_repository().resolve(), owner)
        lookup = lambda *_: identity
        environment = {"KENT_" + key.upper(): value for key, value in owner.items()}
        with mock.patch.dict(os.environ, {**environment, "KENT_RUN_ID": "stale-ignored"}):
            with self.assertRaisesRegex(ValueError, "fresh recovery"):
                ledger.recover_evidence(root, task="TASK-4", request=request,
                                        confirm=manifest["request_sha256"], _native_lookup=lookup)
            self.assertFalse((directory / "evidence-continuation.jsonl").exists())
            tail = directory / "evidence-continuation.jsonl"
            tail.write_bytes(continuation)
            tail.chmod(0o600)
            result = ledger.recover_evidence(root, task="TASK-4", request=request,
                                            confirm=manifest["request_sha256"], _native_lookup=lookup)
            self.assertTrue(result["reused"])
            self.assertEqual((directory / "evidence-recovery.json").read_bytes(), runtime.canonical_bytes(manifest))
            payload = {
                "node_key": "cleanup", "evidence_type": "cleanup", "summary": "new genuine event",
                "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
            }
            event, reused = ledger.append_entry(
                root, directory / "evidence-ledger.jsonl", task="TASK-4", _payload=payload)
            self.assertFalse(reused)  # The same owner's recovery boundary is not an ordinary event.
            self.assertEqual(event["schema_version"], 2)
            self.assertEqual(event["sequence"], 3)
            before = tail.read_bytes()
            self.assertTrue(ledger.recover_evidence(
                root, task="TASK-4", request=request, confirm=manifest["request_sha256"],
                _native_lookup=lookup)["reused"])
            self.assertEqual(tail.read_bytes(), before)
            self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "already recovered"):
                runtime.build_evidence_recovery_request(
                    original.splitlines(keepends=True)[0] + before + b"broken",
                    task_short_id="TASK-4", owner=owner)

    def test_legacy_session_step_match_and_ambiguity_preserve_bytes(self):
        root = self.create_repository().resolve()
        (root / "AGENTS.md").write_text("rules\n")
        owner = {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "22222222-2222-4222-8222-222222222222",
        }
        ledger = load_template_module(EVIDENCE, "legacy_dedup_identity")
        payload = {
            "node_key": "implement", "evidence_type": "implementation", "summary": "fixture",
            "context": {"manifest_path": "AGENTS.md", "files_read": []},
        }
        path = root / ".kent/runtime/TASK-4/evidence-ledger.jsonl"
        with mock.patch.dict(os.environ, {"KENT_" + key.upper(): value for key, value in owner.items()}):
            first, _ = ledger.append_entry(root, path, task="TASK-4", _payload=payload)
            first.update(schema_version=1, run_id="historical-run")
            first["event_hash"] = ledger.entry_hash({k: v for k, v in first.items() if k != "event_hash"})
            path.write_bytes(ledger.canonical_bytes(first) + b"\n")
            before = path.read_bytes()
            self.assertEqual(ledger.append_entry(root, path, task="TASK-4", _payload=payload), (first, True))
            self.assertEqual(path.read_bytes(), before)
            second = {**first, "sequence": 2, "previous_hash": first["event_hash"], "run_id": "other-old-run"}
            second["event_hash"] = ledger.entry_hash({k: v for k, v in second.items() if k != "event_hash"})
            path.write_bytes(before + ledger.canonical_bytes(second) + b"\n")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "ambiguous historical"):
                ledger.append_entry(root, path, task="TASK-4", _payload=payload)
            self.assertEqual(path.read_bytes(), before)

    def test_v2_append_ignores_run_and_deduplicates_session_step(self):
        root = self.create_repository().resolve()
        (root / "AGENTS.md").write_text("rules\n")
        payload = {
            "node_key": "implement", "evidence_type": "implementation",
            "summary": "Identity fixture", "context": {"manifest_path": "AGENTS.md", "files_read": []},
        }
        ledger = load_template_module(EVIDENCE, "v2_append_identity")
        session = "11111111-1111-4111-8111-111111111111"
        step = "22222222-2222-4222-8222-222222222222"
        path = root / ".kent/runtime/TASK-4/evidence-ledger.jsonl"
        with mock.patch.dict(os.environ, {"KENT_SESSION_ID": session, "KENT_STEP_ID": step}):
            os.environ.pop("KENT_RUN_ID", None)
            first, reused = ledger.append_entry(root, path, task="TASK-4", _payload=payload)
            self.assertFalse(reused)
            self.assertEqual(first["schema_version"], 2)
            self.assertNotIn("run_id", first)
            os.environ["KENT_RUN_ID"] = "stale-run"
            self.assertEqual(ledger.append_entry(root, path, task="TASK-4", _payload=payload), (first, True))
            os.environ["KENT_STEP_ID"] = "33333333-3333-4333-8333-333333333333"
            second, reused = ledger.append_entry(root, path, task="TASK-4", _payload=payload)
            self.assertFalse(reused)
            self.assertEqual(second["sequence"], 2)
            os.environ["KENT_STEP_ID"] = "unknown"
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                ledger.append_entry(root, path, task="TASK-4", _payload=payload)
            self.assertEqual(path.read_bytes(), before)

    def native_fixture(self, root, source, owner):
        return {
            "task_short_id": "TASK-4", "workspace": str(root.resolve()),
            "source_workspace": str(source.resolve()), "owner": owner,
            "source_native_identity": {
                "project_id": "native-project", "workspace_id": "native-primary",
                "task_id": "native-task",
            },
        }

    def test_recovery_crash_exact_retry_and_native_proof_gate(self):
        root, scripts, directory, original = self.recovered_fixture()
        (directory / "evidence-recovery.json").unlink()
        (directory / "evidence-continuation.jsonl").unlink()
        source = self.create_repository().resolve()
        ledger = load_template_module(scripts / "workflow-evidence-ledger", "recovery_operation")
        owner = {"session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "s")),
                 "step_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "t"))}
        identity = self.native_fixture(root, source, owner)
        environment = {"KENT_" + key.upper(): value for key, value in owner.items()}
        with mock.patch.dict(os.environ, environment):
            with mock.patch.dict(os.environ, {"KENT_STEP_ID": "unknown"}):
                with self.assertRaisesRegex(ValueError, "canonical Kent"):
                    ledger.recovery_preview(root, task="TASK-4")
            preview = ledger.recovery_preview(root, task="TASK-4", _native_lookup=lambda *_: identity)
            self.assertEqual(set(path.name for path in directory.iterdir()), {"evidence-ledger.jsonl"})

            def crash(phase):
                if phase == "after_continuation_fsync":
                    raise RuntimeError("simulated process death")

            with self.assertRaisesRegex(RuntimeError, "simulated"):
                ledger.recover_evidence(
                    root, task="TASK-4", request=preview["request"],
                    confirm=preview["request_sha256"], _native_lookup=lambda *_: identity,
                    _phase_hook=crash,
                )
            with self.assertRaisesRegex(ValueError, "incomplete"):
                ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4")
        retry_owner = {**owner, "step_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "retry-step"))}
        identity = self.native_fixture(root, source, retry_owner)
        with mock.patch.dict(os.environ, {"KENT_" + key.upper(): value for key, value in retry_owner.items()}):
            result = ledger.recover_evidence(
                root, task="TASK-4", request=preview["request"],
                confirm=preview["request_sha256"], _native_lookup=lambda *_: identity,
            )
            self.assertTrue(result["reused"])
            self.assertEqual(result["retry_owner"], retry_owner)
            self.assertEqual(ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4")[-1]["step_id"], owner["step_id"])
            self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)
            self.assertEqual(
                result,
                ledger.recover_evidence(
                    root, task="TASK-4", request=preview["request"],
                    confirm=preview["request_sha256"], _native_lookup=lambda *_: identity,
                ),
            )

    def test_healthy_and_recovered_retention_crash_retry_and_readback(self):
        import workflowkit.runtime as runtime
        from workflowkit.delivery import capture_producer

        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                root, scripts, directory, original = self.recovered_fixture()
                if not recovered:
                    (directory / "evidence-ledger.jsonl").write_bytes(original.splitlines(keepends=True)[0])
                    for name in runtime.EVIDENCE_RECOVERY_FILES:
                        (directory / name).unlink()
                ledger = load_template_module(scripts / "workflow-evidence-ledger", "retention_operation")
                source = self.create_repository().resolve()
                owner = {"session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "s")),
                         "step_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "t"))}
                identity = self.native_fixture(root, source, owner)
                shutil.copyfile(CHECKPOINT, scripts / "workflow-checkpoint")
                home = self.create_repository().resolve()
                tools = home / ".kent/bin"
                tools.mkdir(parents=True)
                (tools / "kent-prepare-github-ci").symlink_to(REPO_ROOT / "scripts/prepare-github-ci")
                packet_identity = {
                    "task_id": "native-task", "task_short_id": "TASK-4",
                    "project_id": "native-project", "workspace_path": str(root), **owner,
                }
                packet_locator, _ = capture_producer(
                    root, identity=packet_identity, producer_kind="retention-fixture",
                    inputs={}, result={"commentary": "Original exact report"})
                report = root / "redaction.json"
                report.write_bytes(b'{"status":"passed"}')
                report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
                payload = {
                    "node_key": "cleanup", "evidence_type": "cleanup",
                    "summary": "real final event\nKENT_PACKET_SOURCE_V1 " + packet_locator,
                    "artifacts": ["redaction.json"],
                    "checks": [], "decisions": [],
                    "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
                }
                seal_request = {
                    "schema": runtime.TERMINAL_SEAL_REQUEST_SCHEMA,
                    "operation_report_digests": [],
                    "redaction": {"status": "passed", "report_sha256": report_hash},
                    "retention_class": "cleanup_report_only",
                }
                with mock.patch.dict(os.environ, {
                        **{"KENT_" + key.upper(): value for key, value in owner.items()},
                        "HOME": str(home)}):
                    with mock.patch.object(ledger, "read_json_input", return_value=payload):
                        ledger.append_entry(root, directory / "evidence-ledger.jsonl", task="TASK-4")
                    with mock.patch.object(ledger, "read_json_input", return_value=seal_request):
                        ledger.seal_terminal(root, task="TASK-4")
                    lookup = lambda *_: identity
                    with self.assertRaises(FileNotFoundError):
                        ledger.retain_evidence(root, task="TASK-4", _native_lookup=lookup)
                    plan = root / ".todo/task/plan.md"
                    plan.parent.mkdir(parents=True)
                    plan.write_text("retained historical reference\n")
                    preview = ledger.retain_evidence(root, task="TASK-4", _native_lookup=lookup)
                    self.assertFalse((source / ".kent/retained-evidence").exists())
                    receipt = preview["receipt"]
                    self.assertEqual(receipt["recovery_request_sha256"] is not None, recovered)
                    paths = {item["logical_path"] for item in receipt["files"]}
                    self.assertIn(packet_locator, paths)
                    self.assertEqual(
                        f".kent/runtime/TASK-4/evidence-recovery.json" in paths, recovered,
                    )

                    def crash(phase):
                        if phase == "before_retention_publish":
                            raise RuntimeError("retention publication interrupted")

                    with self.assertRaisesRegex(RuntimeError, "interrupted"):
                        ledger.retain_evidence(
                            root, task="TASK-4", confirm=preview["confirm"],
                            _native_lookup=lookup, _phase_hook=crash,
                        )
                    result = ledger.retain_evidence(
                        root, task="TASK-4", confirm=preview["confirm"],
                        _native_lookup=lookup,
                    )
                    self.assertTrue(result["published"])
                    bundle = Path(result["bundle_path"])
                    self.assertEqual(bundle.stat().st_mode & 0o777, 0o700)
                    for path in bundle.iterdir():
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    retry = ledger.retain_evidence(
                        root, task="TASK-4", confirm=preview["confirm"],
                        _native_lookup=lookup,
                    )
                    self.assertTrue(retry["reused"])
                    self.assertEqual(result["receipt"], retry["receipt"])
                    self.assertEqual(ledger.verify_retention_bundle(identity, "TASK-4", receipt, runtime), str(bundle))
                    marker = runtime.validate_terminal_chain(
                        ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4"),
                        task_short_id="TASK-4",
                    )
                    packet_directory = root / packet_locator
                    packet_directory.parent.rename(packet_directory.parent.with_name("retired-packets"))
                    found = ledger.find_retained_evidence(root, task="TASK-4", marker=marker, _native_lookup=lookup)
                    self.assertEqual(found["receipt"], receipt)
                    packet_directory.parent.with_name("retired-packets").rename(packet_directory.parent)
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        ledger.verify_retained_sources(root, descriptor, found)
                    finally:
                        os.close(descriptor)
                    (bundle / "000").write_bytes(b"tampered")
                    with self.assertRaisesRegex(ValueError, "digest|limit"):
                        ledger.retain_evidence(
                            root, task="TASK-4", confirm=preview["confirm"],
                            _native_lookup=lookup,
                        )
                    self.assertTrue(directory.exists())
                    if recovered:
                        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)

    def test_retained_healthy_and_recovered_janitor_retirement_and_crash_resume(self):
        import workflowkit.runtime as runtime

        for recovered in (False, True):
            for managed in (False, True):
                with self.subTest(recovered=recovered, managed=managed):
                    root, scripts, directory, original = self.recovered_fixture()
                    if not recovered:
                        (directory / "evidence-ledger.jsonl").write_bytes(original.splitlines(keepends=True)[0])
                        for name in runtime.EVIDENCE_RECOVERY_FILES:
                            (directory / name).unlink()
                        (root / ".kent/workflow-profile.toml").write_text('[policies]\ndelivery_reliability = "v1"\n')
                    source = self.create_repository().resolve()
                    owner, detail, native_file, environment = self.install_native_fixture(root, source)
                    plan = root / ".todo/task/plan.md"
                    plan.parent.mkdir(parents=True)
                    plan.write_text("retained plan\n")
                    report = root / "redaction.json"
                    report.write_bytes(b'{"status":"passed"}')
                    ledger = load_template_module(scripts / "workflow-evidence-ledger", "janitor_retained_ledger")
                    janitor = load_template_module(scripts / "workflow-task-janitor", "janitor_retained_operation")
                    payload = {
                        "node_key": "cleanup", "evidence_type": "cleanup", "summary": "real final event",
                        "artifacts": ["redaction.json"], "checks": [], "decisions": [],
                        "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
                    }
                    seal_request = {
                        "schema": runtime.TERMINAL_SEAL_REQUEST_SCHEMA,
                        "operation_report_digests": [],
                        "redaction": {"status": "passed", "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()},
                        "retention_class": "cleanup_report_only",
                    }
                    with mock.patch.dict(os.environ, environment, clear=True):
                        with mock.patch.object(ledger, "read_json_input", return_value=payload):
                            ledger.append_entry(root, directory / "evidence-ledger.jsonl", task="TASK-4")
                        with mock.patch.object(ledger, "read_json_input", return_value=seal_request):
                            sealed = ledger.seal_terminal(root, task="TASK-4")
                        operation = janitor._prepare_v2_managed_runtime_state if managed else janitor._remove_v2_runtime_state
                        absent = operation(root, "TASK-4", cleanup_report=sealed["terminal_marker"])
                        self.assertFalse(absent[0], absent)
                        self.assertTrue(directory.exists(), "missing retention must block before rename")
                        preview = ledger.retain_evidence(root, task="TASK-4")
                        retained = ledger.retain_evidence(root, task="TASK-4", confirm=preview["confirm"])
                        archive = Path(retained["bundle_path"])
                        before = {path.name: path.read_bytes() for path in archive.iterdir()}
                        failed = False
                        def crash(phase):
                            nonlocal failed
                            boundary = "after_tombstone_rename" if managed else "after_opaque_unlink_fsync"
                            if phase == boundary and not failed:
                                failed = True
                                raise OSError("retirement interrupted")
                        first = operation(root, "TASK-4", cleanup_report=sealed["terminal_marker"], _phase_hook=crash)
                        if recovered or managed:
                            self.assertFalse(first[0], first)
                        final = operation(root, "TASK-4", cleanup_report=sealed["terminal_marker"])
                        self.assertTrue(final[0], final)
                        self.assertEqual({path.name: path.read_bytes() for path in archive.iterdir()}, before)
                        marker = runtime.validate_cleanup_report(sealed["terminal_marker"])
                        tombstone = directory.parent / (".evidence-cleanup-" + runtime.canonical_sha256(marker))
                        if managed:
                            self.assertTrue(tombstone.is_dir())
                            if recovered:
                                self.assertEqual((tombstone / "evidence-ledger.jsonl").read_bytes(), original)
                        else:
                            self.assertFalse(tombstone.exists())
                        self.assertFalse(directory.exists())

    def install_native_fixture(self, root, source):
        owner = {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "33333333-3333-4333-8333-333333333333",
        }
        detail = {
            "summary": {"id": "native-task", "project_id": "native-project", "short_id": "TASK-4"},
            "project": {},
            "source_workspace": {
                "root_path": str(source.resolve()), "workspace_id": "native-primary", "is_primary": True,
            },
            "worktree_path": str(root.resolve()),
            "current_nodes": [{"node_id": "cleanup", "session_id": owner["session_id"]}],
            # A retained-current Session need not be in the optional live list.
            "retained_session_count": 1000,
        }
        native_file = root / "native-fixture.json"
        native_file.write_text(json.dumps(detail))
        executable = root / "fake-native/kent"
        executable.parent.mkdir()
        executable.write_text(
            "#!" + sys.executable + "\n"
            "import json,os,sys\n"
            "a=sys.argv[1:]\n"
            "if a[0]=='project': print('native-project')\n"
            "elif a[:2]==['task','show']: print(open(os.environ['NATIVE_FIXTURE']).read())\n"
            "elif a[:3]==['workflow','graph','inspect']: print(open(os.environ['NATIVE_GRAPH']).read())\n"
            "else: raise SystemExit('irrelevant native inventory must not be queried')\n"
        )
        executable.chmod(0o755)
        environment = {**os.environ, **{"KENT_" + key.upper(): value for key, value in owner.items()}}
        environment.pop("KENT_TASK_ID", None)
        environment.update(PATH=str(executable.parent) + os.pathsep + os.environ["PATH"], NATIVE_FIXTURE=str(native_file))
        return owner, detail, native_file, environment

    def test_public_identity_needs_only_inherited_session_and_native_binding(self):
        root, scripts, directory, original = self.recovered_fixture()
        source = self.create_repository().resolve()
        owner, detail, native_file, environment = self.install_native_fixture(root, source)
        for key in ("KENT_STEP_ID", "KENT_RUN_ID", "KENT_TASK_ID"):
            environment.pop(key, None)
        # Neither ignored runtime state nor R2/retention admission is needed.
        (root / ".gitignore").write_text("")
        (root / ".kent/workflow-profile.toml").write_text("not valid TOML")
        command = [str(scripts / "workflow-evidence-ledger"), "identity",
                   "--task", "TASK-4", "--workspace", str(root)]

        def invoke(env=environment):
            return subprocess.run(command, capture_output=True, text=True, env=env)

        result = invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "task_id": "native-task", "task_short_id": "TASK-4",
            "project_id": "native-project", "workspace_path": str(root),
            "session_id": owner["session_id"],
        })
        for value in ("", "123", "made-up", "22222222-2222-4222-8222-222222222222"):
            result = invoke({**environment, "KENT_SESSION_ID": value})
            self.assertEqual(result.returncode, 1, result.stdout)
        for changed in (
            {**detail, "current_nodes": [], "live_sessions": []},
            {**detail, "worktree_path": str(source)},
            {**detail, "summary": {**detail["summary"], "project_id": "foreign"}},
        ):
            native_file.write_text(json.dumps(changed))
            self.assertEqual(invoke().returncode, 1)
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)

    def test_real_script_janitor_retained_reader_without_agent_environment(self):
        """Synthetic native transport; identity and actual Janitor are not stubbed."""
        import workflowkit.runtime as runtime
        from workflowkit.delivery import capture_producer

        for recovered, scenario in (
            (False, "healthy"), (True, "recovered"), (True, "archive-resume"),
            (True, "foreign-task"), (True, "foreign-node"), (True, "foreign-branch"),
            (True, "missing-current"), (True, "ambiguous-current"), (True, "foreign-path"),
            (True, "foreign-workspace"), (True, "foreign-source"),
            (True, "bundle-tamper"), (True, "bundle-missing"),
        ):
            with self.subTest(scenario=scenario):
                root, scripts, directory, original = self.recovered_fixture()
                if not recovered:
                    (directory / "evidence-ledger.jsonl").write_bytes(original.splitlines(keepends=True)[0])
                    for name in runtime.EVIDENCE_RECOVERY_FILES:
                        (directory / name).unlink()
                    (root / ".kent/workflow-profile.toml").write_text('[policies]\ndelivery_reliability = "v1"\n')
                source = self.create_repository().resolve()
                owner, detail, native_file, environment = self.install_native_fixture(root, source)
                plan = root / ".todo/task/plan.md"
                plan.parent.mkdir(parents=True)
                plan.write_text("retained plan\n")
                report = root / "redaction.json"
                report.write_bytes(b'{"status":"passed"}')
                ledger = load_template_module(scripts / "workflow-evidence-ledger", "script_reader_ledger")
                shutil.copyfile(CHECKPOINT, scripts / "workflow-checkpoint")
                home = self.create_repository().resolve()
                tools = home / ".kent/bin"
                tools.mkdir(parents=True)
                (tools / "kent-prepare-github-ci").symlink_to(REPO_ROOT / "scripts/prepare-github-ci")
                environment["HOME"] = str(home)
                packet_locator, _ = capture_producer(
                    root, identity={
                        "task_id": "native-task", "task_short_id": "TASK-4",
                        "project_id": "native-project", "workspace_path": str(root), **owner,
                    }, producer_kind="script-reader-fixture", inputs={}, result={"commentary": "Original exact report"},
                )
                with mock.patch.dict(os.environ, environment, clear=True):
                    ledger.append_entry(root, directory / "evidence-ledger.jsonl", task="TASK-4", _payload={
                        "node_key": "cleanup", "evidence_type": "cleanup",
                        "summary": "final\nKENT_PACKET_SOURCE_V1 " + packet_locator,
                        "artifacts": ["redaction.json"],
                        "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
                    })
                    with mock.patch.object(ledger, "read_json_input", return_value={
                        "schema": runtime.TERMINAL_SEAL_REQUEST_SCHEMA,
                        "operation_report_digests": [],
                        "redaction": {"status": "passed", "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()},
                        "retention_class": "cleanup_report_only",
                    }):
                        sealed = ledger.seal_terminal(root, task="TASK-4")
                    preview = ledger.retain_evidence(root, task="TASK-4")
                    retained = ledger.retain_evidence(root, task="TASK-4", confirm=preview["confirm"])
                archive = Path(retained["bundle_path"])
                native = {"task_id": "native-task", "node_id": "janitor", "transition_branch_key": "cleanup"}
                workflow = "44444444-4444-4444-8444-444444444444"
                detail["summary"]["workflow_id"] = workflow
                detail["current_nodes"] = []
                detail["current_scripts"] = [{
                    "current_node": {"node_id": "janitor", "transition_branch_key": "cleanup"},
                    "path": str(scripts / "workflow-task-janitor"),
                }]
                graph = root / "graph.json"
                graph.write_text(json.dumps({"workflow_id": workflow, "graph": {"nodes": [{
                    "id": "janitor", "kind": "script", "script_path": str(scripts / "workflow-task-janitor"),
                }]}}))
                environment["NATIVE_GRAPH"] = str(graph)
                for key in ("KENT_SESSION_ID", "KENT_STEP_ID", "KENT_RUN_ID", "KENT_TASK_ID"):
                    environment.pop(key, None)
                if scenario == "foreign-task":
                    native["task_id"] = "foreign"
                elif scenario == "foreign-node":
                    native["node_id"] = "foreign"
                elif scenario == "foreign-branch":
                    native["transition_branch_key"] = "foreign"
                elif scenario == "missing-current":
                    detail["current_scripts"] = []
                elif scenario == "ambiguous-current":
                    detail["current_scripts"] *= 2
                elif scenario == "foreign-path":
                    detail["current_scripts"][0]["path"] = "/foreign/script"
                elif scenario == "foreign-workspace":
                    detail["worktree_path"] = str(source)
                elif scenario == "foreign-source":
                    detail["source_workspace"]["workspace_id"] = "foreign"
                elif scenario == "bundle-tamper":
                    (archive / "000").write_bytes(b"tampered")
                elif scenario == "bundle-missing":
                    (archive / "000").unlink()
                elif scenario == "archive-resume":
                    marker = runtime.validate_cleanup_report(sealed["terminal_marker"])
                    tombstone = directory.parent / (".evidence-cleanup-" + runtime.canonical_sha256(marker))
                    directory.rename(tombstone)
                    (tombstone / "evidence-ledger.jsonl").unlink()
                    _, sentinel = ledger.runtime_state_names("TASK-4")
                    (directory.parent / sentinel).touch(mode=0o600)
                native_file.write_text(json.dumps(detail))
                before = {path.name: path.read_bytes() for path in archive.iterdir()}
                # Reproduce the old Agent-only admission defect without
                # substituting the identity validator in the actual invocation.
                with mock.patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "UUID"):
                        ledger._verified_context(root, "TASK-4")
                    for writer in (ledger.recovery_preview, ledger.retain_evidence):
                        with self.assertRaisesRegex(ValueError, "UUID"):
                            writer(root, task="TASK-4")
                payload = {
                    "_kent": native, "workspace_path": str(root),
                    "branch_name": self.run_git(root, "branch", "--show-current").stdout.strip(),
                    "pr_url": "", "cleanup_mode": "no_pr",
                    "pr_base_branch": "not-applicable",
                    "cleanup_session_id": owner["session_id"],
                    "cleanup_report": sealed["terminal_marker"], "task_short_id": "TASK-4",
                }
                result = subprocess.run(
                    [str(scripts / "workflow-task-janitor")], input=json.dumps(payload),
                    capture_output=True, text=True, env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                expected = "task_janitor_done" if scenario in {"healthy", "recovered", "archive-resume"} else "task_janitor_blocked"
                self.assertEqual(output["transition"], expected, output)
                self.assertEqual({path.name: path.read_bytes() for path in archive.iterdir()}, before)
                if expected == "task_janitor_blocked":
                    self.assertTrue(directory.exists())
                else:
                    self.assertFalse(directory.exists())

    def test_native_proof_port_current_retained_script_foreign_and_missing_fields(self):
        root, scripts, directory, original = self.recovered_fixture()
        source = self.create_repository().resolve()
        owner, detail, native_file, environment = self.install_native_fixture(root, source)
        ledger = load_template_module(scripts / "workflow-evidence-ledger", "native_owner_port")
        with mock.patch.dict(os.environ, environment, clear=True):
            result = ledger._native_context(root, "TASK-4")
            self.assertEqual(result["owner"], owner)
            self.assertEqual(result["source_workspace"], str(source))
            self.assertEqual(ledger.native_current_identity(root, "TASK-4"), {
                "task_id": "native-task", "task_short_id": "TASK-4",
                "project_id": "native-project", "workspace_path": str(root), **owner,
            })
            # A fresh active Session can be proven by live_sessions even when
            # current_nodes is not included in this native projection.
            fresh = dict(detail)
            fresh.pop("current_nodes")
            fresh["live_sessions"] = [{"session_id": owner["session_id"]}]
            native_file.write_text(json.dumps(fresh))
            self.assertEqual(ledger._native_context(root, "TASK-4")["owner"], owner)
            for changed in (
                {**detail, "current_nodes": [], "live_sessions": []},
                {**detail, "current_nodes": [{"node_id": "cleanup"}], "current_scripts": [{"path": "script"}]},
                {**detail, "current_nodes": [{"node_id": "cleanup", "session_id": "foreign"}]},
                {**detail, "worktree_path": str(source)},
                {**detail, "summary": {**detail["summary"], "short_id": "OTHER-1"}},
                {**detail, "source_workspace": {**detail["source_workspace"], "is_primary": False}},
            ):
                with self.subTest(changed=changed):
                    native_file.write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):
                        ledger._native_context(root, "TASK-4")
            native_file.write_text(json.dumps(detail))
            for key in ("KENT_SESSION_ID", "KENT_STEP_ID"):
                with mock.patch.dict(os.environ, {key: ""}), self.assertRaisesRegex(ValueError, "UUID"):
                    ledger._native_context(root, "TASK-4")
                with mock.patch.dict(os.environ, {key: "made-up"}), self.assertRaisesRegex(ValueError, "UUID"):
                    ledger._native_context(root, "TASK-4")
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)

    def test_public_recovery_native_cli_staged_crash_and_fresh_run_retry(self):
        import workflowkit.runtime as runtime

        root, scripts, directory, original = self.recovered_fixture()
        for name in runtime.EVIDENCE_RECOVERY_FILES:
            (directory / name).unlink()
        source = self.create_repository().resolve()
        owner, detail, native_file, environment = self.install_native_fixture(root, source)
        command = [str(scripts / "workflow-evidence-ledger")]
        arguments = ["--task", "TASK-4", "--workspace", str(root)]
        preview = subprocess.run(
            [*command, "recovery-preview", *arguments], capture_output=True, text=True, env=environment,
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        preview = json.loads(preview.stdout)
        self.assertEqual(preview["request"]["owner_provenance"], runtime.OWNER_PROVENANCE)
        duplicate = json.dumps(preview["request"])[:-1] + ',"owner":' + json.dumps(owner) + "}"
        invalid = subprocess.run(
            [*command, "recover", *arguments, "--confirm", preview["request_sha256"]],
            input=duplicate, capture_output=True, text=True, env=environment,
        )
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("duplicate JSON", invalid.stderr)
        self.assertEqual(set(path.name for path in directory.iterdir()), {"evidence-ledger.jsonl"})
        ledger = load_template_module(scripts / "workflow-evidence-ledger", "staged_recovery")
        with mock.patch.dict(os.environ, environment, clear=True):
            def crash(phase):
                if phase == "after_manifest_staging_fsync":
                    raise RuntimeError("staged crash")
            with self.assertRaisesRegex(RuntimeError, "staged crash"):
                ledger.recover_evidence(
                    root, task="TASK-4", request=preview["request"],
                    confirm=preview["request_sha256"], _phase_hook=crash,
                )
        temporary = root / "build/kent-workflow/TASK-4" / ("recovery-" + preview["request_sha256"] + ".tmp")
        self.assertTrue(temporary.is_file())
        environment["KENT_RUN_ID"] = "44444444-4444-4444-8444-444444444444"
        environment["KENT_STEP_ID"] = "55555555-5555-4555-8555-555555555555"
        recovered = subprocess.run(
            [*command, "recover", *arguments, "--confirm", preview["request_sha256"]],
            input=json.dumps(preview["request"]), capture_output=True, text=True, env=environment,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["retry_owner"]["step_id"], environment["KENT_STEP_ID"])
        self.assertFalse(temporary.exists())
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)
        self.assertEqual(set(path.name for path in directory.iterdir()), {
            "evidence-ledger.jsonl", *runtime.EVIDENCE_RECOVERY_FILES,
        })

    def recovered_fixture(self):
        import workflowkit.runtime as runtime
        root = self.create_repository()
        scripts = self.install_v2_runtime_commands(root)
        (root / ".gitignore").write_text("/.kent/runtime/\n/build/kent-workflow/\n")
        context = root / ".kent/context"
        context.mkdir()
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "context")
        self.append(root, summary="seed", files_read=[])
        directory = root / ".kent/runtime/TASK-4"
        base = directory / "evidence-ledger.jsonl"
        original = base.read_bytes() + b'{"artifacts":["/untrusted/secret"],"broken":'
        base.write_bytes(original)
        owner = {"session_id": "test-session", "run_id": "recovery-run", "step_id": "recovery-step"}
        request = runtime.build_evidence_recovery_request(
            original, task_short_id="TASK-4", owner=owner,
        )
        digest = runtime.canonical_sha256(request)
        prefix = json.loads(original.splitlines()[0])
        boundary = {
            "schema_version": 1, "sequence": 2, "task_short_id": "TASK-4",
            "previous_hash": prefix["event_hash"], **owner,
            "record_kind": "evidence_recovery_boundary_v1",
            "recovery_request_sha256": digest,
        }
        boundary["event_hash"] = runtime._event_hash(boundary)
        manifest = {
            "schema": "evidence-recovery-v1", "request": request,
            "request_sha256": digest, "boundary_event_hash": boundary["event_hash"],
        }
        for name, raw in (
            ("evidence-recovery.json", runtime.canonical_bytes(manifest)),
            ("evidence-continuation.jsonl", runtime.canonical_bytes(boundary) + b"\n"),
        ):
            path = directory / name
            path.write_bytes(raw)
            path.chmod(0o600)
        return root.resolve(), scripts, directory, original

    def test_inspection_uses_recovered_view_without_opaque_suffix_disclosure(self):
        root, scripts, directory, original = self.recovered_fixture()
        target = scripts / "workflow-plan-contract"
        shutil.copyfile(PLAN_CONTRACT, target)
        module = load_template_module(target, "recovered_context_inspection")
        result = module.inspect_workflow_context({
            "schema": "workflow-context-inspection-request-v1",
            "workspace_path": str(root), "task_short_id": "TASK-4",
            "plan_path": "not-applicable", "evidence_refs": [],
        })
        serialized = json.dumps(result)
        self.assertNotIn("/untrusted/secret", serialized)
        self.assertEqual(result["evidence_status"], "recovered_with_gap")
        self.assertFalse(result["discovery_complete"])
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)
        self.assertFalse((directory / "plan-contract.json").exists())

    def test_recovery_boundary_versions_summary_and_validate_preserve_history(self):
        import workflowkit.runtime as runtime

        for version in (1, 2):
            with self.subTest(version=version):
                root, scripts, directory, original = self.recovered_fixture()
                ledger = load_template_module(
                    scripts / "workflow-evidence-ledger", f"recovery_summary_{version}",
                )
                if version == 2:
                    owner = {
                        "session_id": EVIDENCE_FIXTURE_OWNER["KENT_SESSION_ID"],
                        "step_id": EVIDENCE_FIXTURE_OWNER["KENT_STEP_ID"],
                    }
                    request = runtime.build_evidence_recovery_request(
                        original, task_short_id="TASK-4", owner=owner,
                    )
                    manifest, continuation = ledger._recovery_documents(
                        runtime, original, request,
                    )
                    (directory / "evidence-recovery.json").write_bytes(
                        runtime.canonical_bytes(manifest),
                    )
                    (directory / "evidence-continuation.jsonl").write_bytes(continuation)
                before = {path.name: path.read_bytes() for path in directory.iterdir()}
                records = ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4")
                self.assertEqual(records[-1]["record_kind"], f"evidence_recovery_boundary_v{version}")
                expected = ledger.summarize(records[:1])
                for command in ("summary", "validate"):
                    result = subprocess.run(
                        [sys.executable, str(scripts / "workflow-evidence-ledger"),
                         command, "--task", "TASK-4", "--workspace", str(root)],
                        cwd=root, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = json.loads(result.stdout)
                    self.assertEqual(output["status"], "recovered_with_gap")
                    self.assertEqual(output["entry_count"], 2)
                    if command == "summary":
                        for key in expected.keys() - {"entry_count", "last_event_hash"}:
                            self.assertEqual(output[key], expected[key], key)
                        self.assertEqual(output["last_event_hash"], records[-1]["event_hash"])
                        self.assertNotIn("/untrusted/secret", result.stdout)
                    else:
                        self.assertTrue(output["valid"])
                self.assertEqual(
                    {path.name: path.read_bytes() for path in directory.iterdir()}, before,
                )
                self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
    def test_recovered_read_append_dedup_and_seal_use_continuation(self):
        root, scripts, directory, original = self.recovered_fixture()
        ledger = load_template_module(scripts / "workflow-evidence-ledger", "recovered_ledger")
        records = ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4")
        self.assertEqual(len(records), 2)
        payload = {
            "node_key": "cleanup", "evidence_type": "cleanup", "summary": "real final event",
            "artifacts": [], "checks": [], "decisions": [],
            "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
        }
        seal_request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": "a" * 64},
            "retention_class": "cleanup_report_only",
        }
        with mock.patch.object(ledger, "read_json_input", return_value=seal_request):
            with self.assertRaisesRegex(ValueError, "ordinary"):
                ledger.seal_terminal(root, task="TASK-4")
        with mock.patch.dict(os.environ, {
            "KENT_SESSION_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "test-session")),
            "KENT_RUN_ID": "stale-ignored-run",
            "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "real-final-step")),
        }), mock.patch.object(ledger, "read_json_input", return_value=payload):
            event, duplicate = ledger.append_entry(root, directory / "evidence-ledger.jsonl", task="TASK-4")
            self.assertFalse(duplicate, "boundary must not suppress the genuine same-Run event")
            self.assertEqual(event["sequence"], 3)
            _, duplicate = ledger.append_entry(root, directory / "evidence-ledger.jsonl", task="TASK-4")
            self.assertTrue(duplicate)
        with mock.patch.object(ledger, "read_json_input", return_value=seal_request):
            sealed = ledger.seal_terminal(root, task="TASK-4")
            self.assertEqual(sealed, ledger.seal_terminal(root, task="TASK-4"))
        records = ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4")
        self.assertEqual(len(records), 4)
        sealed_bytes = (directory / "evidence-continuation.jsonl").read_bytes()
        with mock.patch.object(ledger, "read_json_input", return_value=payload):
            with self.assertRaisesRegex(ValueError, "sealed"):
                ledger.append_entry(root, directory / "evidence-ledger.jsonl", task="TASK-4")
        self.assertEqual((directory / "evidence-continuation.jsonl").read_bytes(), sealed_bytes)
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)
        janitor = load_template_module(scripts / "workflow-task-janitor", "recovered_janitor")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assertEqual(janitor._read_ledger_records(descriptor, task="TASK-4"), records)
        finally:
            os.close(descriptor)
        cleanup = load_template_module(REPO_ROOT / ".kent/scripts/workflow-prepare-cleanup", "recovered_cleanup")
        self.assertEqual(cleanup.records_at(directory / "evidence-ledger.jsonl", "TASK-4", ledger, janitor), records)

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
    def test_incomplete_recovery_publication_blocks_read_append_and_janitor(self):
        root, scripts, directory, original = self.recovered_fixture()
        (directory / "evidence-recovery.json").unlink()
        ledger = load_template_module(scripts / "workflow-evidence-ledger", "incomplete_ledger")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4")
        before = (directory / "evidence-continuation.jsonl").read_bytes()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            ledger.append_entry(
                root, directory / "evidence-ledger.jsonl", task="TASK-4",
                _payload={
                    "node_key": "implement", "evidence_type": "implementation",
                    "summary": "must not append",
                    "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
                },
            )
        self.assertEqual((directory / "evidence-continuation.jsonl").read_bytes(), before)
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)
        janitor = load_template_module(scripts / "workflow-task-janitor", "incomplete_janitor")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaisesRegex(ValueError, "incomplete"):
                janitor._read_ledger_records(descriptor, task="TASK-4")
        finally:
            os.close(descriptor)

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
                "KENT_SESSION_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "test-session")),
                "KENT_RUN_ID": resolved_run_id,
                "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{resolved_run_id}-step")),
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

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
    def test_v2_seal_is_idempotent_and_janitor_retains_terminal_sentinel(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "context").mkdir()
        (root / ".kent" / "context" / "implement.md").write_text("manifest\n")
        (root / "AGENTS.md").write_text("rules\n")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "Add context")
        scripts = self.install_v2_runtime_commands(root)
        self.run_git(root, "add", ".kent/scripts")
        self.run_git(root, "commit", "-q", "-m", "Install runtime fixtures")
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
                    "pr_base_branch": "not-applicable",
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
            janitored.stdout + janitored.stderr,
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
        self.run_git(root, "add", ".kent/scripts")
        self.run_git(root, "commit", "-q", "-m", "Install runtime fixtures")
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
                    "pr_base_branch": "not-applicable",
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
                    "pr_base_branch": "not-applicable",
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
        self.assertFalse(tombstone.exists(), janitor.stdout + janitor.stderr)
        self.assertTrue(
            (runtime_dir / f".evidence-terminal-{hashlib.sha256(b'TASK-4').hexdigest()}").exists()
        )


    def test_ledger_suppresses_duplicate_append_for_same_session_step(self) -> None:
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
        self.assertNotIn("run_id", entries[0])
        self.assertEqual(entries[0]["step_id"], str(uuid.uuid5(uuid.NAMESPACE_URL, "recovered-run-step")))

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

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
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

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
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

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
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

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
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

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
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

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
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
        mode = VERIFY_REPORT.lstat().st_mode
        self.assertTrue(stat.S_ISREG(mode))
        self.assertTrue(mode & stat.S_IXUSR)

    def test_verify_private_tmp_survives_grandchild_close_fds_and_exec(self):
        root = self.create_repository()
        leaf = (
            "import json, os, tempfile; from pathlib import Path; "
            "tmp = Path(os.environ['TMPDIR']); "
            "capture = {'tmp': str(tmp), 'mode': tmp.stat().st_mode & 0o777}; "
            "scratch = tempfile.TemporaryDirectory(dir=tmp); "
            "nested = Path(scratch.name) / 'nested'; nested.mkdir(); "
            "(nested / 'data').write_bytes(b'grandchild'); "
            "assert (nested / 'data').read_bytes() == b'grandchild'; "
            "scratch.cleanup(); "
            "Path('tmp-capture.json').write_text(json.dumps(capture))"
        )
        grandchild = (
            "import os, sys; "
            f"os.execv(sys.executable, [sys.executable, '-c', {leaf!r}])"
        )
        scripts = self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            f"subprocess.run([sys.executable, '-c', {grandchild!r}], "
            "close_fds=True, check=True)\n"
            "print('{\"transition\":\"passed\"}')\n",
        )
        report = self.parse_verification_report(self.run_verify_command(root))
        self.assertEqual(report["code"], "passed", report)
        capture = json.loads((root / "tmp-capture.json").read_text())
        tmp = Path(capture["tmp"])
        self.assertEqual(tmp.parent, root.resolve() / "build" / "kent-workflow")
        self.assertTrue(tmp.name.startswith(".verify-tmp-"))
        self.assertEqual(capture["mode"], 0o700)
        self.assertFalse(tmp.exists(), "empty private root must be removed")

    def test_verify_nonempty_tmp_retains_product_pass_and_digest_diagnostic(self):
        root = self.create_repository()
        scripts = self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "import os, sys\nfrom pathlib import Path\n"
            "tmp = Path(os.environ['TMPDIR'])\n"
            "Path('tmp-capture.txt').write_text(str(tmp))\n"
            "(tmp / 'nested').mkdir()\n"
            "(tmp / 'nested' / 'PRIVATE_CHILD_NAME').write_bytes(b'PRIVATE_CONTENT')\n"
            "sys.stderr.write('product stderr\\n')\n"
            "print('{\"transition\":\"passed\"}')\n",
        )
        workflow = root / "build" / "kent-workflow"
        unrelated = workflow / ".verify-tmp-another-invocation"
        unrelated.mkdir(parents=True, mode=0o700)
        (unrelated / "keep").write_bytes(b"other invocation")
        module = load_template_module(
            scripts / "workflow-verify-report", "verify_tmp_retention",
        )
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "passed", report)
        content = (root / report["log_path"]).read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), report["log_sha256"])
        self.assertIn(report["log_sha256"], report["log_path"])
        stdout, stderr = self.parse_framed_output(content)
        self.assertEqual(json.loads(stdout), {"transition": "passed"})
        tmp = Path((root / "tmp-capture.txt").read_text())
        self.assertEqual((tmp / "nested" / "PRIVATE_CHILD_NAME").read_bytes(), b"PRIVATE_CONTENT")
        self.assertFalse((tmp / ".verification-input").exists())
        self.assertEqual((unrelated / "keep").read_bytes(), b"other invocation")
        diagnostic = stderr.removeprefix(b"product stderr\n")
        self.assertIn(b"Verifier temporary files retained:", diagnostic)
        self.assertIn(str(tmp.relative_to(root.resolve())).encode(), diagnostic)
        self.assertLessEqual(len(diagnostic), 512)
        for forbidden in (b"PRIVATE_CHILD_NAME", b"PRIVATE_CONTENT", str(root.resolve()).encode()):
            self.assertNotIn(forbidden, diagnostic)

    def test_verify_retention_diagnostic_does_not_create_output_overflow(self):
        module = load_template_module(VERIFY_REPORT, "verify_retention_frame")
        stdout = b'{"transition":"passed"}\n'
        diagnostic = b"\nVerifier temporary files retained: build/kent-workflow/.verify-tmp-123-456\n"
        for limit in (64, 128, 1024):
            for excess in (False, True):
                with self.subTest(limit=limit, excess=excess):
                    lengths = [
                        length for length in range(limit)
                        if not module._frame(stdout, b"x" * length, limit)[1]
                    ]
                    stderr = b"x" * (max(lengths) + int(excess))
                    frame, overflow = module._frame(stdout, stderr, limit, diagnostic)
                    self.assertEqual(overflow, excess)
                    self.assertLessEqual(len(frame), limit)
                    out, err = self.parse_framed_output(frame)
                    if limit >= 128:
                        self.assertEqual(out, stdout)
                        self.assertTrue(err.endswith(diagnostic))
        root = self.create_repository()
        limit = 1024
        stderr_length = max(
            length for length in range(limit)
            if not module._frame(stdout, b"x" * length, limit)[1]
        )
        scripts = self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "import os, sys\nfrom pathlib import Path\n"
            "(Path(os.environ['TMPDIR']) / 'keep').touch()\n"
            f"sys.stdout.buffer.write({stdout!r})\n"
            f"sys.stderr.buffer.write(b'x' * {stderr_length})\n",
        )
        module = load_template_module(scripts / "workflow-verify-report", "verify_retention_limit")
        module.OUTPUT_LIMIT = limit
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "passed", report)
        content = (root / report["log_path"]).read_bytes()
        self.assertLessEqual(len(content), limit)
        self.assertEqual(hashlib.sha256(content).hexdigest(), report["log_sha256"])
        self.assertIn(b"Verifier temporary files retained:", self.parse_framed_output(content)[1])


    def test_verify_failed_child_retention_notice_remains_in_bounded_digest(self):
        for transition, exit_code, expected in (
            ("failed", 0, "verification_failed"),
            ("passed", 7, "child_exit_nonzero"),
        ):
            with self.subTest(transition=transition, exit_code=exit_code):
                root = self.create_repository()
                module = load_template_module(VERIFY_REPORT, "retention_failed_frame_capacity")
                limit = 1024
                raw_stdout = json.dumps({"transition": transition}).encode() + b"\n"
                stderr_length = max(
                    length for length in range(limit)
                    if not module._frame(raw_stdout, b"E" * length, limit)[1]
                )
                scripts = self.install_verify_fixture(
                    root,
                    "#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\n"
                    "(Path(os.environ['TMPDIR']) / 'keep').touch()\n"
                    f"sys.stdout.buffer.write({raw_stdout!r})\n"
                    f"sys.stderr.buffer.write(b'E' * {stderr_length})\n"
                    f"sys.exit({exit_code})\n",
                )
                module = load_template_module(
                    scripts / "workflow-verify-report", "retention_failed_report",
                )
                module.OUTPUT_LIMIT = limit
                report = self.run_module_in_workspace(module, root)
                self.assertEqual(report["code"], expected, report)
                self.assertEqual(report["exit_code"], exit_code)
                content = (root / report["log_path"]).read_bytes()
                self.assertLessEqual(len(content), limit)
                self.assertEqual(hashlib.sha256(content).hexdigest(), report["log_sha256"])
                self.assertIn(report["log_sha256"], report["log_path"])
                out, err = self.parse_framed_output(content)
                self.assertEqual(out, raw_stdout)
                private = next((root / "build" / "kent-workflow").glob(".verify-tmp-*"))
                notice = (
                    f"\nVerifier temporary files retained: build/kent-workflow/{private.name}\n"
                ).encode()
                self.assertTrue(err.endswith(notice))
                self.assertTrue((private / "keep").exists())

    def test_verify_private_tmp_prelaunch_drift_blocks_execution_and_cleanup(self):
        for kind in ("directory", "symlink", "mode", "build", "workspace"):
            self._assert_prelaunch_drift_blocks_execution_and_cleanup(kind)

    def _assert_prelaunch_drift_blocks_execution_and_cleanup(self, kind):
        with self.subTest(kind=kind):
            root = self.create_repository()
            scripts = self.install_verify_fixture(
                root,
                "#!/usr/bin/env python3\nfrom pathlib import Path\n"
                "Path('child-ran').touch()\nprint('{\"transition\":\"passed\"}')\n",
            )
            module = load_template_module(scripts / "workflow-verify-report", "verify_tmp_prelaunch")
            outside = root / "external"
            outside.mkdir()
            (outside / "keep").write_bytes(b"external")
            selected = []

            def swap(phase):
                if phase != "after_verifier_open_before_child":
                    return
                tmp = next((root / "build" / "kent-workflow").glob(".verify-tmp-*"))
                selected.append(tmp)
                if kind == "mode":
                    tmp.chmod(0o755)
                elif kind == "build":
                    (root / "build").rename(root / "moved-build")
                    (root / "build").mkdir(mode=0o700)
                elif kind == "workspace":
                    moved = root.with_name(root.name + "-moved")
                    root.rename(moved)
                    self.addCleanup(shutil.rmtree, moved)
                    root.mkdir(mode=0o700)
                else:
                    tmp.rename(tmp.with_name("moved-private"))
                    if kind == "symlink":
                        tmp.symlink_to(outside, target_is_directory=True)
                    else:
                        tmp.mkdir(mode=0o700)

            report = self.run_module_in_workspace(module, root, hook=swap)
            self.assertEqual(report["code"], "log_path_unsafe", report)
            self.assertEqual(len(selected), 1)
            self.assertFalse((root / "child-ran").exists())
            if kind == "workspace":
                moved = root.with_name(root.name + "-moved")
                self.assertFalse((moved / "child-ran").exists())
                self.assertEqual((moved / "external" / "keep").read_bytes(), b"external")
            else:
                self.assertEqual((outside / "keep").read_bytes(), b"external")
            if kind in {"directory", "symlink", "mode"}:
                self.assertTrue(selected[0].exists(), "do not remove the unsafe replacement")

    def test_verify_private_tmp_postlaunch_swap_preserves_replacements(self):
        for kind in ("directory", "symlink", "input"):
            with self.subTest(kind=kind):
                root = self.create_repository()
                outside = root / "external"
                outside.mkdir()
                (outside / "keep").write_bytes(b"external")
                swap = {
                    "directory": "tmp.rename(moved)\ntmp.mkdir(mode=0o700)\n",
                    "symlink": "tmp.rename(moved)\ntmp.symlink_to(Path.cwd() / 'external', target_is_directory=True)\n",
                    "input": (
                        "(tmp / '.verification-input').rename(tmp / 'saved-input')\n"
                        "(tmp / '.verification-input').write_bytes(b'replacement')\n"
                        "(tmp / '.verification-input').chmod(0o600)\n"
                    ),
                }[kind]
                scripts = self.install_verify_fixture(
                    root,
                    "#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n"
                    "tmp = Path(os.environ['TMPDIR'])\n"
                    "Path('tmp-capture.txt').write_text(str(tmp))\n"
                    "moved = tmp.with_name('moved-private')\n"
                    + swap + "print('{\"transition\":\"passed\"}')\n",
                )
                module = load_template_module(scripts / "workflow-verify-report", "verify_tmp_postlaunch")
                report = self.run_module_in_workspace(module, root)
                self.assertEqual(report["code"], "log_path_unsafe", report)
                self.assertIsNone(report["log_path"])
                tmp = Path((root / "tmp-capture.txt").read_text())
                self.assertTrue(tmp.exists())
                self.assertEqual((outside / "keep").read_bytes(), b"external")
                if kind == "input":
                    self.assertEqual((tmp / ".verification-input").read_bytes(), b"replacement")
                else:
                    self.assertTrue((tmp.with_name("moved-private") / ".verification-input").exists())

    def test_verify_private_tmp_revalidates_before_empty_delete(self):
        root = self.create_repository()
        scripts = self.install_verify_fixture(root, "#!/bin/sh\necho '{\"transition\":\"passed\"}'\n")
        module = load_template_module(scripts / "workflow-verify-report", "verify_tmp_delete_gate")
        cleanup = module._cleanup_private_tmp
        replacements = []

        def swap_then_cleanup(*args):
            tmp = Path(module._private_tmp_path(*args))
            tmp.rename(tmp.with_name("moved-private"))
            tmp.mkdir(mode=0o700)
            replacements.append(tmp)
            return cleanup(*args)

        with mock.patch.object(module, "_cleanup_private_tmp", swap_then_cleanup):
            report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "log_path_unsafe")
        self.assertEqual(len(replacements), 1)
        self.assertTrue(replacements[0].is_dir())

    def test_verify_private_tmp_checks_owner_and_held_inode(self):
        root = self.create_repository().resolve()
        module = load_template_module(VERIFY_REPORT, "verify_tmp_owner_inode")
        workflow = root / "build" / "kent-workflow"
        private = workflow / ".verify-tmp-test"
        private.mkdir(parents=True, mode=0o700)
        descriptors = [
            os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for path in (root, root / "build", workflow, private)
        ]
        try:
            args = (root, *descriptors[:3], private.name, descriptors[3])
            self.assertEqual(module._private_tmp_path(*args), str(private))
            real_stat, real_fstat = os.stat, os.fstat

            def wrong_owner(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if path == private.name and kwargs.get("dir_fd") == descriptors[2]:
                    values = list(result)
                    values[stat.ST_UID] = os.getuid() + 1
                    return os.stat_result(values)
                return result

            def wrong_inode(fd):
                result = real_fstat(fd)
                if fd == descriptors[3]:
                    values = list(result)
                    values[stat.ST_INO] += 1
                    return os.stat_result(values)
                return result

            for attribute, replacement in (("stat", wrong_owner), ("fstat", wrong_inode)):
                with self.subTest(attribute=attribute):
                    with mock.patch.object(module.os, attribute, replacement):
                        with self.assertRaises(module.VerificationFailure) as raised:
                            module._private_tmp_path(*args)
                    self.assertEqual(raised.exception.code, "log_path_unsafe")
            self.assertTrue(private.is_dir())
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def test_verify_private_tmp_cleanup_io_errors_are_observable_without_retry(self):
        for operation in ("rmdir", "input-unlink", "fsync"):
            with self.subTest(operation=operation):
                root = self.create_repository()
                scripts = self.install_verify_fixture(root, "#!/bin/sh\necho '{\"transition\":\"passed\"}'\n")
                module = load_template_module(scripts / "workflow-verify-report", "verify_tmp_cleanup_io")
                rmdir, unlink, fsync = module.os.rmdir, module.os.unlink, module.os.fsync
                attempts = []
                removed = []

                def fail_rmdir(name, *args, **kwargs):
                    if str(name).startswith(".verify-tmp-"):
                        attempts.append(name)
                        if operation == "rmdir":
                            raise OSError(errno.EIO, "private diagnostic")
                        result = rmdir(name, *args, **kwargs)
                        removed.append(name)
                        return result
                    return rmdir(name, *args, **kwargs)

                def fail_unlink(name, *args, **kwargs):
                    if name == ".verification-input" and operation == "input-unlink":
                        raise OSError(errno.EIO, "private diagnostic")
                    return unlink(name, *args, **kwargs)

                def fail_fsync(fd):
                    if removed and operation == "fsync":
                        raise OSError(errno.EIO, "private diagnostic")
                    return fsync(fd)

                with mock.patch.object(module.os, "rmdir", fail_rmdir), mock.patch.object(
                    module.os, "unlink", fail_unlink
                ), mock.patch.object(module.os, "fsync", fail_fsync):
                    report = self.run_module_in_workspace(module, root)
                self.assertEqual(report["code"], "internal_error", report)
                self.assertEqual(len(attempts), 1)
                self.assertNotIn("private diagnostic", json.dumps(report))

    def test_verify_private_tmp_finally_preserves_original_failure_and_ownership(self):
        for drift in (False, True):
            with self.subTest(drift=drift):
                root = self.create_repository()
                scripts = self.install_verify_fixture(root, "#!/usr/bin/env unknown\n")
                module = load_template_module(scripts / "workflow-verify-report", "verify_tmp_error_finally")
                cleanup = module._cleanup_private_tmp
                selected = []

                def inspect_cleanup(*args):
                    selected.append(Path(module._private_tmp_path(*args)))
                    if drift:
                        selected[-1].rename(selected[-1].with_name("moved-private"))
                        selected[-1].mkdir(mode=0o700)
                    return cleanup(*args)

                with mock.patch.object(module, "_cleanup_private_tmp", inspect_cleanup):
                    report = self.run_module_in_workspace(module, root)
                self.assertEqual(report["code"], "verifier_unsafe")
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0].exists(), drift)

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

    def test_verify_default_environment_never_writes_bytecode(self) -> None:
        for raw_input, expected_code in (
            ('{"workspace_path":', "input_invalid"),
            (None, "passed"),
        ):
            with self.subTest(expected_code=expected_code):
                root = self.create_repository()
                scripts = self.install_verify_fixture(
                    root,
                    "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
                )
                environment = dict(os.environ)
                environment.pop("PYTHONDONTWRITEBYTECODE", None)

                def bytecode_paths() -> list[Path]:
                    return sorted(
                        path.relative_to(scripts)
                        for path in scripts.rglob("*")
                        if path.name == "__pycache__"
                        or path.suffix in {".pyc", ".pyo"}
                    )

                self.assertEqual(bytecode_paths(), [])
                result = subprocess.run(
                    [sys.executable, str(scripts / "workflow-verify-report")],
                    cwd=root,
                    input=(
                        raw_input
                        if raw_input is not None
                        else json.dumps({"workspace_path": str(root)})
                    ),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report = self.parse_verification_report(
                    json.loads(result.stdout)
                )
                self.assertEqual(report["code"], expected_code)
                self.assertEqual(bytecode_paths(), [])

    def test_verify_finite_shebangs_preserve_exact_interpreter_fd_and_stdin(self):
        for header, interpreter in (
            ("#!/bin/sh", "/bin/sh"),
            ("#!/bin/bash", "/bin/bash"),
            ("#!/usr/bin/env bash", "/bin/bash"),
            ("#!/usr/bin/env python3", sys.executable),
        ):
            for fd_executable in (False, True):
                with self.subTest(header=header, fd_executable=fd_executable):
                    root = self.create_repository()
                    capture = root / "stdin.json"
                    if "python3" in header:
                        body = (
                            "import os, sys\n"
                            "from pathlib import Path\n"
                            f"Path({str(capture)!r}).write_bytes(sys.stdin.buffer.read())\n"
                            "assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'\n"
                            "print('{\"transition\":\"passed\"}')\n"
                        )
                    else:
                        body = (
                            f"cat > {str(capture)!r}\n"
                            + (
                                "values=(passed)\n"
                                '[[ "${values[0]}" == passed ]] || exit 2\n'
                                if "bash" in header else ""
                            )
                            + "printf '%s\\n' '{\"transition\":\"passed\"}'\n"
                        )
                    scripts = self.install_verify_fixture(root, header + "\n" + body)
                    module = load_template_module(scripts / "workflow-verify-report", "verify_finite_shebang")
                    real_access = os.access
                    real_command = module._child_command
                    selected = []

                    def access(path, mode, *args, **kwargs):
                        if str(path).startswith("/dev/fd/"):
                            return fd_executable
                        return real_access(path, mode, *args, **kwargs)

                    def command(path, fd):
                        argv = real_command(path, fd)
                        self.assertEqual(argv, [interpreter, path])
                        self.assertEqual(path, f"/dev/fd/{fd}")
                        selected.append(argv)
                        return argv

                    payload = {"workspace_path": str(root), "unicode": "é", "items": [1, "two"]}
                    previous = Path.cwd()
                    try:
                        os.chdir(root)
                        with mock.patch.object(os, "access", side_effect=access), \
                             mock.patch.object(module, "_child_command", side_effect=command), \
                             mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "0"}):
                            emitted = module.run_verification(payload)
                    finally:
                        os.chdir(previous)
                    self.assertEqual(emitted["code"], "passed")
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(
                        capture.read_bytes(),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
                    )
                    self.assertEqual(list(scripts.rglob("*.pyc")), [])
                    self.assertEqual(list((root / "build/kent-workflow").glob(".verify-tmp-*")), [])

    def test_verify_unsupported_shebang_forms_fail_before_child_launch(self):
        headers = (
            b"#!/bin/zsh\n", b"#!/usr/bin/python3\n", b"#!/bin/python3\n",
            b"#!/usr/bin/env sh\n", b"#!/usr/bin/env /bin/bash\n",
            b"#!/bin/bash -e\n", b"#!/bin/sh -eu\n",
            b"#!/usr/bin/env bash -e\n", b"#!/usr/bin/env python3 -B\n",
            b"#!/usr/bin/env -S bash\n", b"#!/usr/bin/env NAME=value bash\n",
            b"#!/usr/bin/env bash \n", b"#!/usr/bin/env  bash\n",
            b"#! /bin/bash\n", b"#!/bin/bash\t\n",
            b"#!/tmp/not-bash\n", b"#!/tmp/python-bash\n",
            b"#!/bin/bashx\n", b"#!/usr/bin/env not-python3\n",
            b"#!/bin/bash\r\n", b"#!/bin/bash\x00\n",
            b"#!/bin/bash", b"#!/usr/bin/env python3", b"#!",
            b"#!" + b"x" * 253 + b"\n",
            b"#!" + b"x" * 254 + b"\n",
            b"#!" + b"x" * 4096 + b"\n",
        )
        for header in headers:
            for fd_executable in (False, True):
                with self.subTest(header=header[:60], length=len(header), fd_executable=fd_executable):
                    root = self.create_repository()
                    scripts = self.install_verify_fixture(root, "#!/bin/sh\n")
                    (scripts / "workflow-compile-verify").write_bytes(
                        header + b"echo '{\"transition\":\"passed\"}'\n"
                    )
                    module = load_template_module(scripts / "workflow-verify-report", "verify_rejected_shebang")
                    real_access = os.access
                    real_popen = subprocess.Popen
                    launches = []

                    def access(path, mode, *args, **kwargs):
                        if str(path).startswith("/dev/fd/"):
                            return fd_executable
                        return real_access(path, mode, *args, **kwargs)

                    def popen(argv, *args, **kwargs):
                        if kwargs.get("pass_fds"):
                            launches.append(argv)
                        return real_popen(argv, *args, **kwargs)

                    with mock.patch.object(os, "access", side_effect=access), \
                         mock.patch.object(subprocess, "Popen", side_effect=popen):
                        emitted = self.run_module_in_workspace(module, root)
                    report = emitted
                    self.assertEqual(report["code"], "verifier_unsafe", report)
                    self.assertEqual(launches, [])
                    self.assertEqual(list((root / "build/kent-workflow").glob(".verify-tmp-*")), [])

    def test_verify_header_read_is_capped_offset_preserving_and_failures_explicit(self):
        module = load_template_module(VERIFY_REPORT, "verify_shebang_read_bounds")
        root = self.create_repository()
        script = root / "header"
        script.write_bytes(b"#!/bin/bash\n" + b"x" * 4096)
        descriptor = os.open(script, os.O_RDONLY)
        try:
            os.lseek(descriptor, 7, os.SEEK_SET)
            with mock.patch.object(os, "pread", wraps=os.pread) as reads:
                argv = module._child_command(f"/dev/fd/{descriptor}", descriptor)
            self.assertEqual(argv, ["/bin/bash", f"/dev/fd/{descriptor}"])
            reads.assert_called_once_with(descriptor, 257, 0)
            self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), 7)
            with mock.patch.object(os, "pread", side_effect=OSError(errno.EIO, "read failure")), \
                 self.assertRaises(OSError):
                module._child_command(f"/dev/fd/{descriptor}", descriptor)
        finally:
            os.close(descriptor)
        for raw in (b"#!/bin/bash", b"#!/usr/bin/env bash", b"#!/bin/sh", b"#!/usr/bin/env python3"):
            with tempfile.TemporaryFile() as stream:
                stream.write(raw)
                stream.flush()
                with self.assertRaises(module.VerificationFailure) as raised:
                    module._child_command(f"/dev/fd/{stream.fileno()}", stream.fileno())
                self.assertEqual(raised.exception.code, "verifier_unsafe")

    def test_verify_native_non_shebang_dispatch_has_no_interpreter_fallback(self):
        module = load_template_module(VERIFY_REPORT, "verify_native_fd_dispatch")
        with tempfile.TemporaryFile() as stream:
            for raw in (b"\x7fELF\x00native", b"\xcf\xfa\xed\xfe\x00native", b"plain executable bytes"):
                stream.seek(0)
                stream.truncate()
                stream.write(raw)
                stream.flush()
                fd_path = f"/dev/fd/{stream.fileno()}"
                with mock.patch.object(module, "_validated_interpreter") as interpreter, \
                     mock.patch.object(os, "access", return_value=False):
                    self.assertEqual(module._child_command(fd_path, stream.fileno()), [fd_path])
                interpreter.assert_not_called()

    def test_verify_interpreter_canonical_targets_permissions_and_symlinks(self):
        module = load_template_module(VERIFY_REPORT, "verify_trusted_interpreter")
        root = self.create_repository()
        target = root / "trusted-runtime"
        target.write_bytes(b"fixture executable")
        target.chmod(0o755)
        alias = root / "trusted-installation-link"
        alias.symlink_to(target)
        self.assertEqual(module._validated_interpreter(str(alias)), str(alias))
        real_stat = os.stat

        def system_owned(path, *args, **kwargs):
            metadata = real_stat(path, *args, **kwargs)
            if Path(path) == target:
                fields = list(metadata)
                fields[3] = 2  # Trusted installations may have multiple links.
                fields[4] = 0  # System ownership differs from a non-root project owner.
                return os.stat_result(fields)
            return metadata

        with mock.patch.object(os, "stat", side_effect=system_owned):
            self.assertEqual(module._validated_interpreter(str(alias)), str(alias))
        for mode in (0o644, 0o775, 0o757, 0o777):
            with self.subTest(mode=oct(mode)):
                target.chmod(mode)
                with self.assertRaises(module.VerificationFailure) as raised:
                    module._validated_interpreter(str(alias))
                self.assertEqual(raised.exception.code, "verifier_unsafe")
        target.chmod(0o755)
        for path, code in (
            (str(root / "missing"), "verification_blocked"),
            (str(root), "verifier_unsafe"),
            ("python3", "verifier_unsafe"),
            ("", "verifier_unsafe"),
        ):
            with self.subTest(path=path), self.assertRaises(module.VerificationFailure) as raised:
                module._validated_interpreter(path)
            self.assertEqual(raised.exception.code, code)
        with mock.patch.object(os, "access", return_value=False), \
             self.assertRaises(module.VerificationFailure) as raised:
            module._validated_interpreter(str(alias))
        self.assertEqual(raised.exception.code, "verifier_unsafe")
        loop = root / "interpreter-loop"
        loop.symlink_to(loop)
        with self.assertRaises(module.VerificationFailure) as raised:
            module._validated_interpreter(str(loop))
        self.assertEqual(raised.exception.code, "verifier_unsafe")

    def test_verify_interpreter_low_level_failures_remain_explicit_without_launch(self):
        for failure in ("pread", "interpreter_stat"):
            with self.subTest(failure=failure):
                root = self.create_repository()
                scripts = self.install_verify_fixture(
                    root, "#!/usr/bin/env python3\nprint('{\"transition\":\"passed\"}')\n",
                )
                module = load_template_module(scripts / "workflow-verify-report", "verify_dispatch_io_error")
                real_popen = subprocess.Popen
                real_stat = os.stat
                python_target = Path(sys.executable).resolve()
                launched = []

                def popen(argv, *args, **kwargs):
                    if kwargs.get("pass_fds"):
                        launched.append(argv)
                    return real_popen(argv, *args, **kwargs)

                def interpreter_stat(path, *args, **kwargs):
                    if Path(path) == python_target and kwargs.get("follow_symlinks") is False:
                        raise OSError(errno.EIO, "interpreter stat failure")
                    return real_stat(path, *args, **kwargs)

                failing = (
                    mock.patch.object(os, "pread", side_effect=OSError(errno.EIO, "script pread failure"))
                    if failure == "pread"
                    else mock.patch.object(os, "stat", side_effect=interpreter_stat)
                )
                with failing, mock.patch.object(subprocess, "Popen", side_effect=popen):
                    report = self.run_module_in_workspace(module, root)
                self.assertEqual(report["code"], "internal_error", report)
                self.assertEqual(launched, [])
                self.assertEqual(list((root / "build/kent-workflow").glob(".verify-tmp-*")), [])

    def test_verify_missing_or_unsafe_interpreter_uses_existing_report_codes(self):
        for state in ("missing", "nonexecutable", "writable", "directory", "relative"):
            with self.subTest(state=state):
                root = self.create_repository()
                scripts = self.install_verify_fixture(root, "#!/usr/bin/env python3\nprint('unused')\n")
                interpreter = root / "test-interpreter"
                if state == "directory":
                    interpreter.mkdir()
                elif state != "missing":
                    interpreter.write_bytes(b"fixture interpreter")
                    interpreter.chmod(0o644 if state == "nonexecutable" else 0o777 if state == "writable" else 0o755)
                module = load_template_module(scripts / "workflow-verify-report", "verify_interpreter_failure")
                with mock.patch.object(module, "sys", types.SimpleNamespace(
                    executable="relative-python" if state == "relative" else str(interpreter),
                )):
                    emitted = self.run_module_in_workspace(module, root)
                report = emitted
                self.assertEqual(report["code"], "verification_blocked" if state == "missing" else "verifier_unsafe")
                self.assertIsNone(report["exit_code"])
                self.assertIsNone(report["log_path"])
                self.assertEqual(list((root / "build/kent-workflow").glob(".verify-tmp-*")), [])

    def test_verify_shebang_never_selects_interpreter_from_path_or_env(self):
        for header in ("#!/usr/bin/env bash", "#!/usr/bin/env python3"):
            with self.subTest(header=header):
                root = self.create_repository()
                marker = root / "interpreter-hijacked"
                java = root / "fake-java"
                (java / "bin").mkdir(parents=True)
                for name in ("env", "bash", "python3"):
                    fake = java / "bin" / name
                    fake.write_text(f"#!/bin/sh\ntouch {str(marker)!r}\nexit 99\n")
                    fake.chmod(0o755)
                body = (
                    "values=(passed)\n[[ ${values[0]} == passed ]] || exit 2\n"
                    "printf '%s\\n' '{\"transition\":\"passed\"}'\n"
                    if "bash" in header else "print('{\"transition\":\"passed\"}')\n"
                )
                self.install_verify_fixture(root, header + "\n" + body)
                emitted = self.run_verify_command(root, environment={
                    **os.environ, "PATH": str(java / "bin"), "JAVA_HOME": str(java),
                    "BASH_ENV": str(java / "bin/bash"),
                    "ENV": str(java / "bin/env"), "PYTHONDONTWRITEBYTECODE": "0",
                })
                self.assertEqual(self.parse_verification_report(emitted)["code"], "passed")
                self.assertFalse(marker.exists())

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
            "PYTHONDONTWRITEBYTECODE": "0",
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
        private_tmp = Path(captured["tmp"])
        self.assertEqual(private_tmp.parent, root.resolve() / "build" / "kent-workflow")
        self.assertTrue(private_tmp.name.startswith(".verify-tmp-"))
        self.assertFalse(private_tmp.exists())
        expected_keys = {
            "CI",
            "LANG",
            "LC_ALL",
            "GIT_TERMINAL_PROMPT",
            "GCM_INTERACTIVE",
            "PYTHONDONTWRITEBYTECODE",
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
        self.assertEqual(captured["env"]["PYTHONDONTWRITEBYTECODE"], "1")
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
        try:
            environment = module.replacement_environment("/private/verify-tmp")
        finally:
            module.os = original_os
        expected_keys = {
            "CI",
            "LANG",
            "LC_ALL",
            "GIT_TERMINAL_PROMPT",
            "GCM_INTERACTIVE",
            "PYTHONDONTWRITEBYTECODE",
            "TMPDIR",
            "PATH",
            "HOME",
            "JAVA_HOME",
        }
        self.assertEqual(set(environment), expected_keys)
        self.assertEqual(environment["TMPDIR"], "/private/verify-tmp")
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(java / "bin"))
        self.assertNotIn("KENT_SECRET", environment)
        self.assertNotIn("PYTHONHOME", environment)
        self.assertNotIn("__CF_USER_TEXT_ENCODING", environment)

    def test_verify_fd_path_descriptor_identity_matrix(self) -> None:
        module = load_template_module(
            VERIFY_REPORT,
            "verify_report_fd_path_identity_matrix",
        )
        base_os = module.os

        class DelegatingOS:
            def __init__(self, fstat, stat):
                self._base = base_os
                self._fstat = fstat
                self._stat = stat
                self.path = base_os.path

            def fstat(self, descriptor):
                return self._fstat(descriptor)

            def stat(self, path):
                return self._stat(path)

            def __getattr__(self, name):
                return getattr(self._base, name)

        def metadata(device: int, inode: int):
            return types.SimpleNamespace(st_dev=device, st_ino=inode)

        cases = (
            (
                "initial_fstat_failure",
                [OSError(errno.EBADF, "initial")],
                [],
                "verifier_unsafe",
                1,
                0,
            ),
            (
                "transient_ebadf",
                [metadata(1, 2), metadata(1, 2), metadata(1, 2)],
                [OSError(errno.EBADF, "first"), metadata(9, 2)],
                None,
                3,
                2,
            ),
            (
                "persistent_ebadf",
                [metadata(1, 2), metadata(1, 2)],
                [OSError(errno.EBADF, "first"), OSError(errno.EBADF, "second")],
                "verifier_unsafe",
                2,
                2,
            ),
            (
                "first_non_ebadf",
                [metadata(1, 2)],
                [OSError(errno.EACCES, "denied")],
                "verifier_unsafe",
                1,
                1,
            ),
            (
                "retry_non_ebadf",
                [metadata(1, 2), metadata(1, 2)],
                [OSError(errno.EBADF, "first"), OSError(errno.EACCES, "denied")],
                "verifier_unsafe",
                2,
                2,
            ),
            (
                "current_fstat_failure",
                [metadata(1, 2), OSError(errno.EIO, "current")],
                [OSError(errno.EBADF, "first")],
                "verifier_unsafe",
                2,
                1,
            ),
            (
                "identity_change_after_ebadf",
                [metadata(1, 2), metadata(1, 3)],
                [OSError(errno.EBADF, "first")],
                "verifier_unsafe",
                2,
                1,
            ),
            (
                "final_fstat_failure",
                [metadata(1, 2), OSError(errno.EIO, "final")],
                [metadata(9, 2)],
                "verifier_unsafe",
                2,
                1,
            ),
            (
                "final_identity_change",
                [metadata(1, 2), metadata(1, 3)],
                [metadata(9, 2)],
                "verifier_unsafe",
                2,
                1,
            ),
            (
                "resolved_device_change_allowed",
                [metadata(1, 2), metadata(1, 2)],
                [metadata(9, 2)],
                None,
                2,
                1,
            ),
            (
                "resolved_inode_mismatch",
                [metadata(1, 2), metadata(1, 2)],
                [metadata(9, 3)],
                "verifier_unsafe",
                2,
                1,
            ),
        )
        original_stat = os.stat
        original_fstat = os.fstat
        for name, fstat_values, stat_values, code, expected_fstat, expected_stat in cases:
            with self.subTest(case=name):
                fstat_calls = 0
                stat_calls = 0

                def fake_fstat(_descriptor):
                    nonlocal fstat_calls
                    value = fstat_values[fstat_calls]
                    fstat_calls += 1
                    if isinstance(value, BaseException):
                        raise value
                    return value

                def fake_stat(_path):
                    nonlocal stat_calls
                    value = stat_values[stat_calls]
                    stat_calls += 1
                    if isinstance(value, BaseException):
                        raise value
                    return value

                with mock.patch.object(
                    module,
                    "os",
                    DelegatingOS(fake_fstat, fake_stat),
                ):
                    if code is None:
                        self.assertEqual(module._fd_path(42), "/dev/fd/42")
                    else:
                        with self.assertRaises(module.VerificationFailure) as raised:
                            module._fd_path(42)
                        self.assertEqual(raised.exception.code, code)
                self.assertEqual(fstat_calls, expected_fstat)
                self.assertEqual(stat_calls, expected_stat)
        self.assertIs(os.stat, original_stat)
        self.assertIs(os.fstat, original_fstat)

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

    def test_verify_post_spawn_io_failures_settle_child_and_close_handles(self) -> None:
        from contextlib import ExitStack

        for phase in (
            "set_blocking",
            "selector_create",
            "register",
            "select",
            "read",
            "cleanup_path",
        ):
            with self.subTest(phase=phase):
                root = self.create_repository()
                scripts = self.install_verify_fixture(
                    root,
                    "#!/bin/sh\nprintf 'ready\\n'\nexec /bin/sleep 60\n",
                )
                module = load_template_module(
                    scripts / "workflow-verify-report",
                    "verify_owned_child_" + phase,
                )
                children = []
                selectors = []
                original_popen = module.subprocess.Popen
                original_selector = module.selectors.DefaultSelector
                original_blocking = module.os.set_blocking
                original_read = module.os.read
                original_private_tmp_path = module._private_tmp_path
                private_path_calls = 0

                def capture_popen(*args, **kwargs):
                    process = original_popen(*args, **kwargs)
                    if kwargs.get("start_new_session"):
                        children.append(process)
                    return process

                def injected_set_blocking(fd, value):
                    if phase in {"set_blocking", "cleanup_path"} and children:
                        raise OSError(errno.EIO, "injected post-spawn set_blocking")
                    return original_blocking(fd, value)

                class TrackingSelector(original_selector):
                    def __init__(self):
                        if phase == "selector_create":
                            raise OSError(errno.EIO, "injected post-spawn selector creation")
                        super().__init__()
                        selectors.append(self)

                    def register(self, *args, **kwargs):
                        if phase == "register":
                            raise OSError(errno.EIO, "injected post-spawn register")
                        return super().register(*args, **kwargs)

                    def select(self, *args, **kwargs):
                        if phase == "select":
                            raise ValueError("injected post-spawn select")
                        return super().select(*args, **kwargs)

                def injected_read(fd, count):
                    pipe_fds = {
                        stream.fileno()
                        for process in children
                        for stream in (process.stdout, process.stderr)
                        if stream is not None and not stream.closed
                    }
                    if phase == "read" and fd in pipe_fds:
                        raise OSError(errno.EIO, "injected post-spawn read")
                    return original_read(fd, count)

                def injected_private_tmp_path(*args, **kwargs):
                    nonlocal private_path_calls
                    private_path_calls += 1
                    if phase == "cleanup_path" and private_path_calls == 2:
                        raise module.VerificationFailure("log_path_unsafe")
                    return original_private_tmp_path(*args, **kwargs)

                try:
                    with ExitStack() as stack:
                        stack.enter_context(
                            mock.patch.object(
                                module.subprocess,
                                "Popen",
                                capture_popen,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                module.os,
                                "set_blocking",
                                injected_set_blocking,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(module.os, "read", injected_read)
                        )
                        stack.enter_context(
                            mock.patch.object(
                                module.selectors,
                                "DefaultSelector",
                                TrackingSelector,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                module, "_private_tmp_path", injected_private_tmp_path
                            )
                        )
                        report = self.run_module_in_workspace(module, root)

                    self.assertEqual(len(children), 1)
                    child = children[0]
                    self.assertEqual(report["code"], "internal_error", report)
                    self.assertIsNotNone(child.poll(), "child must be reaped before return")
                    self.assertTrue(child.stdout.closed)
                    self.assertTrue(child.stderr.closed)
                    self.assertTrue(
                        all(selector.get_map() is None for selector in selectors)
                    )
                    if phase == "cleanup_path":
                        self.assertGreaterEqual(private_path_calls, 2)
                finally:
                    for child in children:
                        if child.poll() is None:
                            module._terminate_group(child)
                        for stream in (child.stdout, child.stderr):
                            if stream is not None:
                                stream.close()
                    for selector in selectors:
                        selector.close()

    def test_verify_malformed_output_retains_published_log_reference(self) -> None:
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/bin/sh\necho not-json\necho DIAGNOSTIC >&2\n",
        )

        payload = self.run_verify_command(root)
        report = self.parse_verification_report(payload)

        self.assertEqual(payload["verification_status"], "blocked")
        self.assertEqual(report["code"], "child_output_invalid")
        self.assertIsNone(report["exit_code"])
        self.assertIsNotNone(report["log_path"])
        self.assertIsNotNone(report["log_sha256"])
        log = root / str(report["log_path"])
        content = log.read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), report["log_sha256"])
        stdout, stderr = self.parse_framed_output(content)
        self.assertEqual(stdout, b"not-json\n")
        self.assertEqual(stderr, b"DIAGNOSTIC\n")

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

        timeout_ready = outside / "timeout-grandchild-ready"
        timeout_release = outside / "timeout-grandchild-release"
        timeout_marker = outside / "timeout-grandchild-marker"
        def release_and_wait(release: Path, ready: Path) -> None:
            release.touch()
            if ready.exists():
                assert_pids_gone(self, ready, timeout=5.0)
        def grandchild_code(ready: Path, release: Path, marker: Path) -> str:
            return (
                "import os, pathlib, time; "
                + f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
                + f"release = pathlib.Path({str(release)!r}); "
                + "exec('while not release.exists():\\n time.sleep(0.01)'); "
                + f"pathlib.Path({str(marker)!r}).write_text('leaked')"
            )
        self.addCleanup(release_and_wait, timeout_release, timeout_ready)
        timeout_code = grandchild_code(
            timeout_ready, timeout_release, timeout_marker
        )
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "import pathlib, subprocess, sys, time\n"
            + f"subprocess.Popen([sys.executable, '-c', {timeout_code!r}])\n"
            + f"deadline = time.monotonic() + 1; ready = pathlib.Path({str(timeout_ready)!r})\n"
            + "while not ready.exists() and time.monotonic() < deadline: time.sleep(0.001)\n"
            + "assert ready.exists()\n"
            "time.sleep(5)\n",
        )
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_timeout_group_test",
        )
        module.TIMEOUT_SECONDS = 0.05
        original_popen = module.subprocess.Popen

        def wait_for_timeout_ready(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            if kwargs.get("start_new_session"): self.assertTrue(
                self.wait_for_path(timeout_ready, 5)
            )
            return process

        with mock.patch.object(
            module.subprocess,
            "Popen",
            side_effect=wait_for_timeout_ready,
        ):
            report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "child_timeout")
        assert_pids_gone(self, timeout_ready)
        timeout_release.touch()
        self.assert_no_path_for_duration(timeout_marker, 0.7)
        self.assertFalse(timeout_marker.exists())
        output_ready = outside / "output-grandchild-ready"
        output_release = outside / "output-grandchild-release"
        output_marker = outside / "output-grandchild-marker"
        self.addCleanup(release_and_wait, output_release, output_ready)
        output_code = grandchild_code(
            output_ready, output_release, output_marker
        )
        root = self.create_repository()
        self.install_verify_fixture(
            root,
            "#!/usr/bin/env python3\n"
            "import pathlib, subprocess, sys, time\n"
            + f"subprocess.Popen([sys.executable, '-c', {output_code!r}])\n"
            + f"deadline = time.monotonic() + 1; ready = pathlib.Path({str(output_ready)!r})\n"
            + "while not ready.exists() and time.monotonic() < deadline: time.sleep(0.001)\n"
            + "assert ready.exists()\n"
            "sys.stdout.write('x' * (5 * 1024))\n"
            "sys.stdout.flush()\n"
            "time.sleep(5)\n",
        )
        module = load_template_module(
            root / ".kent" / "scripts" / "workflow-verify-report",
            "verify_report_output_group_test",
        )
        module.OUTPUT_LIMIT = 4 * 1024
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "log_limit_exceeded")
        self.assertTrue(self.wait_for_path(output_ready))
        assert_pids_gone(self, output_ready)
        output_release.touch()
        self.assert_no_path_for_duration(output_marker, 0.7)
        self.assertFalse(output_marker.exists())

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

    def test_verify_unsafe_log_paths_have_exact_code(self) -> None:
        framing = load_template_module(VERIFY_REPORT, "verify_existing_log_frame")
        content, overflow = framing._frame(b'{"transition":"passed"}\n', b"", framing.OUTPUT_LIMIT)
        self.assertFalse(overflow)
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
                report = self.run_module_in_workspace(module, root)
                self.assertEqual(report["code"], "log_path_unsafe", report)
                self.assertIsNone(report["log_path"])
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
        report = self.run_module_in_workspace(module, root)
        self.assertEqual(report["code"], "passed", report)
        self.assertEqual(report["log_path"], f"build/kent-workflow/{final.name}")
        self.assertEqual(report["log_sha256"], digest)
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
        scripts = self.install_verify_fixture(
            root,
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"passed\"}'\n",
        )
        module = load_template_module(
            scripts / "workflow-verify-report",
            "verify_report_symlink_test",
        )

        def swap_directory(phase: str) -> None:
            if phase != "after_log_temp_open_before_child":
                return
            build = root / "build"
            moved = root / "build-original"
            build.rename(moved)
            build.symlink_to(outside, target_is_directory=True)

        report = self.run_module_in_workspace(module, root, hook=swap_directory)
        self.assertEqual(report["code"], "log_path_unsafe", report)
        self.assertIsNone(report["log_path"])
        self.assertEqual(list(outside.iterdir()), [])


class GitHubPrFeedbackTest(GitRepositoryTest):
    def test_feedback_subprocess_materializes_all_item_variants(self) -> None:
        root = self.create_repository()
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *issues/*/comments*) printf \'[[{"id":1,"body":"issue body","created_at":"'
            '2026-08-20T00:00:00Z","updated_at":"2026-08-20T00:00:00Z",'
            '"user":{"login":"alice"}}]]\\n\' ;;\n'
            '  *pulls/*/reviews*) printf \'[[{"id":2,"body":"review body","state":"COMMENTED",'
            '"submitted_at":"2026-08-20T00:01:00Z","user":{"login":"bob"},'
            '"commit_id":"0000000000000000000000000000000000000000"}]]\\n\' ;;\n'
            '  *pulls/*/comments*) printf \'[[{"id":3,"node_id":"thread-comment-1","body":"inline body","created_at":"'
            '2026-08-20T00:02:00Z","updated_at":"2026-08-20T00:02:00Z",'
            '"user":{"login":"carol"},"commit_id":"0000000000000000000000000000000000000000",'
            '"original_commit_id":"0000000000000000000000000000000000000000",'
            '"pull_request_review_thread_id":"thread-1"}]]\\n\' ;;\n'
            '  *graphql*) printf \'{"data":{"repository":{"pullRequest":{"reviewThreads":'
            '{"nodes":[{"id":"thread-1","isResolved":true,"isOutdated":false,'
            '"path":"src/app.py","line":12,"startLine":10,"originalLine":20,'
            '"originalStartLine":18,"subjectType":"LINE","comments":{"nodes":'
            '[{"id":"thread-comment-1"}],"pageInfo":{"hasNextPage":false,'
            '"endCursor":null}}}],"pageInfo":{"hasNextPage":false,'
            '"endCursor":null}}}}}}\\n\' ;;\n'
            '  *) printf \'[]\\n\' ;;\n'
            "esac\n"
        )
        fake_gh.chmod(0o755)
        from workflowkit.github_observation import GitHubObserver
        from workflowkit.runtime import validate_pr_feedback_item

        items = GitHubObserver(
            root,
            gh_bin=str(fake_gh),
            timeout=5,
        ).read_feedback("https://github.com/example/repo/pull/1")
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
        for item in items:
            validate_pr_feedback_item(item)

    def test_feedback_cursor_identity_matrix_and_limits(self) -> None:
        from workflowkit import github_observation as module
        from workflowkit import runtime
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
            thread_id="thread-1",
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
                "event": "pull_request",
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
                "review_decision": "",
            }
            state.update(overrides)
            return runtime.make_dynamic_pr_feedback_cursor(
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
            runtime.diff_dynamic_pr_feedback_cursor("uninitialized", complete)["transition"],
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
            ("review-decision", items, checks, {"review_decision": "APPROVED"}),
        ]
        for label, item_rows, check_rows, overrides in mutations:
            with self.subTest(label=label):
                changed = cursor(
                    item_rows=item_rows,
                    check_rows=check_rows,
                    **overrides,
                )
                self.assertEqual(
                    runtime.diff_dynamic_pr_feedback_cursor(complete, changed)["transition"],
                    "still_waiting" if label in {
                        "issue-created", "issue-updated", "comment-created",
                        "comment-updated", "check-add", "check-delete", "check-edit",
                    } else "state_changed",
                )
        for identity in ({"repository": "other/repo"}, {"pull_number": 2}):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(ValueError, "same pull request"):
                    runtime.diff_dynamic_pr_feedback_cursor(complete, cursor(**identity))

        many = [
            {**issue, "id": "issue-{:03d}".format(index)}
            for index in range(101)
        ]
        digest_only = cursor(item_rows=many)
        self.assertEqual(digest_only["mode"], "digest_only")
        self.assertEqual(
            runtime.diff_dynamic_pr_feedback_cursor(digest_only, digest_only)["transition"],
            "still_waiting",
        )
        changed_digest = [
            {**item, "body_sha256": hashlib.sha256(b"changed").hexdigest()}
            if index == 0
            else item
            for index, item in enumerate(many)
        ]
        self.assertEqual(
            runtime.diff_dynamic_pr_feedback_cursor(
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
        from workflowkit.github_observation import GitHubObserver, ObservationError

        paged = root / "gh-paged"
        paged.write_text(
            "#!/bin/sh\n"
            "printf '[[{\"id\":\"one\"}],[{\"id\":\"two\"}]]\\n'\n"
        )
        paged.chmod(0o755)
        rows = GitHubObserver(
            root,
            gh_bin=str(paged),
            timeout=5,
        )._rest_pages("repos/example/repo/issues/1/comments")
        self.assertEqual([row["id"] for row in rows], ["one", "two"])

        failing = root / "gh-failing"
        failing.write_text("#!/bin/sh\nprintf out\nprintf err >&2\nexit 7\n")
        failing.chmod(0o755)
        with self.assertRaises(ObservationError) as failure:
            GitHubObserver(
                root,
                gh_bin=str(failing),
                timeout=5,
            )._rest_pages("repos/example/repo/issues/1/comments")
        self.assertEqual(failure.exception.code, "query_failed")
        self.assertEqual(
            failure.exception.stdout_sha256,
            hashlib.sha256(b"out").hexdigest(),
        )
        self.assertEqual(
            failure.exception.stderr_sha256,
            hashlib.sha256(b"err").hexdigest(),
        )
        hung = root / "gh-hung"
        hung.write_text("#!/bin/sh\nsleep 2\n")
        hung.chmod(0o755)
        with self.assertRaises(ObservationError) as timeout_error:
            GitHubObserver(
                root,
                gh_bin=str(hung),
                timeout=1,
            )._rest_pages("repos/example/repo/issues/1/comments")
        self.assertEqual(timeout_error.exception.code, "query_timeout")

        malformed = root / "gh-malformed"
        malformed.write_text("#!/bin/sh\nprintf '[[{}], [\"bad\"]]\\n'\n")
        malformed.chmod(0o755)
        with self.assertRaises(ObservationError) as output:
            GitHubObserver(
                root,
                gh_bin=str(malformed),
                timeout=5,
            )._rest_pages("repos/example/repo/issues/1/comments")
        self.assertEqual(output.exception.code, "invalid_observation")


class GitHubWatchSupportTest(GitRepositoryTest):
    def test_ci_and_pr_emitters_preserve_original_input_for_capture(self):
        root = self.create_repository().resolve()
        scripts = root / ".kent/scripts"
        scripts.mkdir()
        (root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n')
        (scripts / "workflow-checkpoint").write_text(
            "import json\n"
            "def emit_producer_result(root, payload, raw, kind):\n"
            "    assert payload['delivery_context'] == ' exact incoming '\n"
            "    value = json.loads(raw)\n"
            "    value['commentary'] = kind + ':captured'\n"
            "    return json.dumps(value).encode()\n")
        for source, kind in ((CI_WATCH, "wait-ci"), (PR_WATCH, "wait-pr")):
            target = scripts / source.name
            shutil.copyfile(source, target)
            module = load_template_module(target, kind.replace("-", "_") + "_capture")
            output = io.BytesIO()
            stream = types.SimpleNamespace(buffer=output)
            with mock.patch.object(module.Path, "cwd", return_value=root), \
                    mock.patch.object(module.sys, "stdout", stream):
                module._emit("failed", {"ci_report": " exact output "},
                             producer_payload={"delivery_context": " exact incoming "})
            value = json.loads(output.getvalue())
            self.assertEqual(value["ci_report"], " exact output ")
            self.assertEqual(value["commentary"], kind + ":captured")


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
            "workflow_runtime_contracts.py support module is unsafe",
        ):
            module.runtime_contracts()
        self.assertFalse(marker.exists())


    def test_v2_report_reader_rejects_malformed_history_fields(self) -> None:
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
        discarded = {
            "sequence": 1,
            "head_oid": "0" * 40,
            "base_oid": "1" * 40,
            "reason": "all_expected_checks_terminal_green",
            "watcher_exit_code": 0,
            "expected_checks": [observed],
            "unexpected_check_count": 0,
            "unexpected_checks_sha256": runtime.canonical_sha256([]),
            "retry": None,
            "safe_error": None,
        }
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
        runtime.validate_ci_report(report)
        invalid = {
            "unknown": {**report, "unknown": True},
            "noncontiguous": {
                **report,
                "attempts": [{**report["attempts"][0], "sequence": 3}],
            },
            "count": {**report, "discarded_attempt_count": 0},
            "digest": {**report, "discarded_attempts_sha256": "not-a-digest"},
        }
        for label, value in invalid.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    runtime.validate_ci_report(value)

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

        base = [{
            "workflow_name": "PR",
            "check_name": "unit",
            "bucket": "pass",
            "state": "SUCCESS",
            "link": None,
        }]
        valid = {
            "schema": "github-ci-report-v2",
            "mode": "expected-v1",
            "repository": "example/repo",
            "pull_number": 1,
            "runtime_source_envelope_digest": "a" * 64,
            "expected_ci_checks_sha256": "b" * 64,
            "discarded_attempt_count": 0,
            "discarded_attempts_sha256": "0" * 64,
            "attempts": [attempt(1, base)],
        }
        runtime.validate_ci_report(valid)
        url = "https://github.com/example/repo/actions/runs/"
        bad_field = [{**base[0], "link": url + "x" * (2049 - len(url))}]
        with self.assertRaisesRegex(ValueError, "link.*2048"):
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
                **base[0],
                "check_name": "check-{:03d}".format(index),
                "link": url + "x" * (2048 - len(url)),
            }
            for index in range(30)
        ]
        runtime.validate_ci_report({**valid, "attempts": [attempt(1, wide_attempt[:20])]})
        with self.assertRaisesRegex(ValueError, "attempt.*byte limit"):
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
                **base[0],
                "check_name": "check-{:03d}{}".format(index, "x" * 245),
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
        runtime.validate_ci_report({**oversized, "attempts": oversized["attempts"][:1]})
        with self.assertRaisesRegex(ValueError, "report.*byte limit"):
            runtime.validate_ci_report(oversized)


class GitHubDynamicWatchTest(GitRepositoryTest):
    def contract(
        self,
        *,
        head: str = "a" * 40,
        base: str = "b" * 40,
        require_ci: bool = True,
    ) -> dict[str, object]:
        return {
            "schema": "github-ci-contract-v1",
            "repository": "example/repo",
            "pull_number": 1,
            "head_oid": head,
            "base_oid": base,
            "require_ci": require_ci,
        }

    def _install_dynamic_watch(
        self,
        root: Path,
        watcher_path: Path,
        *,
        state: dict[str, object],
        checks: list[dict[str, object]],
        feedback: list[dict[str, object]] | None = None,
        threads: list[dict[str, object]] | None = None,
        archive: bool = False,
        changing_state: dict[str, object] | None = None,
    ) -> tuple[Path, dict[str, str]]:
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        watcher = scripts / watcher_path.name
        shutil.copyfile(watcher_path, watcher)
        watcher.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "github_observation.py",
            scripts / "workflow_github_observation.py",
        )
        (root / "state.json").write_text(json.dumps(state))
        (root / "checks.json").write_text(json.dumps(checks))
        (root / "feedback.json").write_text(
            json.dumps(feedback or [])
        )
        (root / "threads.json").write_text(json.dumps({
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": threads or [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}},
        }))
        (root / "pr-views").write_text("0")
        (root / "check-views").write_text("0")
        next_state = root / "next-state.json"
        if changing_state is not None:
            next_state.write_text(json.dumps(changing_state))
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            'if [ "$1 $2" = "pr view" ]; then\n'
            '  count=$(cat "$KENT_TEST_PR_VIEWS")\n'
            '  count=$((count + 1)); printf "%s" "$count" > "$KENT_TEST_PR_VIEWS"\n'
            '  if [ -f "$KENT_TEST_NEXT_STATE" ] && [ "$count" -ge 2 ]; then '
            'cat "$KENT_TEST_NEXT_STATE"; else cat "$KENT_TEST_STATE"; fi\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1 $2" = "pr checks" ]; then\n'
            '  count=$(cat "$KENT_TEST_CHECK_VIEWS")\n'
            '  count=$((count + 1)); printf "%s" "$count" > "$KENT_TEST_CHECK_VIEWS"\n'
            '  cat "$KENT_TEST_CHECKS"; exit 0\n'
            "fi\n"
            'case "$*" in\n'
            '  *"issues/"*"comments"*) printf \'[\\n\'; cat "$KENT_TEST_FEEDBACK"; printf \']\\n\' ;;\n'
            '  *"pulls/"*"reviews"*) printf \'[[]]\\n\' ;;\n'
            '  *"pulls/"*"comments"*) printf \'[[]]\\n\' ;;\n'
            '  *graphql*) cat "$KENT_TEST_THREADS" ;;\n'
            '  *) exit 2 ;;\n'
            "esac\n"
        )
        fake_gh.chmod(0o755)
        archive_bin = root / "archive-ci"
        if archive:
            archive_bin.write_text(
                "#!/bin/sh\n"
                'cat > "$KENT_ARCHIVE_INPUT"\n'
                'printf \'{"ci_report_artifact":"artifact"}\\n\'\n'
            )
            archive_bin.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": f"{root}:{os.environ.get('PATH', '')}",
            "KENT_GH_BIN": str(fake_gh),
            "KENT_TEST_STATE": str(root / "state.json"),
            "KENT_TEST_NEXT_STATE": str(next_state),
            "KENT_TEST_CHECKS": str(root / "checks.json"),
            "KENT_TEST_FEEDBACK": str(root / "feedback.json"),
            "KENT_TEST_THREADS": str(root / "threads.json"),
            "KENT_TEST_PR_VIEWS": str(root / "pr-views"),
            "KENT_TEST_CHECK_VIEWS": str(root / "check-views"),
            "KENT_CI_WATCH_TEST_MODE": "1",
            "KENT_CI_WATCH_INTERVAL_SECONDS": "0",
            "KENT_CI_WATCH_MAX_POLLS": "3",
            "KENT_CI_WATCH_MAX_ERRORS": "1",
            "KENT_CI_WATCH_QUERY_TIMEOUT_SECONDS": "5",
            "KENT_PR_WATCH_TEST_MODE": "1",
            "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
            "KENT_PR_WATCH_MAX_POLLS": "1",
            "KENT_PR_WATCH_MAX_ERRORS": "1",
            "KENT_PR_WATCH_QUERY_TIMEOUT_SECONDS": "5",
        }
        if archive:
            environment["KENT_PREPARE_GITHUB_CI_BIN"] = str(archive_bin)
            environment["KENT_ARCHIVE_INPUT"] = str(root / "archive-input.json")
        return watcher, environment

    def _run(
        self,
        watcher: Path,
        root: Path,
        environment: dict[str, str],
        payload: dict[str, object],
    ) -> dict[str, object]:
        result = self._run_raw(watcher, root, environment, payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _run_raw(
        self,
        watcher: Path,
        root: Path,
        environment: dict[str, str],
        payload: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(watcher)],
            cwd=root,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=20,
        )

    def _generated_spec(
        self,
        *,
        ci_monitoring: bool,
        writer_sessions: str | None = None,
        coder_selection: str | None = None,
        branch_identity: str | None = None,
        delivery_profile: str | None = None,
    ):
        from dataclasses import replace

        from workflowkit.delivery import build_delivery_workflow
        from workflowkit.profile import ProjectProfile

        source = (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text()
        profile = ProjectProfile.from_toml(
            REPO_ROOT,
            source,
            check_files=False,
        )
        capabilities = {
            **profile.capabilities,
            "ci_monitoring": ci_monitoring,
        }
        policies = dict(profile.policies)
        if writer_sessions is not None:
            policies["writer_sessions"] = writer_sessions
        if coder_selection is not None:
            policies["coder_selection"] = coder_selection
        commands = dict(profile.commands)
        if ci_monitoring:
            commands["wait_ci"] = ".kent/scripts/workflow-wait-github-ci"
        issue_tracker = profile.issue_tracker
        if branch_identity is not None:
            policies["branch_identity"] = branch_identity
            if branch_identity == "jira":
                issue_tracker = "jira"
            commands["branch_identity"] = ".kent/scripts/workflow-branch-identity"
        if delivery_profile is not None:
            profile = replace(profile, delivery_profile=delivery_profile)
        profile = replace(
            profile,
            capabilities=capabilities,
            policies=policies,
            commands=commands,
            issue_tracker=issue_tracker,
        )
        return build_delivery_workflow(profile, 1)

    def _assert_raw_edge_projection(
        self,
        raw_result: subprocess.CompletedProcess[str],
        *,
        spec,
        edge_key: str,
    ) -> dict[str, object]:
        return assert_raw_edge_projection(
            self,
            raw_result,
            spec=spec,
            edge_key=edge_key,
        )

    def _dynamic_report(self, contract: dict[str, object]) -> dict[str, object]:
        from workflowkit import runtime

        classification = runtime.classify_github_checks(
            [],
            require_ci=bool(contract["require_ci"]),
        )
        attempt = {
            "sequence": 1,
            "head_oid": contract["head_oid"],
            "base_oid": contract["base_oid"],
            "retry": None,
            "checks": classification["checks"],
            "check_count": classification["check_count"],
            "checks_sha256": classification["checks_sha256"],
            "reason": classification["state"],
            "watcher_exit_code": 0,
            "error": None,
        }
        return runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[attempt],
        )

    def _prepare_raw(
        self,
        root: Path,
        environment: dict[str, str],
        payload: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(REPO_ROOT / "scripts" / "prepare-github-ci")],
            cwd=root,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=20,
        )

    def test_prepare_ci_raw_stdout_projects_initial_retry_and_merged_edges(self) -> None:
        from tests.test_delivery_continuity import runtime_v2_profile
        from workflowkit.delivery import build_delivery_workflow
        from workflowkit import runtime

        spec = build_delivery_workflow(
            runtime_v2_profile(ci=True, source_ci=True),
            1,
        )
        url = "https://github.com/example/repo/pull/1"
        contract = self.contract()
        valid_report = self._dynamic_report(contract)

        for case in ("initial", "retry", "merged_first_read", "merged_second_read"):
            with self.subTest(case=case):
                root = self.create_repository()
                context = root / ".kent" / "context"
                context.mkdir()
                (context / "delivery.md").write_text("CI archive context\n")
                states = {
                    "initial": [self._state(), self._state()],
                    "retry": [self._state(), self._state()],
                    "merged_first_read": [self._state(state="MERGED")],
                    "merged_second_read": [
                        self._state(),
                        self._state(state="MERGED"),
                    ],
                }[case]
                state_path = root / "pr-states.json"
                state_path.write_text(json.dumps(states))
                count_path = root / "pr-view-count"
                count_path.write_text("0")
                fake_gh = root / "gh"
                fake_gh.write_text(
                    f"#!{sys.executable}\n"
                    "import json, pathlib, sys\n"
                    f"root = pathlib.Path({str(root)!r})\n"
                    "args = sys.argv[1:]\n"
                    "if args[:2] != ['pr', 'view']:\n"
                    "    raise SystemExit(2)\n"
                    "counter = root / 'pr-view-count'\n"
                    "count = int(counter.read_text()) + 1\n"
                    "counter.write_text(str(count))\n"
                    "states = json.loads((root / 'pr-states.json').read_text())\n"
                    "print(json.dumps(states[min(count - 1, len(states) - 1)]))\n"
                )
                fake_gh.chmod(0o755)
                payload: dict[str, object] = {
                    "workspace_path": str(root),
                    "pr_url": url,
                    "branch_name": "TASK-1",
                    "merge_strategy": "rebase",
                    "task_short_id": "TASK-1",
                    "pr_feedback_cursor": "uninitialized",
                }
                expected_edge = "ci_prepare_ready_initial"
                environment = {**os.environ, "KENT_GH_BIN": str(fake_gh)}
                if case == "retry":
                    payload["ci_contract"] = runtime.canonical_bytes(contract).decode()
                    payload["ci_report"] = runtime.canonical_bytes(valid_report).decode()
                    environment.update(EVIDENCE_FIXTURE_OWNER)
                    expected_edge = "ci_prepare_ready_retry"
                elif case.startswith("merged_"):
                    payload["ci_contract"] = runtime.canonical_bytes(contract).decode()
                    expected_edge = "ci_prepare_merged"

                raw = self._prepare_raw(root, environment, payload)
                output = self._assert_raw_edge_projection(
                    raw,
                    spec=spec,
                    edge_key=expected_edge,
                )
                if case == "retry":
                    self.assertEqual(
                        output["ci_report"],
                        payload["ci_report"],
                        "retry must preserve the original report packet bytes",
                    )
                if case.startswith("merged_"):
                    self.assertNotIn("task_short_id", output)
                    self.assertNotIn("merge_strategy", output)
                    self.assertEqual(
                        int(count_path.read_text()),
                        1 if case == "merged_first_read" else 2,
                    )

    def test_dynamic_watchers_raw_stdout_projects_state_changed_and_merged_edges(self) -> None:
        ci_spec = self._generated_spec(ci_monitoring=True)
        pr_spec = self._generated_spec(ci_monitoring=True)
        for source, spec, changed_edge, merged_edge in (
            (CI_WATCH, ci_spec, "ci_watch_state_changed", "ci_watch_merged"),
            (PR_WATCH, pr_spec, "merge_watch_state_changed", "merge_watch_cleanup"),
        ):
            for state_kind in ("changed", "merged"):
                with self.subTest(watcher=source.name, state=state_kind):
                    root = self.create_repository()
                    contract = self.contract()
                    state = (
                        self._state(merge_state="DIRTY")
                        if state_kind == "changed"
                        else self._state(state="MERGED")
                    )
                    watcher, environment = self._install_dynamic_watch(
                        root,
                        source,
                        state=state,
                        checks=[
                            self._check(
                                name="unit",
                                bucket="pass",
                                state="SUCCESS",
                            )
                        ],
                    )
                    payload = self._payload(
                        root,
                        contract,
                        report=self._dynamic_report(contract),
                    )
                    raw = self._run_raw(watcher, root, environment, payload)
                    edge_key = changed_edge if state_kind == "changed" else merged_edge
                    output = self._assert_raw_edge_projection(
                        raw,
                        spec=spec,
                        edge_key=edge_key,
                    )
                    if state_kind == "changed":
                        self.assertNotIn("observed_feedback_cursor", output)
                        self.assertEqual("pr_head_oid" in output, source == CI_WATCH)
                        self.assertEqual("pr_base_oid" in output, source == CI_WATCH)
                        report = json.loads(output["pr_report"])
                        self.assertEqual(report["reason"], "merge_conflict")
                    else:
                        self.assertIn("merge_report", output)
                        self.assertNotIn("pr_report", output)

    def test_dynamic_ci_invalid_check_classification_precedes_cursor_construction(self) -> None:
        root = self.create_repository()
        watcher, environment = self._install_dynamic_watch(
            root,
            CI_WATCH,
            state=self._state(),
            checks=[
                self._check(
                    name="unit",
                    bucket="fail",
                    state="SUCCESS",
                )
            ],
        )
        cursor_calls = root / "cursor-calls"
        runtime_sibling = (
            root / ".kent" / "scripts" / "workflow_runtime_contracts.py"
        )
        runtime_source = runtime_sibling.read_text()
        runtime_sibling.write_text(
            runtime_source
            + "\n_original_make_cursor = make_dynamic_pr_feedback_cursor\n"
            + "def make_dynamic_pr_feedback_cursor(*args, **kwargs):\n"
            + "    with open(os.environ['KENT_CURSOR_CALLS'], 'a') as output:\n"
            + "        output.write('called\\n')\n"
            + "    return _original_make_cursor(*args, **kwargs)\n"
        )
        environment["KENT_CURSOR_CALLS"] = str(cursor_calls)
        environment["KENT_CI_WATCH_MAX_ERRORS"] = "2"
        result = self._run_raw(
            watcher,
            root,
            environment,
            self._payload(root, self.contract()),
        )
        output = self._assert_raw_edge_projection(
            result,
            spec=self._generated_spec(ci_monitoring=True),
            edge_key="ci_watch_diagnose",
        )
        report = json.loads(output["ci_report"])
        self.assertEqual(report["attempts"][-1]["reason"], "invalid_observation")
        self.assertFalse(cursor_calls.exists())
        self.assertEqual((root / "pr-views").read_text(), "2")
        self.assertEqual((root / "check-views").read_text(), "1")

    def test_dynamic_ci_closed_changed_identity_never_appends_contradictory_attempt(self) -> None:
        root = self.create_repository()
        watcher, environment = self._install_dynamic_watch(
            root,
            CI_WATCH,
            state=self._state(state="CLOSED", head="d" * 40),
            checks=[],
        )
        environment["KENT_CI_WATCH_MAX_ERRORS"] = "2"
        result = self._run_raw(
            watcher,
            root,
            environment,
            self._payload(root, self.contract()),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("closed_pull_request_identity_changed", result.stderr)
        self.assertEqual((root / "pr-views").read_text(), "1")
        self.assertEqual((root / "check-views").read_text(), "0")

    def test_dynamic_watchers_fail_closed_on_foreign_head_branch_before_merge(self) -> None:
        ci_spec = self._generated_spec(ci_monitoring=True)
        pr_spec = self._generated_spec(ci_monitoring=True)
        for source, spec, edge_key in (
            (CI_WATCH, ci_spec, "ci_watch_diagnose"),
            (PR_WATCH, pr_spec, "merge_watch_state_changed"),
        ):
            for state_name in ("OPEN", "MERGED"):
                with self.subTest(watcher=source.name, state=state_name):
                    root = self.create_repository()
                    state = self._state(state=state_name)
                    state["headRefName"] = "FOREIGN-BRANCH"
                    watcher, environment = self._install_dynamic_watch(
                        root,
                        source,
                        state=state,
                        checks=[
                            self._check(
                                name="unit",
                                bucket="pass",
                                state="SUCCESS",
                            )
                        ],
                    )
                    environment[
                        "KENT_CI_WATCH_MAX_ERRORS"
                        if source == CI_WATCH
                        else "KENT_PR_WATCH_MAX_ERRORS"
                    ] = "1"
                    contract = self.contract()
                    raw = self._run_raw(
                        watcher,
                        root,
                        environment,
                        self._payload(
                            root,
                            contract,
                            report=self._dynamic_report(contract),
                        ),
                    )
                    output = self._assert_raw_edge_projection(
                        raw,
                        spec=spec,
                        edge_key=edge_key,
                    )
                    self.assertNotIn("merge_report", output)
                    if source == CI_WATCH:
                        self.assertEqual(
                            json.loads(output["ci_report"])["attempts"][-1]["reason"],
                            "invalid_observation",
                        )
                        self.assertEqual(
                            (root / "pr-views").read_text(),
                            "2" if state_name == "OPEN" else "1",
                        )
                    else:
                        report = json.loads(output["pr_report"])
                        self.assertEqual(report["reason"], "github_query_failed")
                        self.assertEqual(
                            report["safe_error"]["code"],
                            "invalid_observation",
                        )
                        self.assertNotIn("pr_head_oid", output)
                        self.assertNotIn("pr_base_oid", output)

    def test_dynamic_ci_archive_failure_does_not_repeat_observation_or_emit_result(self) -> None:
        from tests.test_delivery_continuity import runtime_v2_profile
        from workflowkit.delivery import build_delivery_workflow

        spec = build_delivery_workflow(
            runtime_v2_profile(ci=True, source_ci=True),
            1,
        )
        root = self.create_repository()
        watcher, environment = self._install_dynamic_watch(
            root,
            CI_WATCH,
            state=self._state(merge_state="DIRTY"),
            checks=[
                self._check(
                    name="unit",
                    bucket="pass",
                    state="SUCCESS",
                )
            ],
            archive=True,
        )
        archive_calls = root / "archive-calls"
        archive = Path(environment["KENT_PREPARE_GITHUB_CI_BIN"])
        archive.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            f"printf 'called\\n' >> {shlex.quote(str(archive_calls))}\n"
            "exit 2\n"
        )
        archive.chmod(0o755)
        environment["KENT_CI_WATCH_MAX_ERRORS"] = "2"
        result = self._run_raw(
            watcher,
            root,
            environment,
            self._payload(root, self.contract(), task="TASK-1"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("ci_report_archive_failed", result.stderr)
        self.assertEqual((root / "pr-views").read_text(), "2")
        self.assertEqual((root / "check-views").read_text(), "1")
        self.assertEqual(archive_calls.read_text().splitlines(), ["called"])
        # The valid raw result would use the generated changed-state edge,
        # which includes task identity only for the source-CI profile.
        edge = next(edge for edge in spec.edges if edge.key == "ci_watch_state_changed")
        self.assertIn("task_short_id", {parameter.key for parameter in edge.parameters})

    def test_dynamic_watcher_capture_failure_is_single_attempt_without_stdout(self) -> None:
        for source in (CI_WATCH, PR_WATCH):
            with self.subTest(watcher=source.name):
                root = self.create_repository()
                watcher, environment = self._install_dynamic_watch(
                    root,
                    source,
                    state=self._state(merge_state="DIRTY"),
                    checks=[
                        self._check(
                            name="unit",
                            bucket="pass",
                            state="SUCCESS",
                        )
                    ],
                )
                profile = root / ".kent" / "workflow-profile.toml"
                profile.write_text(
                    '[policies]\ndelivery_reliability = "v1"\n'
                )
                capture_calls = root / "capture-calls"
                checkpoint = root / ".kent" / "scripts" / "workflow-checkpoint"
                checkpoint.write_text(
                    "from pathlib import Path\n"
                    "def current_script_delivery_protocol(root, *, task, native_context):\n"
                    f"    return {{'protocol': 'legacy', 'target_required': False, "
                    f"'context_schema': 'workflow-delivery-context-v1', "
                    f"'node_key': {'ci_watch' if source == CI_WATCH else 'merge_watch'!r}, "
                    "'identity': {'task_id': 'native-task', 'task_short_id': 'TASK-1', "
                    "'project_id': 'native-project', 'workspace_path': str(root), "
                    "'invocation_kind': 'script', 'node_id': 'native-watcher', "
                    "'transition_branch_key': None}}\n"
                    "def require_github_provider_read(root, payload, identity):\n"
                    "    assert identity['workspace_path'] == str(root)\n"
                    "def is_github_provider_read_blocked(error):\n"
                    "    return False\n"
                    "def emit_producer_result(root, payload, result, producer_kind):\n"
                    "    path = Path(root) / 'capture-calls'\n"
                    "    with path.open('a') as output:\n"
                    "        output.write(producer_kind + '\\n')\n"
                    "    raise RuntimeError('injected capture failure')\n"
                )
                environment[
                    "KENT_CI_WATCH_MAX_ERRORS"
                    if source == CI_WATCH
                    else "KENT_PR_WATCH_MAX_ERRORS"
                ] = "2"
                raw = self._run_raw(
                    watcher,
                    root,
                    environment,
                    self._payload(root, self.contract()),
                )
                self.assertNotEqual(raw.returncode, 0)
                self.assertEqual(raw.stdout, "")
                self.assertIn("producer_capture_failed", raw.stderr)
                self.assertEqual(
                    capture_calls.read_text().splitlines(),
                    ["wait-ci" if source == CI_WATCH else "wait-pr"],
                )
                self.assertEqual((root / "pr-views").read_text(), "2")
                self.assertEqual((root / "check-views").read_text(), "1")

    def _payload(
        self,
        root: Path,
        contract: dict[str, object],
        *,
        cursor: object = "uninitialized",
        report: dict[str, object] | None = None,
        task: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "workspace_path": str(root),
            "pr_url": "https://github.com/example/repo/pull/1",
            "branch_name": "TASK-1",
            "merge_strategy": "rebase",
            "ci_contract": json.dumps(
                contract,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "pr_head_oid": contract["head_oid"],
            "pr_base_oid": contract["base_oid"],
            "pr_base_branch": "main",
            "pr_feedback_cursor": cursor,
        }
        if task is not None:
            payload["task_short_id"] = task
        if report is not None:
            payload["ci_report"] = json.dumps(
                report,
                sort_keys=True,
                separators=(",", ":"),
            )
        return payload

    def _state(
        self,
        *,
        head: str = "a" * 40,
        base: str = "b" * 40,
        state: str = "OPEN",
        merge_state: str = "CLEAN",
        review: str = "",
        rollup: object = [{}],
    ) -> dict[str, object]:
        return {
            "state": state,
            "mergedAt": None if state != "MERGED" else "2026-09-22T00:00:00Z",
            "mergeCommit": None if state != "MERGED" else {"oid": "c" * 40},
            "headRefName": "TASK-1",
            "headRefOid": head,
            "baseRefName": "main",
            "baseRefOid": base,
            "url": "https://github.com/example/repo/pull/1",
            "mergeable": "MERGEABLE" if merge_state == "CLEAN" else "CONFLICTING",
            "mergeStateStatus": merge_state,
            "reviewDecision": review,
            "statusCheckRollup": rollup,
        }

    def _check(
        self,
        *,
        name: str,
        bucket: str,
        state: str,
        workflow: str = "PR",
        event: str = "pull_request",
        link: str | None = "https://github.com/example/repo/actions/runs/1",
    ) -> dict[str, object]:
        return {
            "name": name,
            "workflow": workflow,
            "event": event,
            "bucket": bucket,
            "state": state,
            "link": link,
        }

    def test_dynamic_ci_uses_complete_checks_and_external_links(self) -> None:
        root = self.create_repository()
        contract = self.contract()
        state = self._state()
        checks = [
            self._check(
                name="Aikido",
                bucket="pass",
                state="SUCCESS",
                workflow="",
                event="",
                link="https://app.aikido.dev/repos/example",
            ),
            self._check(
                name="unexpected-lint",
                bucket="fail",
                state="FAILURE",
            ),
        ]
        watcher, environment = self._install_dynamic_watch(
            root,
            CI_WATCH,
            state=state,
            checks=checks,
        )
        result = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract),
        )
        self.assertEqual(result["transition"], "ci_watch_failed")
        report = json.loads(result["ci_report"])
        self.assertEqual(report["schema"], "github-ci-report-v3")
        self.assertEqual(report["attempts"][-1]["reason"], "failed")
        self.assertEqual(len(report["attempts"][-1]["checks"]), 2)
        self.assertNotIn("expected_ci_checks", result)
        self.assertNotIn("ci_policy_snapshot", result)

    def test_dynamic_ci_green_archives_v3(self) -> None:
        root = self.create_repository()
        contract = self.contract()
        watcher, environment = self._install_dynamic_watch(
            root,
            CI_WATCH,
            state=self._state(),
            checks=[
                self._check(
                    name="unit",
                    bucket="pass",
                    state="SUCCESS",
                )
            ],
            archive=True,
        )
        result = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract, task="TASK-1"),
        )
        self.assertEqual(result["transition"], "ci_watch_passed")
        report = json.loads(result["ci_report"])
        self.assertEqual(report["schema"], "github-ci-report-v3")
        self.assertTrue((root / "archive-input.json").exists())
        archived = json.loads((root / "archive-input.json").read_text())
        self.assertEqual(archived["operation"], "archive_ci_report")
        self.assertEqual(
            json.loads(archived["ci_report"])["schema"],
            "github-ci-report-v3",
        )

    def test_dynamic_ci_source_change_never_fabricates_or_rewrites_report(self) -> None:
        root = self.create_repository()
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "dynamic_ci_source_change_runtime",
        )
        contract = self.contract()
        row = self._check(name="unit", bucket="pass", state="SUCCESS")
        classified = runtime.classify_github_checks([{
            "workflow_name": row["workflow"],
            "check_name": row["name"],
            "event": row["event"],
            "bucket": row["bucket"],
            "state": row["state"],
            "link": row["link"],
        }], require_ci=True)
        attempt = {
            "sequence": 1,
            "head_oid": contract["head_oid"],
            "base_oid": contract["base_oid"],
            "retry": None,
            "checks": classified["checks"],
            "check_count": classified["check_count"],
            "checks_sha256": classified["checks_sha256"],
            "reason": classified["state"],
            "watcher_exit_code": None,
            "error": None,
        }
        report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[attempt],
        )
        watcher, environment = self._install_dynamic_watch(
            root,
            CI_WATCH,
            state=self._state(),
            changing_state=self._state(head="d" * 40),
            checks=[row],
        )
        for prior in (None, report):
            with self.subTest(prior_report=prior is not None):
                (root / "pr-views").write_text("0")
                result = self._run(
                    watcher, root, environment,
                    self._payload(root, contract, report=prior),
                )
                self.assertEqual(result["transition"], "ci_watch_source_changed")
                if prior is None:
                    self.assertNotIn("ci_report", result)
                else:
                    self.assertEqual(json.loads(result["ci_report"]), report)

    def test_dynamic_ci_invalid_query_is_a_precise_v3_failure(self) -> None:
        root = self.create_repository()
        contract = self.contract()
        watcher, environment = self._install_dynamic_watch(
            root,
            CI_WATCH,
            state=self._state(),
            checks=[],
        )
        failing = root / "gh-failing"
        failing.write_text(
            "#!/bin/sh\nprintf stdout >&1\nprintf stderr >&2\nexit 7\n"
        )
        failing.chmod(0o755)
        environment["KENT_GH_BIN"] = str(failing)
        result = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract),
        )
        self.assertEqual(result["transition"], "ci_watch_failed")
        attempt = json.loads(result["ci_report"])["attempts"][-1]
        self.assertEqual(attempt["reason"], "query_failed")
        self.assertEqual(attempt["error"]["code"], "query_failed")
        self.assertEqual(attempt["error"]["stdout_sha256"], hashlib.sha256(b"stdout").hexdigest())
        self.assertEqual(attempt["error"]["stderr_sha256"], hashlib.sha256(b"stderr").hexdigest())

    def test_dynamic_ci_pending_skipped_empty_and_invalid_checks(self):
        cases = (
            ("pending", "pending", "IN_PROGRESS", True, "ci_watch_still_waiting", "pending"),
            ("skipped", "skipping", "SKIPPED", True, "ci_watch_passed", "green"),
            ("cancelled", "cancel", "CANCELLED", True, "ci_watch_failed", "failed"),
            ("empty", None, None, True, "ci_watch_still_waiting", "no_checks"),
            ("optional", None, None, False, "ci_watch_passed", "not_required"),
            ("invalid", "pass", "FAILURE", True, "ci_watch_failed", "invalid_observation"),
        )
        for label, bucket, state, require_ci, transition, reason in cases:
            with self.subTest(label=label):
                root = self.create_repository()
                contract = self.contract(require_ci=require_ci)
                checks = [] if bucket is None else [self._check(name="unit", bucket=bucket, state=state)]
                watcher, environment = self._install_dynamic_watch(
                    root, CI_WATCH, state=self._state(rollup=[{}] if checks else []),
                    checks=checks,
                )
                result = self._run(watcher, root, environment, self._payload(root, contract))
                self.assertEqual(result["transition"], transition, result)
                report = json.loads(result["ci_report"])
                self.assertEqual(report["attempts"][-1]["reason"], reason)
                if reason in {"pending", "no_checks"}:
                    self.assertEqual([row["sequence"] for row in report["attempts"]], [1, 2, 3])

    def test_dynamic_ci_attempt_byte_boundary_on_initial_and_append_paths(self):
        from workflowkit import runtime

        watcher = load_template_module(CI_WATCH, "dynamic_attempt_byte_boundary")
        contract = self.contract()
        prefix = "https://github.com/example/repo/actions/runs/"
        rows = [{
            "workflow_name": "PR", "check_name": f"unit-{index:02}",
            "event": "pull_request", "bucket": "pass", "state": "SUCCESS",
            "link": prefix + "x" * (2048 - len(prefix)),
        } for index in range(30)]

        def attempt(checks, sequence):
            classified = runtime.classify_github_checks(checks, require_ci=True)
            self.assertEqual(classified["state"], "green")
            return {
                "sequence": sequence, "head_oid": contract["head_oid"],
                "base_oid": contract["base_oid"], "retry": None,
                "checks": classified["checks"], "check_count": classified["check_count"],
                "checks_sha256": classified["checks_sha256"], "reason": "green",
                "watcher_exit_code": None, "error": None,
            }

        limit = runtime.MAX_DYNAMIC_CI_ATTEMPT_BYTES
        for count in range(1, len(rows) + 1):
            selected = [dict(row) for row in rows[:count]]
            selected[-1]["link"] = prefix
            minimum = len(runtime.canonical_bytes(attempt(selected, 1)))
            if minimum <= limit < minimum + 2048 - len(prefix):
                break
        else:
            self.fail("valid link padding must straddle the attempt limit")
        selected[-1]["link"] += "x" * (limit - minimum)
        oversized = [dict(row) for row in selected]
        oversized[-1]["link"] += "x"
        self.assertEqual(len(runtime.canonical_bytes(attempt(selected, 1))), limit)
        self.assertEqual(len(runtime.canonical_bytes(attempt(oversized, 1))), limit + 1)
        prior = watcher._append(
            runtime, contract, None, head_oid=contract["head_oid"],
            base_oid=contract["base_oid"],
            classification=runtime.classify_github_checks([], require_ci=True),
        )
        for previous in (None, prior):
            with self.subTest(appending=previous is not None):
                kwargs = {
                    "head_oid": contract["head_oid"], "base_oid": contract["base_oid"],
                }
                report = watcher._append(
                    runtime, contract, previous, **kwargs,
                    classification=runtime.classify_github_checks(selected, require_ci=True),
                )
                self.assertEqual(report["attempts"][-1]["reason"], "green")
                self.assertEqual(report["attempts"][-1]["sequence"], 1 if previous is None else 2)
                runtime.validate_dynamic_ci_report(report)
                with self.assertRaisesRegex(ValueError, "attempt.*byte limit"):
                    watcher._append(
                        runtime, contract, previous, **kwargs,
                        classification=runtime.classify_github_checks(oversized, require_ci=True),
                    )

    def test_dynamic_pr_head_and_base_changes_are_independent_of_review(self):
        for key, reason in (
            ("head", "pull_request_head_changed"),
            ("base", "pull_request_base_changed"),
        ):
            with self.subTest(key=key):
                root = self.create_repository()
                contract = self.contract()
                watcher, environment = self._install_dynamic_watch(
                    root, PR_WATCH, state=self._state(**{key: "d" * 40}),
                    checks=[self._check(name="unit", bucket="pass", state="SUCCESS")],
                )
                result = self._run(watcher, root, environment, self._payload(root, contract))
                self.assertEqual(result["transition"], "merge_watch_state_changed")
                report = json.loads(result["pr_report"])
                self.assertEqual(report["reason"], reason)
                self.assertEqual(result["pr_feedback_cursor"], "uninitialized")
                self.assertEqual(report["headRefOid" if key == "head" else "baseRefOid"], "d" * 40)
                self.assertNotIn("pr_head_oid", result)
                self.assertNotIn("pr_base_oid", result)

    def test_dynamic_watchers_round_trip_unicode_feedback_cursor(self):
        for source in (CI_WATCH, PR_WATCH):
            with self.subTest(watcher=source.name):
                root = self.create_repository()
                thread = {
                    "id": "THREAD1", "isResolved": False, "isOutdated": False,
                    "path": "src/é.kt", "subjectType": "LINE",
                    "line": 12, "startLine": 10, "originalLine": 20,
                    "originalStartLine": 18,
                    "comments": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
                watcher, environment = self._install_dynamic_watch(
                    root, source, state=self._state(),
                    checks=[self._check(name="unit", bucket="pass", state="SUCCESS")],
                    threads=[thread],
                )
                contract = self.contract()
                changed = self._run(watcher, root, environment, self._payload(root, contract))
                report = json.loads(changed["pr_report"])
                cursor = report["observed_feedback_cursor"]
                self.assertIn("é", cursor)
                item = json.loads(cursor)["items"][0]
                self.assertEqual(item["path"], thread["path"])
                self.assertEqual(item["current_line"], 12)
                self.assertNotIn("observed_feedback_cursor", changed)
                acknowledged = self._run(
                    watcher, root, environment, self._payload(root, contract, cursor=cursor),
                )
                self.assertEqual(acknowledged["pr_feedback_cursor"], cursor)
                self.assertNotIn("observed_feedback_cursor", acknowledged)
                self.assertEqual(
                    acknowledged["transition"],
                    "ci_watch_passed" if source == CI_WATCH else "merge_watch_still_waiting",
                )

    def test_dynamic_watchers_fail_closed_before_query_on_invalid_carriers(self):
        for source, field in ((CI_WATCH, "ci_contract"), (PR_WATCH, "pr_feedback_cursor")):
            for value in ("{not-json", "[" * 101 + "0" + "]" * 101):
                with self.subTest(watcher=source.name, field=field, value=value[:20]):
                    root = self.create_repository()
                    watcher, environment = self._install_dynamic_watch(
                        root, source, state=self._state(), checks=[],
                    )
                    marker = root / "queried"
                    (root / "gh").write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
                    payload = self._payload(root, self.contract())
                    payload[field] = value
                    result = subprocess.run(
                        [str(watcher)], cwd=root, input=json.dumps(payload),
                        text=True, capture_output=True, env=environment,
                        check=False, timeout=5,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertFalse(marker.exists())

    def test_dynamic_watchers_terminate_real_query_groups(self):
        cases = {
            "timeout": "query_timeout",
            "closed_timeout": "query_timeout",
            "orphan_open": "query_timeout",
            "orphan_closed": "query_failed",
            "overflow": "observation_limit",
        }
        for source in (CI_WATCH, PR_WATCH):
            for case, code in cases.items():
                with self.subTest(watcher=source.name, case=case):
                    root = self.create_repository()
                    watcher, environment = self._install_dynamic_watch(
                        root, source, state=self._state(), checks=[],
                    )
                    environment["KENT_CI_WATCH_QUERY_TIMEOUT_SECONDS"] = "1"
                    environment["KENT_PR_WATCH_QUERY_TIMEOUT_SECONDS"] = "1"
                    pid_file = root / "query-pids"
                    (root / "gh").write_text(
                        f"#!{sys.executable}\n"
                        "import json, os, signal, time\nfrom pathlib import Path\n"
                        f"case = {case!r}\n"
                        "ready, notify = os.pipe()\n"
                        "child = os.fork()\n"
                        "if child == 0:\n"
                        "    os.close(ready)\n"
                        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                        "    if case in {'closed_timeout', 'orphan_closed'}:\n"
                        "        os.close(1); os.close(2)\n"
                        "    os.write(notify, b'R'); os.close(notify)\n"
                        "    time.sleep(30); os._exit(0)\n"
                        "os.close(notify)\n"
                        "assert os.read(ready, 1) == b'R'\n"
                        "os.close(ready)\n"
                        f"Path({str(pid_file)!r}).write_text(f'{{os.getpid()}} {{child}}')\n"
                        "if case.startswith('orphan'):\n"
                        "    print('{}', flush=True); os._exit(0)\n"
                        "if case == 'closed_timeout':\n"
                        "    os.close(1); os.close(2)\n"
                        "if case == 'overflow':\n"
                        "    os.write(1, b'x' * (4 * 1024 * 1024 + 1))\n"
                        "time.sleep(30)\n"
                    )
                    result = self._run(
                        watcher, root, environment, self._payload(root, self.contract()),
                    )
                    self.assertTrue(pid_file.exists(), result)
                    assert_pids_gone(self, pid_file)
                    if source == CI_WATCH:
                        self.assertEqual(result["transition"], "ci_watch_failed")
                        error = json.loads(result["ci_report"])["attempts"][-1]["error"]
                    else:
                        self.assertEqual(result["transition"], "merge_watch_state_changed")
                        report = json.loads(result["pr_report"])
                        self.assertEqual(report["reason"], "github_query_failed")
                        error = report["safe_error"]
                    self.assertEqual(error["code"], code, result)

    def test_dynamic_pr_failed_extra_check_wakes_with_complete_report(self):
        root = self.create_repository()
        watcher, environment = self._install_dynamic_watch(
            root, PR_WATCH, state=self._state(),
            checks=[
                self._check(name="unit", bucket="pass", state="SUCCESS"),
                self._check(name="extra", bucket="fail", state="FAILURE"),
            ],
        )
        result = self._run(watcher, root, environment, self._payload(root, self.contract()))
        self.assertEqual(result["transition"], "merge_watch_state_changed")
        report = json.loads(result["pr_report"])
        self.assertEqual(report["reason"], "checks_failed")
        self.assertEqual(len(report["checks"]), 2)
        self.assertEqual(next(row for row in report["checks"] if row["check_name"] == "extra")["bucket"], "fail")

    def test_dynamic_pr_preserves_ack_cursor_and_wakes_for_feedback_or_conflict(
        self,
    ) -> None:
        root = self.create_repository()
        contract = self.contract()
        cursor = "uninitialized"
        watcher, environment = self._install_dynamic_watch(
            root,
            PR_WATCH,
            state=self._state(),
            checks=[
                self._check(
                    name="unit",
                    bucket="pending",
                    state="IN_PROGRESS",
                )
            ],
            feedback=[],
        )
        baseline = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract, cursor=cursor),
        )
        self.assertEqual(baseline["transition"], "merge_watch_still_waiting")

        issue = {
            "id": "issue-1",
            "body": "please fix",
            "created_at": "2026-09-22T00:00:00Z",
            "updated_at": "2026-09-22T00:00:00Z",
            "user": {"login": "reviewer"},
        }
        (root / "feedback.json").write_text(json.dumps([issue]))
        changed = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract, cursor=cursor),
        )
        self.assertEqual(changed["transition"], "merge_watch_state_changed")
        self.assertEqual(changed["pr_feedback_cursor"], cursor)
        observed = json.loads(changed["pr_report"])["observed_feedback_cursor"]
        self.assertEqual(json.loads(observed)["item_count"], 1)
        self.assertIn("pull_request_feedback_changed", changed["pr_report"])
        self.assertNotIn("observed_feedback_cursor", changed)

        conflict = self._state(merge_state="DIRTY")
        (root / "state.json").write_text(json.dumps(conflict))
        conflict_result = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract, cursor=observed),
        )
        self.assertEqual(conflict_result["transition"], "merge_watch_state_changed")
        self.assertIn("merge_conflict", conflict_result["pr_report"])

    def test_dynamic_pr_merged_takes_precedence_over_feedback(self) -> None:
        root = self.create_repository()
        contract = self.contract()
        watcher, environment = self._install_dynamic_watch(
            root,
            PR_WATCH,
            state=self._state(state="MERGED"),
            checks=[],
            feedback=[{
                "id": "issue-1",
                "body": "late",
                "created_at": "2026-09-22T00:00:00Z",
                "updated_at": "2026-09-22T00:00:00Z",
                "user": {"login": "reviewer"},
            }],
        )
        result = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract),
        )
        self.assertEqual(result["transition"], "merge_watch_pr_merged")
        self.assertIn("merge_report", result)
        self.assertNotIn("pr_report", result)

    def test_dynamic_pr_ci_disabled_profile_has_no_fake_ci_contract(self) -> None:
        root = self.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            "[capabilities]\nci_monitoring = false\n"
        )
        contract = self.contract(require_ci=False)
        watcher, environment = self._install_dynamic_watch(
            root,
            PR_WATCH,
            state=self._state(),
            checks=[],
            feedback=[],
        )
        payload = self._payload(root, contract)
        payload.pop("ci_contract")
        payload.pop("ci_report", None)
        result = self._run(watcher, root, environment, payload)
        self.assertEqual(result["transition"], "merge_watch_still_waiting")
        self.assertNotIn("ci_contract", result)
        self.assertNotIn("ci_report", result)

    def test_dynamic_pr_does_not_repeat_acknowledged_changes_requested(self) -> None:
        root = self.create_repository()
        contract = self.contract()
        watcher, environment = self._install_dynamic_watch(
            root,
            PR_WATCH,
            state=self._state(
                review="CHANGES_REQUESTED",
                rollup=[{}],
            ),
            checks=[
                self._check(
                    name="unit",
                    bucket="pending",
                    state="IN_PROGRESS",
                )
            ],
            feedback=[],
        )
        first = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract),
        )
        self.assertEqual(first["transition"], "merge_watch_state_changed")
        observed = json.loads(first["pr_report"])["observed_feedback_cursor"]
        self.assertNotIn("observed_feedback_cursor", first)
        second = self._run(
            watcher,
            root,
            environment,
            self._payload(root, contract, cursor=observed),
        )
        self.assertEqual(second["transition"], "merge_watch_still_waiting")


class WorkflowJanitorTest(GitRepositoryTest):
    def setUp(self) -> None:
        self._git_shim_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._git_shim_temporary.cleanup)
        shim_directory = Path(self._git_shim_temporary.name)
        shim = shim_directory / "git"
        shim.write_text(
            f"""#!{sys.executable}
import os
from pathlib import Path
import subprocess
import sys

REAL_GIT = {REAL_GIT!r}
args = sys.argv[1:]
network_commands = {{"fetch", "ls-remote", "push"}}
blocked_network_commands = {{
    "clone", "pull", "submodule", "fetch-pack", "send-pack", "receive-pack",
}}
command_index = next(
    (index for index, value in enumerate(args)
     if value in network_commands | blocked_network_commands),
    None,
)
if command_index is None:
    os.execv(REAL_GIT, [REAL_GIT, *args])

command = args[command_index]
if command not in network_commands:
    print("fixture Git shim rejected an unsupported network command", file=sys.stderr)
    raise SystemExit(97)

directory = Path.cwd()
for index, value in enumerate(args[:-1]):
    if value == "-C":
        directory = Path(args[index + 1])
try:
    repository = subprocess.run(
        [REAL_GIT, "-C", str(directory), "rev-parse", "--show-toplevel"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.strip()
    fetch_url = subprocess.run(
        [REAL_GIT, "-C", repository, "remote", "get-url", "origin"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.strip()
    push_url = subprocess.run(
        [REAL_GIT, "-C", repository, "remote", "get-url", "--push", "origin"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.strip()
    bare_remote = subprocess.run(
        [REAL_GIT, "-C", repository, "config", "--get",
         "kent.test.githubBareRemote"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.strip()
except (OSError, subprocess.CalledProcessError) as error:
    print("fixture Git shim could not resolve its owned repository", file=sys.stderr)
    raise SystemExit(97) from error

github_origin = "https://github.com/example/repo.git"
bare_path = Path(bare_remote)
if (
    fetch_url != github_origin
    or push_url != github_origin
    or not bare_path.is_absolute()
    or not bare_path.is_dir()
):
    print("fixture Git shim rejected an unowned remote destination", file=sys.stderr)
    raise SystemExit(97)

destination_index = next(
    (index for index in range(command_index + 1, len(args))
     if not args[index].startswith("-")),
    None,
)
if destination_index is None or args[destination_index] not in {{
    "origin", github_origin,
}}:
    print("fixture Git shim rejected an unknown remote destination", file=sys.stderr)
    raise SystemExit(97)

bare_check = subprocess.run(
    [REAL_GIT, "--git-dir", str(bare_path), "rev-parse", "--is-bare-repository"],
    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
)
if bare_check.returncode != 0 or bare_check.stdout.strip() != "true":
    print("fixture Git shim rejected a non-bare destination", file=sys.stderr)
    raise SystemExit(97)

args[destination_index] = str(bare_path)
completed = subprocess.run([REAL_GIT, *args], check=False)
raise SystemExit(completed.returncode)
"""
        )
        shim.chmod(0o755)
        self._git_shim_path = shim
        self._git_path_patch = mock.patch.dict(
            os.environ,
            {"PATH": f"{shim_directory}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        self._git_path_patch.start()
        self.addCleanup(self._git_path_patch.stop)

    def set_fixture_github_origin(self, root: Path, remote: Path) -> None:
        self.run_git(
            root, "config", "kent.test.githubBareRemote", str(remote.resolve()),
        )
        self.run_git(
            root,
            "remote",
            "set-url",
            "origin",
            "https://github.com/example/repo.git",
        )

    def test_delete_result_native_and_legacy_contracts(self) -> None:
        module = load_template_module(JANITOR, "janitor_delete_contract")
        for outcome in ("DELETED", "RETAINED", "NOT_REQUESTED", "NOT_APPLICABLE"):
            with self.subTest(outcome=outcome):
                cleanup = {"kind": "WORKTREE_BRANCH_CLEANUP_OUTCOME_" + outcome}
                if outcome in ("DELETED", "RETAINED"):
                    cleanup["branchName"] = "TASK-1"
                self.assertEqual(module.deletion_result_kind({"cleanup": cleanup}), "completed")
        self.assertEqual(module.deletion_result_kind({"kind": "completed"}), "completed")
        self.assertEqual(module.deletion_result_kind({
            "kind": "scheduled", "scheduled": {"operation_id": "operation"},
        }), "scheduled")
        native = {"cleanup": {
            "kind": "WORKTREE_BRANCH_CLEANUP_OUTCOME_DELETED", "branchName": "TASK-1",
        }}
        invalid = [
            {}, {"success": native}, {"cleanup": None}, {"cleanup": {}},
            {"cleanup": {"kind": "WORKTREE_BRANCH_CLEANUP_OUTCOME_UNSPECIFIED"}},
            {"cleanup": {"kind": "WORKTREE_BRANCH_CLEANUP_OUTCOME_DELETED"}},
            {"cleanup": {**native["cleanup"], "branchName": 1}},
            {"cleanup": {**native["cleanup"], "diagnostic": []}},
            {"cleanup": {**native["cleanup"], "unknown": True}},
            {**native, "leftoverRoot": "/retained"},
            {**native, "leftoverRoot": None},
            {**native, "kind": "completed"},
            {"kind": "completed", "scheduled": {}},
            {"kind": "scheduled", "completed": {}},
            {"kind": "completed", "completed": None},
            {"kind": "completed", "completed": {"leftover_root": "/retained"}},
            {"kind": "scheduled", "scheduled": []},
            {"kind": "unknown"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises((TypeError, ValueError)):
                    module.deletion_result_kind(payload)

    def test_native_delete_success_reaches_done_after_actual_deletion(self) -> None:
        original = self.retained_kent_record_wrapper
        counters = []

        def native_wrapper(root: Path) -> Path:
            wrapper = original(root)
            counter = root / "delete-count"
            counters.append(counter)
            text = wrapper.read_text()
            text = text.replace(
                'git -C "$KENT_TEST_PRIMARY" worktree remove --force "$target"\n',
                'git -C "$KENT_TEST_PRIMARY" worktree remove --force "$target"\n'
                'git -C "$KENT_TEST_PRIMARY" branch -D TASK-1 >/dev/null\n'
                f"echo deleted >> '{counter}'\n",
            )
            text = text.replace(
                '{"kind":"completed","completed":{"cleanup":{"kind":"retained"}}}',
                '{"cleanup":{"kind":"WORKTREE_BRANCH_CLEANUP_OUTCOME_DELETED",'
                '"branchName":"TASK-1"}}',
            )
            wrapper.write_text(text)
            return wrapper

        with mock.patch.object(self, "retained_kent_record_wrapper", native_wrapper):
            self.test_no_pr_cleanup_settles_local_branch_and_reports_retained_kent_record()
        self.assertEqual(counters[0].read_text().splitlines(), ["deleted"])

    def test_managed_retirement_blocks_foreign_runtime_sibling_before_native_delete(
        self,
    ) -> None:
        for with_v2 in (True, False):
            with self.subTest(with_v2=with_v2):
                root, worktree, scripts, marker = self.make_managed_worktree(
                    with_v2=with_v2,
                )
                runtime = worktree / ".kent" / "runtime"
                foreign = runtime / "foreign-owner"
                foreign.mkdir(parents=True)
                foreign_file = foreign / "opaque-state"
                foreign_bytes = b"foreign runtime evidence\n"
                foreign_file.write_bytes(foreign_bytes)
                worktree_stat = worktree.stat()
                worktree_identity = (worktree_stat.st_dev, worktree_stat.st_ino)
                branch_oid = self.run_git(
                    root, "rev-parse", "refs/heads/TASK-1",
                ).stdout.strip()
                native_delete_called = root / "native-delete-called"
                wrapper = self.completed_wrapper(root)
                environment = {
                    **os.environ,
                    "KENT_WORKTREE_WRAPPER": str(wrapper),
                    "KENT_TEST_PRIMARY": str(root),
                    "KENT_TEST_WRAPPER_MARKER": str(native_delete_called),
                }
                cleanup_report = marker if with_v2 else "Cleanup preflight passed."

                result = subprocess.run(
                    [str(scripts / "workflow-task-janitor")],
                    cwd=worktree,
                    input=self.janitor_input(
                        worktree,
                        branch_name="TASK-1",
                        cleanup_mode="no_pr",
                        cleanup_report=cleanup_report,
                    ),
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertFalse(
                    native_delete_called.exists(),
                    "foreign runtime sibling reached native worktree deletion",
                )
                self.assertEqual(
                    payload["transition"],
                    "task_janitor_blocked",
                    payload,
                )
                self.assertIn("unknown_runtime_entry", payload["cleanup_report"])
                self.assertIn("owner", payload["blocker_reason"].lower())
                self.assertTrue(worktree.is_dir())
                current = worktree.stat()
                self.assertEqual(
                    (current.st_dev, current.st_ino),
                    worktree_identity,
                )
                self.assertEqual(foreign_file.read_bytes(), foreign_bytes)
                self.assertIn(
                    str(worktree),
                    self.run_git(root, "worktree", "list", "--porcelain").stdout,
                )
                self.assertEqual(
                    self.run_git(
                        root, "rev-parse", "refs/heads/TASK-1",
                    ).stdout.strip(),
                    branch_oid,
                )

    def test_managed_retirement_rechecks_parent_after_terminal_preparation(self) -> None:
        root, worktree, scripts, marker = self.make_managed_worktree(with_v2=True)
        wrapper = self.completed_wrapper(root)
        environment = {
            **os.environ,
            "KENT_WORKTREE_WRAPPER": str(wrapper),
            "KENT_TEST_PRIMARY": str(root),
        }
        branch_oid = self.run_git(
            root, "rev-parse", "refs/heads/TASK-1",
        ).stdout.strip()
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_parent_recheck_after_prepare",
        )
        original_prepare = janitor._prepare_v2_managed_runtime_state
        delete_calls = []
        foreign_bytes = b"arrived after evidence preparation\n"
        foreign_file = (
            worktree / ".kent" / "runtime" / "late-foreign-owner" / "opaque-state"
        )

        def prepare_then_inject(
            workspace,
            task_short_id,
            *,
            cleanup_report,
            **prepare_options,
        ):
            result = original_prepare(
                workspace,
                task_short_id,
                cleanup_report=cleanup_report,
                **prepare_options,
            )
            foreign_file.parent.mkdir()
            foreign_file.write_bytes(foreign_bytes)
            return result

        def refuse_native_delete(*args, **kwargs):
            delete_calls.append((args, kwargs))
            return False, "injected native refusal", "native refused"

        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(
                janitor,
                "_prepare_v2_managed_runtime_state",
                side_effect=prepare_then_inject,
            ),
            mock.patch.object(
                janitor,
                "delete_managed_worktree",
                side_effect=refuse_native_delete,
            ),
            mock.patch.object(
                sys,
                "stdin",
                io.StringIO(
                    self.janitor_input(
                        worktree,
                        branch_name="TASK-1",
                        cleanup_mode="no_pr",
                        cleanup_report=marker,
                    )
                ),
            ),
            mock.patch.object(sys, "stdout", output),
        ):
            exit_code = janitor.main()

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            delete_calls,
            [],
            "runtime-parent drift reached native worktree deletion",
        )
        self.assertEqual(payload["transition"], "task_janitor_blocked", payload)
        self.assertTrue(worktree.is_dir())
        self.assertEqual(foreign_file.read_bytes(), foreign_bytes)
        self.assertIn(
            str(worktree),
            self.run_git(root, "worktree", "list", "--porcelain").stdout,
        )
        self.assertEqual(
            self.run_git(root, "rev-parse", "refs/heads/TASK-1").stdout.strip(),
            branch_oid,
        )

    def test_managed_runtime_parent_admission_fails_closed_for_unsafe_state(self) -> None:
        for case in ("foreign_symlink", "wrong_type", "over_limit",
                     "read_failure", "directory_identity_drift"):
            with self.subTest(case=case):
                root, worktree, scripts, marker = self.make_managed_worktree(with_v2=True)
                runtime = worktree / ".kent" / "runtime"
                janitor = load_template_module(
                    scripts / "workflow-task-janitor",
                    f"janitor_runtime_parent_{case}",
                )
                outside = root / "foreign-runtime-state"
                outside_bytes = b"preserve external runtime bytes\n"
                outside.write_bytes(outside_bytes)
                preserved_runtime = runtime.with_name("runtime-preserved")
                preserved_active = root / "preserved-task-runtime"
                preserved_active_snapshot: dict[str, bytes] = {}

                def snapshot_runtime_parent(directory: Path) -> dict[str, object]:
                    snapshot = {}
                    for item in directory.iterdir():
                        if item.is_symlink():
                            value = ("symlink", os.readlink(item))
                        elif item.is_dir():
                            value = (
                                "directory",
                                {
                                    nested.name: nested.read_bytes()
                                    for nested in item.iterdir()
                                },
                            )
                        elif item.is_file():
                            value = ("file", item.read_bytes())
                        else:
                            value = ("other", item.lstat().st_mode)
                        snapshot[item.name] = value
                    return snapshot

                if case == "foreign_symlink":
                    (runtime / "foreign-link").symlink_to(outside)
                elif case == "wrong_type":
                    active = runtime / "TASK-1"
                    preserved_active_snapshot = {
                        item.name: item.read_bytes() for item in active.iterdir()
                    }
                    active.rename(preserved_active)
                    active.write_bytes(b"wrong type at task name\n")
                elif case == "over_limit":
                    for index in range(janitor.MAX_RUNTIME_ENTRIES + 1):
                        (runtime / f"foreign-{index}").write_bytes(
                            f"state-{index}\n".encode()
                        )

                entry_snapshot = snapshot_runtime_parent(runtime)

                initial_runtime_identity = (
                    runtime.stat().st_dev,
                    runtime.stat().st_ino,
                )
                initial_worktree_stat = worktree.stat()
                initial_worktree_identity = (
                    initial_worktree_stat.st_dev,
                    initial_worktree_stat.st_ino,
                )
                branch_oid = self.run_git(
                    root, "rev-parse", "refs/heads/TASK-1",
                ).stdout.strip()
                native_delete_called = root / "native-delete-called"

                if case == "read_failure":
                    original_scandir = janitor.os.scandir
                    runtime_stat = runtime.stat()

                    def fail_runtime_inventory(path):
                        if isinstance(path, int):
                            descriptor_stat = os.fstat(path)
                            if (
                                descriptor_stat.st_dev == runtime_stat.st_dev
                                and descriptor_stat.st_ino == runtime_stat.st_ino
                            ):
                                raise PermissionError(
                                    "injected runtime inventory read failure"
                                )
                        return original_scandir(path)

                    with mock.patch.object(
                        janitor.os,
                        "scandir",
                        side_effect=fail_runtime_inventory,
                    ):
                        result_code, payload = self.run_managed_janitor_module(
                            janitor,
                            root,
                            worktree,
                            cleanup_report=marker,
                            native_delete_called=native_delete_called,
                        )
                elif case == "directory_identity_drift":
                    original_open = janitor.open_child_directory
                    replaced = False

                    def replace_runtime_after_open(parent_fd, name):
                        nonlocal replaced
                        descriptor = original_open(parent_fd, name)
                        if name == "runtime" and not replaced:
                            replaced = True
                            runtime.rename(preserved_runtime)
                            runtime.mkdir(mode=0o700)
                        return descriptor

                    with mock.patch.object(
                        janitor,
                        "open_child_directory",
                        side_effect=replace_runtime_after_open,
                    ):
                        result_code, payload = self.run_managed_janitor_module(
                            janitor,
                            root,
                            worktree,
                            cleanup_report=marker,
                            native_delete_called=native_delete_called,
                        )
                else:
                    result_code, payload = self.run_managed_janitor_module(
                        janitor,
                        root,
                        worktree,
                        cleanup_report=marker,
                        native_delete_called=native_delete_called,
                    )

                self.assertEqual(result_code, 0, payload)
                self.assertEqual(payload["transition"], "task_janitor_blocked", payload)
                expected_failure_code = {
                    "foreign_symlink": "unknown_runtime_entry",
                    "wrong_type": "unsafe_runtime_entry",
                    "over_limit": "inventory_limit",
                    "read_failure": "io_failure",
                    "directory_identity_drift": "unsafe_runtime_entry",
                }[case]
                self.assertIn(expected_failure_code, payload["cleanup_report"])
                self.assertIn("owner", payload["blocker_reason"].lower())
                self.assertFalse(
                    native_delete_called.exists(),
                    "unsafe runtime-parent state reached native deletion",
                )
                self.assertTrue(worktree.is_dir())
                current_worktree_stat = worktree.stat()
                self.assertEqual(
                    (current_worktree_stat.st_dev, current_worktree_stat.st_ino),
                    initial_worktree_identity,
                )
                self.assertIn(
                    str(worktree),
                    self.run_git(root, "worktree", "list", "--porcelain").stdout,
                )
                self.assertEqual(
                    self.run_git(
                        root, "rev-parse", "refs/heads/TASK-1",
                    ).stdout.strip(),
                    branch_oid,
                )
                self.assertEqual(outside.read_bytes(), outside_bytes)
                if case == "foreign_symlink":
                    self.assertTrue((runtime / "foreign-link").is_symlink())
                    self.assertTrue(os.path.samefile(runtime / "foreign-link", outside))
                elif case == "directory_identity_drift":
                    self.assertTrue(replaced)
                    self.assertTrue(preserved_runtime.is_dir())
                    self.assertEqual(
                        snapshot_runtime_parent(preserved_runtime),
                        entry_snapshot,
                    )
                    replacement_stat = runtime.stat()
                    self.assertNotEqual(
                        (replacement_stat.st_dev, replacement_stat.st_ino),
                        initial_runtime_identity,
                    )
                else:
                    self.assertEqual(
                        snapshot_runtime_parent(runtime),
                        entry_snapshot,
                    )
                if case == "wrong_type":
                    self.assertTrue(preserved_active.is_dir())
                    self.assertEqual(
                        {
                            item.name: item.read_bytes()
                            for item in preserved_active.iterdir()
                        },
                        preserved_active_snapshot,
                    )

    def test_native_kent_topology_and_primary_identity(self) -> None:
        module = load_template_module(JANITOR, "janitor_native_topology")
        primary = self.create_repository()
        workspace = (primary / ".kent/worktrees/TASK-1").resolve()
        entries = [
            {"topology": {"mainWorkspace": {"git": {
                "canonicalRoot": str(primary), "isMainWorktree": True,
            }}}},
            {"topology": {"registered": {
                "git": {"canonicalRoot": str(workspace)},
                "kent": {"canonicalRoot": str(workspace),
                         "worktreeId": "kent-record-task-1", "managed": True},
            }}},
        ]
        result = subprocess.CompletedProcess([], 0, json.dumps({"worktrees": entries}), "")
        with mock.patch.object(module, "run", return_value=result):
            record = module.capture_kent_worktree_record(
                "wrapper", primary, workspace, "cleanup-session",
            )
        self.assertEqual(record, {
            "variant": "registered", "root": str(workspace.resolve()),
            "worktree_id": "kent-record-task-1",
        })

    def test_native_kent_session_status_membership(self) -> None:
        module = load_template_module(JANITOR, "janitor_native_status")
        primary = self.create_repository()
        workspace = primary / ".kent/worktrees/TASK-1"
        for root, effective, expected in (
            (primary, primary, False),
            (workspace, workspace, True),
            (workspace, workspace / "subdirectory", True),
        ):
            with self.subTest(root=root, effective=effective):
                payload = {
                    "target": {"effectiveWorkdir": str(effective)},
                    "worktree": {"recordedRoot": str(root), "observedRoot": str(root)},
                }
                result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
                with mock.patch.object(module, "run", return_value=result):
                    self.assertEqual(module.cleanup_session_uses_workspace(
                        "wrapper", primary, "cleanup-session", workspace,
                    ), expected)
        for payload in (
            {},
            {"target": {"effectiveWorkdir": str(primary),
                        "EffectiveWorkdir": str(workspace)}},
            {"target": {"effectiveWorkdir": str(primary)},
             "worktree": {"recordedRoot": str(workspace),
                          "observedRoot": str(primary)}},
        ):
            with self.subTest(invalid=payload):
                result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
                with mock.patch.object(module, "run", return_value=result):
                    with self.assertRaises(RuntimeError):
                        module.cleanup_session_uses_workspace(
                            "wrapper", primary, "cleanup-session", workspace,
                        )

    def test_kent_duplicate_json_identity_is_rejected(self) -> None:
        module = load_template_module(JANITOR, "janitor_duplicate_json")
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            module.json_object('{"worktrees":[],"worktrees":[]}', "Kent list")

    def test_native_kent_rejects_ambiguous_and_unsafe_records(self) -> None:
        module = load_template_module(JANITOR, "janitor_native_rejections")
        primary = self.create_repository()
        workspace = primary / ".kent/worktrees/TASK-1"
        git = {"canonicalRoot": str(workspace)}
        kent = {"canonicalRoot": str(workspace),
                "worktreeId": "kent-record-task-1", "managed": True}
        registered = {"git": git, "kent": kent}
        invalid = [
            {},
            {"futureVariant": registered},
            {"registered": registered, "missing": {"kent": kent}},
            {"variant": "registered", "registered": registered},
            {"registered": {"git": {**git, "canonical_root": str(workspace)},
                            "kent": kent}},
            {"registered": {"git": {"canonicalRoot": str(primary)}, "kent": kent}},
            {"registered": {"git": {**git, "isMainWorktree": True}, "kent": kent}},
            {"registered": {"git": git, "kent": {**kent, "managed": False}}},
            {"registered": {"git": git, "kent": {**kent, "worktreeId": ""}}},
            {"mainWorkspace": {"git": {**git, "isMainWorktree": True}}},
            {"mainWorkspace": {"git": {"canonicalRoot": str(primary),
                                      "isMainWorktree": False}}},
            {"external": {"git": {"canonicalRoot": "relative/path"}}},
        ]
        for topology in invalid:
            with self.subTest(topology=topology):
                with self.assertRaises(RuntimeError):
                    module._kent_record(
                        {"topology": topology}, workspace=workspace, primary=primary,
                    )
        other = primary / ".kent/worktrees/OTHER"
        payload = {"worktrees": [
            {"topology": {"registered": registered}},
            {"topology": {"registered": {
                "git": {"canonicalRoot": str(other)},
                "kent": {**kent, "canonicalRoot": str(other)},
            }}},
        ]}
        result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(module, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "duplicate managed identities"):
                module.capture_kent_worktree_record(
                    "wrapper", primary, workspace.resolve(), "cleanup-session",
                )

    def _write_valid_runtime_file(
        self,
        path: Path,
        content: str | bytes = b"",
    ) -> None:
        payload = content.encode() if isinstance(content, str) else content
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
        finally:
            os.close(descriptor)

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
        environment = {**os.environ, **EVIDENCE_FIXTURE_OWNER}
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
            env=environment,
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
            env=environment,
        )
        self.assertEqual(seal.returncode, 0, seal.stderr)
        return scripts, json.loads(seal.stdout)["terminal_marker"]

    def make_ci_terminal_state(
        self, *, count=2, mutate_records=None, report_size=None, linked=False,
        report_kind="v2",
    ):
        """Typed archive fixture -> actual ledger append/seal -> Janitor."""
        from workflowkit import runtime
        from tests.test_revision import schema4_profile_contents
        from tests.test_runtime_contracts import ci_report

        root = self.create_repository()
        context = root / ".kent/context"
        context.mkdir(parents=True)
        (context / "delivery.md").write_text("CI delivery fixture\n")
        (root / ".kent/workflow-profile.toml").write_text(schema4_profile_contents())
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-q", "-m", "CI archive context")
        if linked:
            linked_root = root / "linked-worktree"
            self.run_git(root, "worktree", "add", "-q", "-b", "ci-linked", str(linked_root))
            root = linked_root
        head = self.run_git(root, "rev-parse", "HEAD").stdout.strip()
        scripts = self.install_v2_runtime_commands(root)
        environment = {**os.environ, **EVIDENCE_FIXTURE_OWNER}
        environment.pop("KENT_RUN_ID", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        artifacts = []
        with mock.patch.dict(os.environ, environment, clear=True):
            for index in range(count):
                # Each genuine CI event is a distinct Step, even without Run.
                environment["KENT_STEP_ID"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ci-event-{index}"))
                os.environ["KENT_STEP_ID"] = environment["KENT_STEP_ID"]
                if report_kind == "v3":
                    contract = {
                        "schema": "github-ci-contract-v1",
                        "repository": "owner/repository",
                        "pull_number": index + 1,
                        "head_oid": head,
                        "base_oid": head,
                        "require_ci": True,
                    }
                    check = {
                        "workflow_name": "CI",
                        "check_name": "unit",
                        "event": "pull_request",
                        "bucket": "fail" if index % 2 else "pass",
                        "state": "FAILURE" if index % 2 else "SUCCESS",
                        "link": "https://github.com/owner/repository/actions/runs/1",
                    }
                    classification = runtime.classify_github_checks(
                        [check],
                        require_ci=True,
                    )
                    report = runtime.build_dynamic_ci_report(
                        contract=contract,
                        attempts=[{
                            "sequence": 1,
                            "head_oid": head,
                            "base_oid": head,
                            "retry": None,
                            "checks": classification["checks"],
                            "check_count": classification["check_count"],
                            "checks_sha256": classification["checks_sha256"],
                            "reason": classification["state"],
                            "watcher_exit_code": 1 if index % 2 else 0,
                            "error": None,
                        }],
                    )
                else:
                    report = ci_report()
                    report["pull_number"] = index + 1
                    report["attempts"][0]["head_oid"] = head
                    if index % 2:
                        attempt = report["attempts"][0]
                        attempt["reason"] = "expected_check_failed"
                        attempt["watcher_exit_code"] = 1
                        attempt["expected_checks"][0].update(bucket="fail", state="FAILURE")
                if report_size is not None and report_kind == "v2":
                    attempt = report["attempts"][0]
                    report["attempts"] = [
                        {
                            **attempt, "sequence": sequence,
                            "expected_checks": [
                                {**attempt["expected_checks"][0],
                                 "check_name": f"check-{index:02d}",
                                 "link": "https://github.com/owner/repository/actions/runs/"}
                                for index in range(15)
                            ],
                        }
                        for sequence in (1, 2)
                    ]
                    for attempt in report["attempts"]:
                        for check in attempt["expected_checks"]:
                            gap = report_size - len(runtime.canonical_bytes(report))
                            self.assertGreaterEqual(gap, 0)
                            check["link"] += "x" * min(gap, 2048 - len(check["link"]))
                    self.assertEqual(len(runtime.canonical_bytes(report)), report_size)
                raw = runtime.canonical_bytes(report)
                artifact_name = "ci-report-{}.json".format(
                    hashlib.sha256(raw).hexdigest()
                )
                task_runtime = root / ".kent/runtime/TASK-1"
                task_runtime.mkdir(parents=True, exist_ok=True)
                self._write_valid_runtime_file(task_runtime / artifact_name, raw)
                artifact = str(
                    (task_runtime / artifact_name).relative_to(root)
                )
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
                            "node_key": "ci_prepare",
                            "evidence_type": "ci_report",
                            "summary": "CI archive janitor fixture.",
                            "artifacts": [artifact],
                            "checks": ["typed CI report fixture"],
                            "context": {
                                "manifest_path": ".kent/context/delivery.md",
                                "files_read": [],
                                "model_calls": 0,
                                "compaction_count": 0,
                            },
                        }
                    ),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    check=False,
                )
                self.assertEqual(append.returncode, 0, append.stderr)
                artifacts.append(artifact)
        ledger = root / ".kent/runtime/TASK-1/evidence-ledger.jsonl"
        records = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(len(records), count)
        self.assertEqual([row["artifacts"][0] for row in records], artifacts)
        if mutate_records is not None:
            mutate_records(records)
            previous = ""
            for index, record in enumerate(records, 1):
                record.pop("event_hash", None)
                record.update(sequence=index, previous_hash=previous)
                record["event_hash"] = runtime.canonical_sha256(record)
                previous = record["event_hash"]
            ledger.write_bytes(b"".join(runtime.canonical_bytes(row) + b"\n" for row in records))
        seal = subprocess.run(
            [str(scripts / "workflow-evidence-ledger"), "seal",
             "--task", "TASK-1", "--workspace", str(root)],
            input=json.dumps({
                "schema": "terminal-evidence-seal-request-v1",
                "operation_report_digests": [],
                "redaction": {"status": "passed", "report_sha256": "a" * 64},
                "retention_class": "cleanup_report_only",
            }),
            text=True, capture_output=True, env=environment, timeout=30,
        )
        self.assertEqual(seal.returncode, 0, seal.stderr)
        marker_line = json.loads(seal.stdout)["terminal_marker"]
        marker = runtime.validate_cleanup_report(marker_line)
        active = ledger.parent
        tombstone = active.parent / (".evidence-cleanup-" + runtime.canonical_sha256(marker))
        janitor = load_template_module(scripts / "workflow-task-janitor", "ci_janitor_test")
        return root, janitor, marker_line, active, tombstone, [Path(p).name for p in artifacts]

    def ci_cleanup(self, fixture, *, managed=False, hook=None):
        root, janitor, marker, _, _, _ = fixture
        operation = (
            janitor._prepare_v2_managed_runtime_state if managed
            else janitor._remove_v2_runtime_state
        )
        return operation(root, "TASK-1", cleanup_report=marker, _phase_hook=hook)

    def test_ci_archive_ledger_seal_primary_and_managed_vertical(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                fixture = self.make_ci_terminal_state(
                    mutate_records=lambda rows: rows[0]["artifacts"].extend(rows[0]["artifacts"] * 2),
                )
                _, _, _, active, tombstone, names = fixture
                for name in ("fix-checkpoint.json", "smoke-checkpoint.json"):
                    self._write_valid_runtime_file(active / name, "{}\n")
                before = {p.name: p.read_bytes() for p in active.iterdir()}
                phases = []
                result = self.ci_cleanup(fixture, managed=managed, hook=phases.append)
                self.assertTrue(result[0], result)
                self.assertFalse(active.exists())
                if managed:
                    self.assertEqual({p.name: p.read_bytes() for p in tombstone.iterdir()}, before)
                    repeated = self.ci_cleanup(fixture, managed=True)
                    self.assertTrue(repeated[0], repeated)
                    self.assertEqual({p.name: p.read_bytes() for p in tombstone.iterdir()}, before)
                else:
                    self.assertFalse(tombstone.exists())
                    self.assertLess(phases.index("after_smoke_checkpoint_unlink_fsync"),
                                    phases.index("before_ci_report_unlink"))
                    self.assertEqual(phases.count("after_ci_report_unlink_fsync"), len(names))
                    self.assertLess(max(i for i, phase in enumerate(phases)
                                        if phase == "after_ci_report_unlink_fsync"),
                                    phases.index("before_ledger_unlink"))

    def test_ci_v3_archive_dispatch_and_resume(self):
        from workflowkit import runtime

        for managed in (False, True):
            with self.subTest(managed=managed):
                fixture = self.make_ci_terminal_state(
                    count=1,
                    report_kind="v3",
                )
                result = self.ci_cleanup(fixture, managed=managed)
                self.assertTrue(result[0], result)
                if managed:
                    self.assertTrue(fixture[4].exists())
                    report = next(fixture[4].glob("ci-report-*.json"))
                    runtime.validate_ci_archive(
                        runtime.parse_canonical_json(
                            report.read_bytes(),
                            label="v3 janitor fixture",
                        )
                    )
                else:
                    self.assertFalse(fixture[4].exists())

        fixture = self.make_ci_terminal_state(
            count=1,
            report_kind="v3",
        )
        root, janitor, marker, active, tombstone, names = fixture
        active.rename(tombstone)
        sentinel = tombstone.parent / janitor.runtime_state_names("TASK-1")[1]
        self._write_valid_runtime_file(sentinel)
        result = self.ci_cleanup(fixture)
        self.assertTrue(result[0], result)
        self.assertFalse(tombstone.exists())

    def test_ci_v3_archive_rejects_malformed_unknown_tampered_and_unreferenced(self):
        from workflowkit import runtime

        for case in ("malformed", "unknown", "tampered", "unreferenced"):
            with self.subTest(case=case):
                fixture = self.make_ci_terminal_state(
                    count=1,
                    report_kind="v3",
                )
                root, _, _, active, tombstone, names = fixture
                report = active / names[0]
                raw = report.read_bytes()
                if case == "malformed":
                    report.write_bytes(b"not-json")
                elif case == "unknown":
                    report.write_bytes(
                        runtime.canonical_bytes(
                            {"schema": "github-ci-report-v4"}
                        )
                    )
                elif case == "tampered":
                    report.write_bytes(raw + b"\n")
                else:
                    value = json.loads(raw)
                    value["attempts"][0]["watcher_exit_code"] = 7
                    extra = runtime.canonical_bytes(value)
                    self._write_valid_runtime_file(
                        active / f"ci-report-{hashlib.sha256(extra).hexdigest()}.json",
                        extra,
                    )
                result = self.ci_cleanup(fixture)
                self.assertFalse(result[0], result)
                self.assertTrue(active.exists())
                self.assertFalse(tombstone.exists())

    def test_ci_invalid_artifacts_block_before_namespace_mutation(self):
        cases = (
            "prefix", "noncanonical", "duplicate", "schema", "digest",
            "oversize", "symlink", "hardlink", "mode", "tracked_logical",
            "tracked_physical", "not_ignored_logical", "not_ignored_physical",
            "missing", "fifo", "directory", "owner",
        )
        for managed in (False, True):
            for case in cases:
                with self.subTest(managed=managed, case=case):
                    fixture = self.make_ci_terminal_state(count=1)
                    root, janitor, _, active, tombstone, names = fixture
                    report = active / names[0]
                    original = report.read_bytes()
                    if case == "prefix":
                        self._write_valid_runtime_file(
                            active / "ci-report-fake.json",
                            original,
                        )
                    elif case == "noncanonical":
                        report.write_bytes(original + b"\n")
                    elif case == "duplicate":
                        report.write_bytes(b'{"schema":"github-ci-report-v2",' + original[1:])
                    elif case == "schema":
                        report.write_bytes(b"{}")
                    elif case == "digest":
                        value = json.loads(original)
                        value["pull_number"] += 1
                        from workflowkit.runtime import canonical_bytes
                        report.write_bytes(canonical_bytes(value))
                    elif case == "oversize":
                        report.write_bytes(b"x" * (64 * 1024 + 1))
                    elif case == "symlink":
                        report.unlink()
                        report.symlink_to(root / "tracked.txt")
                    elif case == "hardlink":
                        os.link(report, root / "retained-report")
                    elif case == "mode":
                        report.chmod(0o644)
                    elif case == "tracked_logical":
                        self.run_git(root, "add", "-f", str(report.relative_to(root)))
                    elif case == "tracked_physical":
                        tombstone.mkdir()
                        self._write_valid_runtime_file(tombstone / report.name, original)
                        self.run_git(root, "add", "-f", str((tombstone / report.name).relative_to(root)))
                        (tombstone / report.name).unlink()
                        tombstone.rmdir()
                    elif case.startswith("not_ignored"):
                        # Only the other namespace is ignored: both must pass.
                        ignored = tombstone.name if case.endswith("logical") else active.name
                        (root / ".gitignore").write_text(f"/.kent/runtime/{ignored}/\n")
                    elif case in ("missing", "fifo", "directory"):
                        report.unlink()
                        if case == "fifo":
                            os.mkfifo(report, 0o600)
                        elif case == "directory":
                            report.mkdir()
                    ledger_before = (active / "evidence-ledger.jsonl").read_bytes()
                    real_fstat = janitor.os.fstat
                    report_inode = report.lstat().st_ino if report.exists() else None

                    def changed_owner(fd):
                        metadata = real_fstat(fd)
                        if case == "owner" and metadata.st_ino == report_inode:
                            fields = list(metadata)
                            fields[4] = os.getuid() + 1
                            return os.stat_result(fields)
                        return metadata

                    with mock.patch.object(janitor.os, "fstat", side_effect=changed_owner):
                        result = self.ci_cleanup(fixture, managed=managed)
                    self.assertFalse(result[0], result)
                    self.assertTrue(active.is_dir(), result)
                    self.assertFalse(tombstone.exists())
                    self.assertEqual((active / "evidence-ledger.jsonl").read_bytes(), ledger_before)

    def test_ci_references_are_sealed_logical_task_paths(self):
        for case in ("unreferenced", "foreign", "tombstone", "absolute", "malformed", "too_many"):
            with self.subTest(case=case):
                def mutate(rows):
                    artifact = rows[0]["artifacts"][0]
                    if case == "unreferenced":
                        rows[0]["artifacts"] = []
                    elif case == "foreign":
                        rows[0]["artifacts"] = [artifact.replace("/TASK-1/", "/OTHER/")]
                    elif case == "tombstone":
                        rows[0]["artifacts"] = [artifact.replace("/TASK-1/", "/.evidence-cleanup-" + "a" * 64 + "/")]
                    elif case == "absolute":
                        rows[0]["artifacts"] = ["/" + artifact]
                    elif case == "malformed":
                        rows[0]["artifacts"] = [artifact.replace("ci-report-", "ci-report-X")]
                    else:
                        rows[0]["artifacts"] = [
                            f".kent/runtime/TASK-1/ci-report-{index:064x}.json"
                            for index in range(65)
                        ]
                fixture = self.make_ci_terminal_state(count=1, mutate_records=mutate)
                for managed in (False, True):
                    result = self.ci_cleanup(fixture, managed=managed)
                    self.assertFalse(result[0], result)
                    self.assertTrue(fixture[3].exists())
                    self.assertFalse(fixture[4].exists())

    def test_ci_crash_boundaries_preserve_ledger_until_last_and_resume(self):
        phases = (
            "after_evidence_admission", "after_tombstone_rename",
            "after_terminal_sentinel_create",
            "before_fix_checkpoint_unlink", "after_fix_checkpoint_unlink_before_fsync",
            "after_fix_checkpoint_unlink_fsync",
            "before_smoke_checkpoint_unlink", "after_smoke_checkpoint_unlink_before_fsync",
            "after_smoke_checkpoint_unlink_fsync",
            "before_ci_report_unlink", "after_ci_report_unlink_before_fsync",
            "after_ci_report_unlink_fsync", "before_ledger_unlink",
            "after_ledger_unlink_before_fsync", "after_ledger_unlink_fsync",
            "before_tombstone_removal",
        )
        boundaries = [(phase, 1) for phase in phases] + [
            (phase, 2) for phase in (
                "before_ci_report_unlink", "after_ci_report_unlink_before_fsync",
                "after_ci_report_unlink_fsync",
            )
        ]
        for boundary, occurrence in boundaries:
            with self.subTest(boundary=boundary, occurrence=occurrence):
                fixture = self.make_ci_terminal_state()
                _, _, _, active, tombstone, names = fixture
                for name in ("fix-checkpoint.json", "smoke-checkpoint.json"):
                    self._write_valid_runtime_file(active / name, "{}\n")
                before = {p.name: p.read_bytes() for p in active.iterdir()}
                fired = False
                calls = 0

                def hook(phase):
                    nonlocal fired, calls
                    if phase == boundary:
                        calls += 1
                        if calls == occurrence:
                            fired = True
                            raise OSError("injected CI cleanup boundary")

                result = self.ci_cleanup(fixture, hook=hook)
                self.assertFalse(result[0], result)
                self.assertTrue(fired)
                remaining = active if active.exists() else tombstone
                after = {p.name: p.read_bytes() for p in remaining.iterdir()}
                self.assertTrue(all(before[name] == raw for name, raw in after.items()))
                if any(name in after for name in names):
                    self.assertIn("evidence-ledger.jsonl", after)
                if boundary.startswith("after_ci_report_unlink"):
                    self.assertEqual(sorted(set(names) - after.keys()), sorted(names)[:occurrence])
                retry = self.ci_cleanup(fixture)
                self.assertTrue(retry[0], retry)
                self.assertFalse(tombstone.exists())

    def test_ci_report_unlink_and_directory_fsync_failures_resume_ledger_last(self):
        for failure in ("unlink", "fsync"):
            for occurrence in (1, 2):
                with self.subTest(failure=failure, occurrence=occurrence):
                    fixture = self.make_ci_terminal_state()
                    _, janitor, _, _, tombstone, names = fixture
                    real_unlink, real_fsync = os.unlink, os.fsync
                    calls = 0
                    pending = False
                    fired = False

                    def unlink(name, *args, **kwargs):
                        nonlocal calls, pending, fired
                        if name in names:
                            calls += 1
                            if calls == occurrence:
                                if failure == "unlink":
                                    fired = True
                                    raise OSError("CI unlink failure")
                                pending = True
                        return real_unlink(name, *args, **kwargs)

                    def fsync(fd):
                        nonlocal pending, fired
                        if pending and stat.S_ISDIR(os.fstat(fd).st_mode):
                            pending = False
                            fired = True
                            raise OSError("CI directory fsync failure")
                        real_fsync(fd)

                    with mock.patch.object(janitor.os, "unlink", side_effect=unlink), \
                         mock.patch.object(janitor.os, "fsync", side_effect=fsync):
                        result = self.ci_cleanup(fixture)
                    self.assertFalse(result[0], result)
                    self.assertTrue(fired)
                    self.assertTrue((tombstone / "evidence-ledger.jsonl").exists())
                    missing = occurrence - (failure == "unlink")
                    self.assertEqual(
                        sorted(set(names) - {p.name for p in tombstone.iterdir()}),
                        sorted(names)[:missing],
                    )
                    retry = self.ci_cleanup(fixture)
                    self.assertTrue(retry[0], retry)
                    self.assertFalse(tombstone.exists())

    def test_ci_missing_subset_requires_verified_deletion_resume(self):
        for state in ("active", "tombstone", "resume", "managed_resume"):
            with self.subTest(state=state):
                fixture = self.make_ci_terminal_state()
                _, janitor, _, active, tombstone, names = fixture
                if state != "active":
                    active.rename(tombstone)
                current = active if state == "active" else tombstone
                (current / names[0]).unlink()
                if "resume" in state:
                    self._write_valid_runtime_file(
                        current.parent / janitor.runtime_state_names("TASK-1")[1],
                    )
                before = {p.name: p.read_bytes() for p in current.iterdir()}
                result = self.ci_cleanup(fixture, managed=state == "managed_resume")
                self.assertEqual(result[0], state == "resume", result)
                if state != "resume":
                    self.assertEqual({p.name: p.read_bytes() for p in current.iterdir()}, before)

    def test_ci_managed_crash_reentry_and_post_admission_drift_retain_raw_bytes(self):
        for boundary in ("after_evidence_admission", "after_tombstone_rename",
                         "after_terminal_sentinel_create"):
            with self.subTest(boundary=boundary):
                fixture = self.make_ci_terminal_state()
                _, _, _, active, tombstone, _ = fixture
                before = {p.name: p.read_bytes() for p in active.iterdir()}

                def crash(phase):
                    if phase == boundary:
                        raise OSError("managed retention interruption")

                result = self.ci_cleanup(fixture, managed=True, hook=crash)
                self.assertFalse(result[0], result)
                current = active if active.exists() else tombstone
                self.assertEqual({p.name: p.read_bytes() for p in current.iterdir()}, before)
                retry = self.ci_cleanup(fixture, managed=True)
                self.assertTrue(retry[0], retry)
                self.assertEqual({p.name: p.read_bytes() for p in tombstone.iterdir()}, before)
        for boundary in ("after_evidence_admission", "after_terminal_sentinel_create"):
            with self.subTest(drift=boundary):
                fixture = self.make_ci_terminal_state()
                _, _, _, active, tombstone, names = fixture

                def mutate(phase):
                    if phase == boundary:
                        current = active if active.exists() else tombstone
                        path = current / names[0]
                        raw = path.read_bytes()
                        inode = path.stat().st_ino
                        path.write_bytes(raw[:-1] + b" ")
                        self.assertEqual(path.stat().st_ino, inode)

                result = self.ci_cleanup(fixture, managed=True, hook=mutate)
                self.assertFalse(result[0], result)
                current = active if active.exists() else tombstone
                self.assertEqual(len(list(current.iterdir())), len(names) + 1)
                self.assertFalse(self.ci_cleanup(fixture, managed=True)[0])

    def test_ci_partial_cleanup_rejects_new_or_tampered_remaining_evidence(self):
        for case in ("new", "report", "ledger", "missing_ledger"):
            with self.subTest(case=case):
                fixture = self.make_ci_terminal_state()
                _, _, _, _, tombstone, names = fixture

                def fail_after_report(phase):
                    if phase == "after_ci_report_unlink_fsync":
                        raise OSError("partial report deletion")

                self.assertFalse(self.ci_cleanup(fixture, hook=fail_after_report)[0])
                remaining = tombstone / sorted(names)[1]
                self.assertTrue(remaining.exists())
                if case == "new":
                    self._write_valid_runtime_file(tombstone / "ci-report-unknown.json", "{}")
                elif case == "report":
                    remaining.write_bytes(remaining.read_bytes() + b" ")
                elif case == "ledger":
                    (tombstone / "evidence-ledger.jsonl").write_text("{}\n")
                else:
                    (tombstone / "evidence-ledger.jsonl").unlink()
                before = {p.name: p.read_bytes() for p in tombstone.iterdir()}
                result = self.ci_cleanup(fixture)
                self.assertFalse(result[0], result)
                self.assertEqual({p.name: p.read_bytes() for p in tombstone.iterdir()}, before)

    def test_ci_fd_and_directory_drift_blocks_without_further_loss(self):
        cases = ("file_swap", "same_inode_report", "same_inode_ledger", "mode",
                 "task_swap", "runtime_swap", "new", "sentinel")
        for boundary in ("after_evidence_admission", "after_tombstone_rename",
                         "before_ci_report_unlink"):
            for case in cases:
                if case == "sentinel" and boundary != "before_ci_report_unlink":
                    continue
                with self.subTest(boundary=boundary, case=case):
                    fixture = self.make_ci_terminal_state()
                    root, janitor, _, active, tombstone, names = fixture
                    saved = None
                    fired = False

                    def hook(phase):
                        nonlocal saved, fired
                        if phase != boundary or fired:
                            return
                        fired = True
                        directory = active if active.exists() else tombstone
                        target = directory / names[0]
                        if case == "file_swap":
                            raw = target.read_bytes()
                            target.rename(root / "saved-report")
                            self._write_valid_runtime_file(target, raw)
                        elif case.startswith("same_inode"):
                            if case.endswith("ledger"):
                                target = directory / "evidence-ledger.jsonl"
                            raw = target.read_bytes()
                            inode = target.stat().st_ino
                            target.write_bytes(raw[:-1] + b" ")
                            self.assertEqual(target.stat().st_ino, inode)
                        elif case == "mode":
                            target.chmod(0o400)
                        elif case == "task_swap":
                            directory.rename(root / "saved-evidence")
                            directory.mkdir(mode=0o700)
                            for source in (root / "saved-evidence").iterdir():
                                self._write_valid_runtime_file(directory / source.name, source.read_bytes())
                        elif case == "runtime_swap":
                            directory.parent.rename(root / "saved-runtime")
                            (root / ".kent/runtime").mkdir(mode=0o700)
                            directory = root / "saved-runtime" / directory.name
                        elif case == "new":
                            self._write_valid_runtime_file(directory / "new-entry")
                        elif case == "sentinel":
                            sentinel = directory.parent / janitor.runtime_state_names("TASK-1")[1]
                            sentinel.write_bytes(b"x")
                        saved = (directory, {p.name: p.read_bytes() for p in directory.iterdir()})

                    result = self.ci_cleanup(fixture, hook=hook)
                    self.assertFalse(result[0], result)
                    self.assertTrue(fired)
                    directory, before = saved
                    self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)

    def test_ci_ledger_duplicate_chain_and_byte_limits_preserve_evidence(self):
        for case in ("duplicate", "chain", "oversize", "exact_bytes"):
            with self.subTest(case=case):
                fixture = self.make_ci_terminal_state(count=1)
                _, janitor, _, active, tombstone, _ = fixture
                ledger = active / "evidence-ledger.jsonl"
                raw = ledger.read_bytes()
                if case == "duplicate":
                    raw = b'{"sequence":1,' + raw[1:]
                elif case == "chain":
                    changed = raw.replace(b'"previous_hash":""', b'"previous_hash":"x"', 1)
                    self.assertNotEqual(changed, raw)
                    raw = changed
                else:
                    length = janitor.MAX_LEDGER_BYTES + (case == "oversize")
                    raw = b" " * (length - len(raw)) + raw
                ledger.write_bytes(raw)
                result = self.ci_cleanup(fixture)
                self.assertEqual(result[0], case == "exact_bytes", result)
                if case != "exact_bytes":
                    self.assertTrue(active.exists())
                    self.assertFalse(tombstone.exists())
                    self.assertEqual(ledger.read_bytes(), raw)

    def test_ci_ledger_record_limit_is_inclusive_and_bounded(self):
        from workflowkit import runtime

        for count in (8192, 8193):
            with self.subTest(count=count):
                def records(rows):
                    prototype = {
                        "schema_version": 1, "task_short_id": "TASK-1",
                        "artifacts": rows[0]["artifacts"],
                    }
                    rows[:] = [dict(prototype) for _ in range(min(count, 8192) - 1)]
                fixture = self.make_ci_terminal_state(count=1, mutate_records=records)
                if count == 8193:
                    # The shared producer now rejects oversized chains itself.
                    # Construct an independently hostile postimage to retain
                    # Janitor's own admission-limit coverage.
                    root, janitor, _, active, _, names = fixture
                    path = active / "evidence-ledger.jsonl"
                    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
                    rows.insert(-1, dict(rows[-2]))
                    previous = ""
                    for index, row in enumerate(rows, 1):
                        row.pop("event_hash", None)
                        row.update(sequence=index, previous_hash=previous)
                        row["event_hash"] = runtime.canonical_sha256(row)
                        previous = row["event_hash"]
                    path.write_bytes(b"".join(runtime.canonical_bytes(row) + b"\n" for row in rows))
                    marker = runtime.validate_terminal_chain(rows, task_short_id="TASK-1")
                    fixture = (
                        root, janitor, runtime.terminal_marker_line(marker), active,
                        active.parent / (".evidence-cleanup-" + runtime.canonical_sha256(marker)), names,
                    )
                result = self.ci_cleanup(fixture)
                self.assertEqual(result[0], count == 8192, result)
                if count == 8193:
                    self.assertTrue(fixture[3].exists())
                    self.assertFalse(fixture[4].exists())

    def test_ci_resume_requires_exact_marker_tombstone_chain_and_safe_sentinel(self):
        from workflowkit import runtime

        for case in ("marker", "tombstone", "chain", "sentinel_bytes", "sentinel_mode"):
            with self.subTest(case=case):
                fixture = self.make_ci_terminal_state()
                root, janitor, marker, active, tombstone, names = fixture
                active.rename(tombstone)
                (tombstone / names[0]).unlink()
                sentinel = tombstone.parent / janitor.runtime_state_names("TASK-1")[1]
                self._write_valid_runtime_file(sentinel)
                if case == "marker":
                    value = runtime.validate_cleanup_report(marker)
                    value["task_short_id"] = "OTHER"
                    marker = "TERMINAL_EVIDENCE_V1 " + runtime.canonical_bytes(value).decode()
                    fixture = (root, janitor, marker, active, tombstone, names)
                elif case == "tombstone":
                    moved = tombstone.with_name(".evidence-cleanup-" + "b" * 64)
                    tombstone.rename(moved)
                    tombstone = moved
                elif case == "chain":
                    ledger = tombstone / "evidence-ledger.jsonl"
                    raw = ledger.read_bytes()
                    changed = raw.replace(b'"previous_hash":""', b'"previous_hash":"x"', 1)
                    self.assertNotEqual(changed, raw)
                    ledger.write_bytes(changed)
                elif case == "sentinel_bytes":
                    sentinel.write_bytes(b"x")
                else:
                    sentinel.chmod(0o644)
                before = {p.name: p.read_bytes() for p in tombstone.iterdir()}
                result = self.ci_cleanup(fixture)
                self.assertFalse(result[0], result)
                self.assertEqual({p.name: p.read_bytes() for p in tombstone.iterdir()}, before)

    def test_ci_declared_v2_never_falls_back_when_runtime_or_support_disappears(self):
        for managed in (False, True):
            for case in ("support_missing", "support_symlink", "runtime_missing",
                         "runtime_empty", "ledger_missing"):
                with self.subTest(managed=managed, case=case):
                    fixture = self.make_ci_terminal_state(count=1)
                    root, _, _, active, _, names = fixture
                    current = active
                    if case.startswith("support"):
                        support = root / ".kent/scripts/workflow_runtime_contracts.py"
                        saved = root / "retained-support.py"
                        support.rename(saved)
                        if case == "support_symlink":
                            support.symlink_to(saved)
                    elif case in ("runtime_missing", "runtime_empty"):
                        moved = root / "retained-runtime"
                        active.parent.rename(moved)
                        current = moved / active.name
                        if case == "runtime_empty":
                            active.parent.mkdir(mode=0o700)
                    else:
                        (active / "evidence-ledger.jsonl").unlink()
                    before = {name: (current / name).read_bytes() for name in names}
                    result = self.ci_cleanup(fixture, managed=managed)
                    self.assertIsNotNone(result, "declared v2 must never select legacy cleanup")
                    self.assertFalse(result[0], result)
                    self.assertEqual({name: (current / name).read_bytes() for name in names}, before)

    def test_ci_inventory_is_bounded_during_enumeration(self):
        janitor = load_template_module(JANITOR, "ci_inventory_bounds")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(256):
                (root / str(index)).touch()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(len(janitor._bounded_inventory(descriptor)), 256)
                (root / "overflow").touch()
                with self.assertRaisesRegex(
                    ValueError, "runtime evidence inventory exceeds a fixed limit",
                ):
                    janitor._bounded_inventory(descriptor)
            finally:
                os.close(descriptor)
        fixture = self.make_ci_terminal_state(count=1)
        for index in range(255):
            self._write_valid_runtime_file(fixture[3] / f"unknown-{index}")
        result = self.ci_cleanup(fixture)
        self.assertFalse(result[0], result)
        self.assertIn("inventory_limit", result[1])
        self.assertTrue(fixture[3].exists())

    def test_ci_report_reference_and_present_byte_bounds(self):
        fixture = self.make_ci_terminal_state(count=64, report_size=64 * 1024)
        result = self.ci_cleanup(fixture, managed=True)
        self.assertTrue(result[0], result)
        self.assertEqual(len(list(fixture[4].glob("ci-report-*.json"))), 64)
        self.assertEqual(
            sum(p.stat().st_size for p in fixture[4].glob("ci-report-*.json")),
            4 * 1024 * 1024,
        )
        fixture = self.make_ci_terminal_state()
        _, janitor, _, active, _, names = fixture
        total = sum((active / name).stat().st_size for name in names)
        # The real aggregate check is exercised at its inclusive boundary.
        # The declared production bound is separately asserted, not weakened.
        self.assertEqual(janitor.MAX_CI_ARTIFACT_BYTES, 4 * 1024 * 1024)
        self.assertEqual(janitor.MAX_CI_ARTIFACTS, 64)
        with mock.patch.object(janitor, "MAX_CI_ARTIFACT_BYTES", total - 1):
            result = self.ci_cleanup(fixture, managed=True)
        self.assertFalse(result[0], result)
        with mock.patch.object(janitor, "MAX_CI_ARTIFACT_BYTES", total):
            result = self.ci_cleanup(fixture, managed=True)
        self.assertTrue(result[0], result)
        with tempfile.TemporaryFile() as stream:
            stream.write(b"x" * (64 * 1024))
            stream.flush()
            self.assertEqual(len(janitor._bounded_file_bytes(stream.fileno(), 64 * 1024)), 64 * 1024)
            stream.seek(0, os.SEEK_END)
            stream.write(b"x")
            stream.flush()
            with self.assertRaisesRegex(
                ValueError, "runtime evidence inventory exceeds a fixed limit",
            ):
                janitor._bounded_file_bytes(stream.fileno(), 64 * 1024)
            # Even a stale size observation cannot bypass the capped read.
            fields = list(os.fstat(stream.fileno()))
            fields[6] = 0
            with mock.patch.object(os, "fstat", return_value=os.stat_result(fields)), \
                 self.assertRaisesRegex(
                     ValueError, "runtime evidence inventory exceeds a fixed limit",
                 ):
                janitor._bounded_file_bytes(stream.fileno(), 64 * 1024)

    def test_ci_scoped_admission_closes_every_descriptor_on_success_and_failure(self):
        for case in ("success", "unknown", "fsync", "managed"):
            with self.subTest(case=case):
                fixture = self.make_ci_terminal_state(count=1)
                _, janitor, _, active, _, _ = fixture
                if case == "unknown":
                    self._write_valid_runtime_file(active / "unknown")
                opened = set()
                real_open, real_dup, real_close = os.open, os.dup, os.close

                def remember_open(*args, **kwargs):
                    fd = real_open(*args, **kwargs)
                    opened.add(fd)
                    return fd

                def remember_dup(*args, **kwargs):
                    fd = real_dup(*args, **kwargs)
                    opened.add(fd)
                    return fd

                def remember_close(fd):
                    real_close(fd)
                    opened.discard(fd)

                real_fsync = os.fsync
                def fsync(fd):
                    if case == "fsync":
                        raise OSError("injected descriptor ownership failure")
                    real_fsync(fd)

                with mock.patch.object(os, "open", side_effect=remember_open), \
                     mock.patch.object(os, "dup", side_effect=remember_dup), \
                     mock.patch.object(os, "close", side_effect=remember_close), \
                     mock.patch.object(os, "fsync", side_effect=fsync):
                    result = self.ci_cleanup(fixture, managed=case == "managed")
                self.assertEqual(
                    result[0],
                    case in ("success", "managed"),
                    result,
                )
                if case == "unknown":
                    self.assertIn("unknown_runtime_entry", result[1])
                    self.assertTrue(active.is_dir())
                    self.assertTrue((active / "unknown").is_file())
                self.assertEqual(opened, set())

    def test_ci_git_admission_rejects_actual_tracked_reports_under_foreign_routing(self):
        for managed in (False, True):
            for injection in ("control", "repository", "index", "objects",
                              "namespace", "config_count", "config_parameters",
                              "config_files", "discovery"):
                with self.subTest(managed=managed, injection=injection):
                    fixture = self.make_ci_terminal_state(count=1)
                    root, janitor, _, active, tombstone, names = fixture
                    report = active / names[0]
                    self.run_git(root, "add", "-f", str(report.relative_to(root)))
                    git_dir = Path(self.run_git(root, "rev-parse", "--absolute-git-dir").stdout.strip()).resolve()
                    foreign = self.create_repository()
                    foreign_dir = foreign / ".git"
                    foreign_config = foreign / "injected-config"
                    foreign_config.write_text(f"[core]\nworktree = {foreign}\n")
                    overrides = {
                        "control": {},
                        "repository": {"GIT_DIR": str(foreign_dir), "GIT_WORK_TREE": str(foreign)},
                        "index": {"GIT_INDEX_FILE": str(foreign_dir / "index")},
                        "objects": {
                            "GIT_COMMON_DIR": str(foreign_dir),
                            "GIT_OBJECT_DIRECTORY": str(foreign_dir / "objects"),
                            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign_dir / "objects"),
                        },
                        "namespace": {
                            "GIT_NAMESPACE": "foreign",
                            "GIT_REPLACE_REF_BASE": "refs/foreign/",
                        },
                        "config_count": {
                            "GIT_CONFIG_COUNT": "1",
                            "GIT_CONFIG_KEY_0": "core.worktree",
                            "GIT_CONFIG_VALUE_0": str(foreign),
                        },
                        "config_parameters": {
                            "GIT_CONFIG_PARAMETERS": f"'core.worktree={foreign}'",
                        },
                        "config_files": {
                            "GIT_CONFIG": str(foreign_config),
                            "GIT_CONFIG_GLOBAL": str(foreign_config),
                            "GIT_CONFIG_SYSTEM": str(foreign_config),
                        },
                        "discovery": {
                            "GIT_CEILING_DIRECTORIES": str(root),
                            "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
                            "GIT_PREFIX": "foreign/",
                        },
                    }[injection]
                    before = {p.name: p.read_bytes() for p in active.iterdir()}
                    protected = {
                        path: path.read_bytes() for path in (
                            git_dir / "index", git_dir / "config",
                            foreign_dir / "index", foreign_dir / "config",
                        )
                    }
                    with mock.patch.dict(os.environ, overrides), \
                         mock.patch.object(janitor, "run", wraps=janitor.run) as probes:
                        result = self.ci_cleanup(fixture, managed=managed)
                    self.assertFalse(result[0], result)
                    self.assertFalse(tombstone.exists())
                    self.assertEqual({p.name: p.read_bytes() for p in active.iterdir()}, before)
                    self.assertEqual({p: p.read_bytes() for p in protected}, protected)
                    self.assertGreater(len(probes.call_args_list), 1)
                    for probe in probes.call_args_list:
                        environment = probe.kwargs["environment"]
                        self.assertLessEqual(
                            {key for key in environment if key.startswith("GIT_")},
                            {"GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT", "GIT_INDEX_FILE"},
                        )
                        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
                        if "rev-parse" not in probe.args[0]:
                            self.assertEqual(environment["GIT_INDEX_FILE"], str(git_dir / "index"))
                            self.assertIn(f"--git-dir={git_dir}", probe.args[0])
                            self.assertIn(f"--work-tree={root.resolve()}", probe.args[0])

    def test_ci_git_admission_binds_linked_worktree_index_not_common_index(self):
        for managed in (False, True):
            for tracked in (False, True):
                with self.subTest(managed=managed, tracked=tracked):
                    fixture = self.make_ci_terminal_state(count=1, linked=True)
                    root, _, _, active, tombstone, names = fixture
                    self.assertTrue((root / ".git").is_file())
                    if tracked:
                        self.run_git(root, "add", "-f", str((active / names[0]).relative_to(root)))
                    git_dir = Path(self.run_git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
                    common = Path(self.run_git(root, "rev-parse", "--git-common-dir").stdout.strip())
                    self.assertNotEqual(git_dir, common)
                    protected = {
                        path: path.read_bytes() for path in (
                            git_dir / "index", common / "index", common / "config",
                        )
                    }
                    foreign = self.create_repository()
                    before = {p.name: p.read_bytes() for p in active.iterdir()}
                    with mock.patch.dict(os.environ, {
                        "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                        "GIT_INDEX_FILE": str(common / "index"),
                    }):
                        result = self.ci_cleanup(fixture, managed=managed)
                    self.assertEqual(result[0], not tracked, result)
                    self.assertEqual({p: p.read_bytes() for p in protected}, protected)
                    if tracked:
                        self.assertTrue(active.exists())
                        self.assertFalse(tombstone.exists())
                    elif managed:
                        self.assertEqual({p.name: p.read_bytes() for p in tombstone.iterdir()}, before)
                    else:
                        self.assertFalse(active.exists())
                        self.assertFalse(tombstone.exists())

    def test_ci_git_admission_honors_ordinary_user_and_repository_exclusions(self):
        for source in ("user", "repository"):
            for managed in (False, True):
                with self.subTest(source=source, managed=managed):
                    fixture = self.make_ci_terminal_state(count=1)
                    root, _, _, active, tombstone, _ = fixture
                    (root / ".gitignore").write_text("")
                    home = root / "fixture-home"
                    home.mkdir()
                    environment = {key: value for key, value in os.environ.items()
                                   if not key.startswith("GIT_")}
                    environment.update(HOME=str(home), XDG_CONFIG_HOME=str(home / "xdg"))
                    if source == "user":
                        ignore = home / "global-ignore"
                        ignore.write_text("/.kent/runtime/\n")
                        (home / ".gitconfig").write_text(f"[core]\nexcludesFile = {ignore}\n")
                    else:
                        (root / ".git/info/exclude").write_text("/.kent/runtime/\n")
                    with mock.patch.dict(os.environ, environment, clear=True):
                        result = self.ci_cleanup(fixture, managed=managed)
                    self.assertTrue(result[0], result)
                    self.assertFalse(active.exists())
                    self.assertEqual(tombstone.exists(), managed)
                    self.assertEqual((root / ".gitignore").read_text(), "")

    def test_ci_git_admission_does_not_accept_injected_ignore_configuration(self):
        for managed in (False, True):
            fixture = self.make_ci_terminal_state(count=1)
            root, _, _, active, tombstone, _ = fixture
            (root / ".gitignore").write_text("")
            foreign_ignore = root / "injected-ignore"
            foreign_ignore.write_text("/.kent/runtime/\n")
            before = {p.name: p.read_bytes() for p in active.iterdir()}
            with mock.patch.dict(os.environ, {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.excludesFile",
                "GIT_CONFIG_VALUE_0": str(foreign_ignore),
            }):
                result = self.ci_cleanup(fixture, managed=managed)
            self.assertFalse(result[0], result)
            self.assertFalse(tombstone.exists())
            self.assertEqual({p.name: p.read_bytes() for p in active.iterdir()}, before)

    def test_ci_git_policy_drift_blocks_before_rename_or_unlink_and_preserves_staging(self):
        for linked in (False, True):
            for managed in (False, True):
                boundaries = ("after_evidence_admission", "after_terminal_sentinel_create") if managed else (
                    "after_evidence_admission", "before_ci_report_unlink",
                )
                for boundary in boundaries:
                    for drift in ("index", "ignore"):
                        with self.subTest(linked=linked, managed=managed,
                                          boundary=boundary, drift=drift):
                            fixture = self.make_ci_terminal_state(count=1, linked=linked)
                            root, _, _, active, tombstone, names = fixture
                            git_dir = Path(self.run_git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
                            saved = None

                            def change_policy(phase):
                                nonlocal saved
                                if phase != boundary or saved is not None:
                                    return
                                current = active if active.exists() else tombstone
                                if drift == "index":
                                    self.run_git(
                                        root, "add", "-f", str((current / names[0]).relative_to(root)),
                                    )
                                else:
                                    (root / ".gitignore").write_text("")
                                saved = (
                                    current,
                                    {p.name: p.read_bytes() for p in current.iterdir()},
                                    (git_dir / "index").read_bytes(),
                                    (root / ".gitignore").read_bytes(),
                                )

                            result = self.ci_cleanup(fixture, managed=managed, hook=change_policy)
                            self.assertIsNotNone(saved)
                            self.assertFalse(result[0], result)
                            current, evidence, index, ignore = saved
                            self.assertEqual({p.name: p.read_bytes() for p in current.iterdir()}, evidence)
                            self.assertEqual((git_dir / "index").read_bytes(), index)
                            self.assertEqual((root / ".gitignore").read_bytes(), ignore)
                            if boundary == "after_evidence_admission":
                                self.assertTrue(active.exists())
                                self.assertFalse(tombstone.exists())

    def test_ci_git_revalidation_batches_all_remaining_reports(self):
        fixture = self.make_ci_terminal_state(count=64)
        _, janitor, _, _, _, _ = fixture
        with mock.patch.object(janitor, "run", wraps=janitor.run) as probes:
            result = self.ci_cleanup(fixture)
        self.assertTrue(result[0], result)
        calls = probes.call_args_list
        # At most two probes per revalidation boundary, not 64 per boundary.
        self.assertLessEqual(len(calls), 2 * (64 + 8) + 1)
        ignored = [call for call in calls if "check-ignore" in call.args[0]]
        tracked = [call for call in calls if "ls-files" in call.args[0]]
        self.assertEqual(len(ignored), len(tracked))
        self.assertGreater(len(ignored), 64)
        for call in ignored:
            paths = call.kwargs["input_text"].split("\0")[:-1]
            self.assertLessEqual(len(paths), 3 * (64 + 3))
            self.assertEqual(paths, sorted(set(paths)))

    def janitor_input(self, root: Path, **overrides: str) -> str:
        cleanup_mode = overrides.get("cleanup_mode", "report_only")
        payload = {
            "workspace_path": str(root),
            "task_short_id": "TASK-1",
            "branch_name": "",
            "pr_url": "",
            "pr_base_branch": (
                "main" if cleanup_mode == "merged" else "not-applicable"
            ),
            "merge_report": "",
            "cleanup_mode": cleanup_mode,
            "cleanup_session_id": "session-test",
            "cleanup_report": "Cleanup preflight passed.",
        }
        payload.update(overrides)
        return json.dumps(payload)

    def run_managed_janitor_module(
        self,
        janitor,
        root: Path,
        worktree: Path,
        *,
        cleanup_report: str,
        native_delete_called: Path,
        cleanup_mode: str = "no_pr",
        pr_url: str = "",
    ) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        wrapper = self.completed_wrapper(root)
        environment = {
            **os.environ,
            "KENT_WORKTREE_WRAPPER": str(wrapper),
            "KENT_TEST_PRIMARY": str(root),
            "KENT_TEST_WRAPPER_MARKER": str(native_delete_called),
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(
                sys,
                "stdin",
                io.StringIO(
                    self.janitor_input(
                        worktree,
                        branch_name="TASK-1",
                        cleanup_mode=cleanup_mode,
                        pr_url=pr_url,
                        cleanup_report=cleanup_report,
                    )
                ),
            ),
            mock.patch.object(sys, "stdout", output),
        ):
            original_cwd = Path.cwd()
            try:
                exit_code = janitor.main()
            finally:
                os.chdir(original_cwd)
        return exit_code, json.loads(output.getvalue())

    def add_origin_branch(self, root: Path, worktree: Path) -> Path:
        remote_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(remote_temporary.cleanup)
        remote = Path(remote_temporary.name)
        self.run_git(remote, "init", "--bare", "-q")
        self.run_git(root, "remote", "add", "origin", str(remote))
        branch = self.run_git(
            worktree,
            "branch",
            "--show-current",
        ).stdout.strip()
        self.run_git(
            root,
            "push",
            "-q",
            "origin",
            f"refs/heads/{branch}:refs/heads/{branch}",
        )
        self.set_fixture_github_origin(root, remote)
        return remote

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
            "if [ \"$1\" = list ]; then\n"
            "  python3 -c 'import json, os, subprocess; primary=os.environ[\"KENT_TEST_PRIMARY\"]; "
            "state=os.path.join(primary, \".kent-test-record-root\"); done=os.path.join(primary, "
            "\".kent-test-delete-done\"); root=(open(state).read().strip() if os.path.exists(state) "
            "else next(line[9:] for line in subprocess.check_output([\"git\", \"-C\", primary, "
            "\"worktree\", \"list\", \"--porcelain\"], text=True).splitlines() if line.startswith("
            "\"worktree \") and os.path.realpath(line[9:]) != os.path.realpath(primary))); "
            "open(state, \"w\").write(root); variant=\"missing\" if os.path.exists(done) else \"registered\"; "
            "topology={\"variant\": variant, variant: {\"kent\": {\"worktree_id\": \"kent-test-record\", "
            "\"canonical_root\": root, \"display_name\": \"TASK-1\", \"managed\": True}}}; "
            "topology[variant].update({\"git\": {\"canonical_root\": root, \"head_object\": "
            "subprocess.check_output([\"git\", \"-C\", root, \"rev-parse\", \"HEAD\"], text=True).strip(), "
            "\"branch_ref\": \"refs/heads/TASK-1\", \"branch_name\": \"TASK-1\", \"path_available\": True}} "
            "if variant == \"registered\" else {}); print(json.dumps({\"worktrees\": [{\"topology\": topology}]}))'\n"
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
            "touch \"$KENT_TEST_PRIMARY/.kent-test-delete-done\"\n"
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

    def retained_kent_record_wrapper(self, root: Path) -> Path:
        wrapper = root / "kent-worktree-retained-record"
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
            "if [ \"$1\" = list ]; then\n"
            "  if [ -e \"$KENT_TEST_DELETE_DONE\" ]; then\n"
            "    cat \"$KENT_TEST_KENT_LIST_AFTER\"\n"
            "  else\n"
            "    cat \"$KENT_TEST_KENT_LIST_BEFORE\"\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "for argument in \"$@\"; do target=\"$argument\"; done\n"
            "git -C \"$KENT_TEST_PRIMARY\" worktree remove --force \"$target\"\n"
            "touch \"$KENT_TEST_DELETE_DONE\"\n"
            "printf '%s\\n' "
            "'{\"kind\":\"completed\",\"completed\":{\"cleanup\":"
            "{\"kind\":\"retained\"}}}'\n"
        )
        wrapper.chmod(0o755)
        return wrapper

    def test_no_pr_cleanup_settles_local_branch_and_reports_retained_kent_record(
        self,
    ) -> None:
        root = self.create_repository()
        remote_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(remote_temporary.cleanup)
        remote = Path(remote_temporary.name)
        self.run_git(remote, "init", "--bare", "-q")
        self.run_git(root, "remote", "add", "origin", str(remote))
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        self.run_git(
            root,
            "push",
            "-q",
            "origin",
            "refs/heads/TASK-1:refs/heads/TASK-1",
        )
        self.set_fixture_github_origin(root, remote)

        kent_record_id = "kent-record-task-1"
        before = root / "kent-list-before.json"
        before.write_text(
            json.dumps(
                {
                    "worktrees": [
                        {
                            "topology": {
                                "variant": "registered",
                                "registered": {
                                    "git": {
                                        "canonical_root": str(worktree),
                                        "head_object": head,
                                        "branch_ref": "refs/heads/TASK-1",
                                        "branch_name": "TASK-1",
                                        "path_available": True,
                                    },
                                    "kent": {
                                        "worktree_id": kent_record_id,
                                        "canonical_root": str(worktree),
                                        "display_name": "TASK-1",
                                        "managed": True,
                                    },
                                },
                            }
                        }
                    ]
                }
            )
        )
        after = root / "kent-list-after.json"
        after.write_text(
            json.dumps(
                {
                    "worktrees": [
                        {
                            "topology": {
                                "variant": "missing",
                                "missing": {
                                    "kent": {
                                        "worktree_id": kent_record_id,
                                        "canonical_root": str(worktree),
                                        "display_name": "TASK-1",
                                        "managed": True,
                                    }
                                },
                            }
                        }
                    ]
                }
            )
        )
        delete_done = root / "kent-delete-done"
        fake_wrapper = self.retained_kent_record_wrapper(root)

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
                "KENT_TEST_PRIMARY": str(root),
                "KENT_TEST_KENT_LIST_BEFORE": str(before),
                "KENT_TEST_KENT_LIST_AFTER": str(after),
                "KENT_TEST_DELETE_DONE": str(delete_done),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["transition"], "task_janitor_done")
        self.assertFalse(worktree.exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    "refs/heads/TASK-1",
                ],
                check=False,
            ).returncode,
            0,
        )
        self.assertEqual(
            subprocess.run(
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
            ).returncode,
            0,
        )
        report = payload["cleanup_report"]
        self.assertIn("retained restorative metadata", report)
        self.assertIn(kent_record_id, report)
        self.assertIn(str(worktree), report)
        self.assertIn("Local branch disposition: absent.", report)
        self.assertIn("Remote branch retained: true", report)

    def test_kent_topology_preflight_and_post_delete_matrix(self) -> None:
        for selected_phase in ("before", "after", "native_before", "native_after"):
            native = selected_phase.startswith("native_")
            phase = selected_phase.removeprefix("native_")
            for state in (
                "absent", "missing", "registered", "changed_id", "changed_root",
                "duplicate", "unreadable", "malformed", "external",
            ):
                with self.subTest(phase=phase, state=state, native=native):
                    root, worktree, _, _ = self.make_managed_worktree(with_v2=False)
                    kent = {
                        "worktree_id": "kent-record-task-1",
                        "canonical_root": str(worktree),
                        "managed": True,
                    }
                    registered = {"topology": {
                        "variant": "registered",
                        "registered": {
                            "kent": kent,
                            "git": {"canonical_root": str(worktree)},
                        },
                    }}
                    missing = {"topology": {
                        "variant": "missing", "missing": {"kent": kent},
                    }}
                    entries = [missing]
                    if state == "absent":
                        entries = []
                    elif state == "registered":
                        entries = [registered]
                    elif state in ("changed_id", "changed_root"):
                        changed = dict(kent)
                        changed["worktree_id" if state == "changed_id" else "canonical_root"] = (
                            "another-record" if state == "changed_id" else str(root / "other")
                        )
                        entries = [{"topology": {
                            "variant": "missing", "missing": {"kent": changed},
                        }}]
                    elif state == "duplicate":
                        entries = [registered, registered] if phase == "before" else [missing, missing]
                    elif state == "external":
                        entries = [{"topology": {
                            "variant": "external",
                            "external": {"git": {"canonical_root": str(worktree)}},
                        }}]
                    before = root / "kent-before.json"
                    after = root / "kent-after.json"
                    before.write_text(json.dumps({"worktrees": [registered]}))
                    after.write_text(json.dumps({"worktrees": [missing]}))
                    selected = before if phase == "before" else after
                    selected.write_text(
                        '{"worktrees":[{"topology":{"variant":"missing","missing":{}}}]}'
                        if state == "malformed" else json.dumps({"worktrees": entries})
                    )
                    if state == "unreadable":
                        selected.unlink()
                    if native and state != "malformed":
                        for fixture in (before, after):
                            if not fixture.exists():
                                continue
                            payload = json.loads(fixture.read_text())
                            for item in payload["worktrees"]:
                                topology = item["topology"]
                                variant = topology.pop("variant")
                                for name in ("git", "kent"):
                                    identity = topology[variant].get(name)
                                    if identity is None:
                                        continue
                                    identity["canonicalRoot"] = identity.pop("canonical_root")
                                    if "worktree_id" in identity:
                                        identity["worktreeId"] = identity.pop("worktree_id")
                            payload["worktrees"].insert(0, {"topology": {
                                "mainWorkspace": {"git": {
                                    "canonicalRoot": str(root), "isMainWorktree": True,
                                }},
                            }})
                            fixture.write_text(json.dumps(payload))
                    deleted = root / "delete-done"
                    wrapper = self.retained_kent_record_wrapper(root)
                    result = subprocess.run(
                        [str(JANITOR)], cwd=worktree,
                        input=self.janitor_input(
                            worktree, branch_name="TASK-1", cleanup_mode="no_pr",
                        ),
                        text=True, capture_output=True, check=False,
                        env={
                            **os.environ, "KENT_WORKTREE_WRAPPER": str(wrapper),
                            "KENT_TEST_PRIMARY": str(root),
                            "KENT_TEST_KENT_LIST_BEFORE": str(before),
                            "KENT_TEST_KENT_LIST_AFTER": str(after),
                            "KENT_TEST_DELETE_DONE": str(deleted),
                        },
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout)
                    accepted = (phase == "before" and state == "registered") or (
                        phase == "after" and state in ("absent", "missing")
                    )
                    self.assertEqual(payload["transition"],
                                     "task_janitor_done" if accepted else "task_janitor_blocked",
                                     payload)
                    native_deleted = phase == "after" or accepted
                    self.assertEqual(deleted.exists(), native_deleted)
                    self.assertEqual(worktree.exists(), not native_deleted)
                    self.assertEqual(str(worktree) in self.run_git(
                        root, "worktree", "list", "--porcelain",
                    ).stdout, not native_deleted)
                    local = subprocess.run(
                        ["git", "-C", str(root), "show-ref", "--verify", "refs/heads/TASK-1"],
                        capture_output=True, check=False,
                    )
                    self.assertEqual(local.returncode == 0, not accepted)
                    self.assertIn("refs/heads/TASK-1", self.run_fixture_git(
                        # The logical GitHub origin is routed to the owned bare
                        # repository only through the fixture shim.
                        root, "ls-remote", "--heads", "origin", "refs/heads/TASK-1",
                    ).stdout)
                    if accepted and state != "absent":
                        self.assertIn("retained restorative metadata", payload["cleanup_report"])
                    if phase == "after" and not accepted:
                        self.assertNotIn("Janitor preserved", payload["cleanup_report"])

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
            environment = {**os.environ, **EVIDENCE_FIXTURE_OWNER}
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
                env=environment,
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
                env=environment,
            )
            self.assertEqual(seal.returncode, 0, seal.stderr)
            marker = json.loads(seal.stdout)["terminal_marker"]
        self.add_origin_branch(root, worktree)
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
                    "baseRefName": "main",
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
        wrapper = self.completed_wrapper(root)
        content = wrapper.read_text()
        content = content.replace(
            "for argument in \"$@\"; do target=\"$argument\"; done\n",
            "if [ \"$1\" = delete ]; then\n"
            "  printf '%s\\n' '{\"kind\":\"completed\"}'\n"
            "  exit 0\n"
            "fi\n"
            "for argument in \"$@\"; do target=\"$argument\"; done\n",
            1,
        )
        wrapper.write_text(content)
        return wrapper

    def fake_tombstone_completed_wrapper(self, root: Path) -> Path:
        wrapper = self.completed_wrapper(root)
        content = wrapper.read_text()
        content = content.replace(
            "for argument in \"$@\"; do target=\"$argument\"; done\n",
            "if [ \"$1\" = delete ]; then\n"
            "  for argument in \"$@\"; do target=\"$argument\"; done\n"
            "  if [ -n \"${KENT_TEST_EXACT_TOMBSTONE:-}\" ]; then\n"
            "    mv \"$target/.kent/runtime/$KENT_TEST_EXACT_TOMBSTONE\" "
            "\"$target/.kent/runtime/.evidence-cleanup-fake\"\n"
            "  fi\n"
            "  printf '%s\\n' '{\"kind\":\"completed\"}'\n"
            "  exit 0\n"
            "fi\n"
            "for argument in \"$@\"; do target=\"$argument\"; done\n",
            1,
        )
        wrapper.write_text(content)
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

    def test_managed_runtime_parent_retry_admits_exact_tombstone_after_native_refusal(
        self,
    ) -> None:
        root, worktree, scripts, marker = self.make_managed_worktree(with_v2=True)
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_runtime_parent_native_retry",
        )
        native_delete_called = root / "native-delete-called"
        delete_calls = []
        branch_oid_before = self.run_git(
            root, "rev-parse", "refs/heads/TASK-1",
        ).stdout.strip()

        def native_refusal(*args, **kwargs):
            delete_calls.append((args, kwargs))
            return False, "injected native refusal", "native refused"

        with mock.patch.object(
            janitor,
            "delete_managed_worktree",
            side_effect=native_refusal,
        ):
            first_code, first = self.run_managed_janitor_module(
                janitor,
                root,
                worktree,
                cleanup_report=marker,
                native_delete_called=native_delete_called,
            )

        self.assertEqual(first_code, 0, first)
        self.assertEqual(first["transition"], "task_janitor_blocked", first)
        self.assertEqual(len(delete_calls), 1)
        self.assertFalse(native_delete_called.exists())
        self.assertTrue(worktree.is_dir())
        self.assertIn(
            str(worktree),
            self.run_git(root, "worktree", "list", "--porcelain").stdout,
        )
        self.assertEqual(
            self.run_git(root, "rev-parse", "refs/heads/TASK-1").stdout.strip(),
            branch_oid_before,
        )

        runtime_contracts = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_parent_retry_contracts",
        )
        marker_value = runtime_contracts.validate_cleanup_report(marker)
        runtime_parent = worktree / ".kent" / "runtime"
        tombstone = (
            ".evidence-cleanup-" + runtime_contracts.canonical_sha256(marker_value)
        )
        lock_name, sentinel_name = janitor.runtime_state_names("TASK-1")
        self.assertEqual(
            {item.name for item in runtime_parent.iterdir()},
            {lock_name, sentinel_name, tombstone},
        )
        self.assertTrue((runtime_parent / tombstone).is_dir())
        self.assertTrue((runtime_parent / sentinel_name).is_file())

        second_code, second = self.run_managed_janitor_module(
            janitor,
            root,
            worktree,
            cleanup_report=marker,
            native_delete_called=native_delete_called,
        )
        self.assertEqual(second_code, 0, second)
        self.assertEqual(second["transition"], "task_janitor_done", second)
        self.assertFalse(worktree.exists())
        self.assertTrue(root.exists(), f"primary checkout disappeared: {root}")
        worktrees_after = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            worktrees_after.returncode,
            0,
            f"primary checkout is not usable: {worktrees_after.stderr}",
        )
        self.assertNotIn(
            str(worktree),
            worktrees_after.stdout,
        )
        local_branch = subprocess.run(
            [
                "git", "-C", str(root), "show-ref", "--verify", "--quiet",
                "refs/heads/TASK-1",
            ],
            check=False,
        )
        self.assertEqual(local_branch.returncode, 1)
        self.assertIn(
            "refs/heads/TASK-1",
            self.run_fixture_git(
                root, "ls-remote", "--heads", "origin", "refs/heads/TASK-1",
            ).stdout,
        )

    def test_managed_legacy_runtime_unknown_blocks_before_native_delete(self) -> None:
        root, worktree, scripts, _ = self.make_managed_worktree(with_v2=False)
        runtime = worktree / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        self._write_valid_runtime_file(
            runtime / "evidence-ledger.jsonl",
            b"legacy ledger\n",
        )
        unknown = runtime / "payload.bin"
        self._write_valid_runtime_file(unknown, b"preserve\n")
        (scripts / "workflow_runtime_contracts.py").unlink()
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_managed_legacy_unknown",
        )
        delete_calls = []
        native_delete_called = root / "native-delete-called"

        with mock.patch.object(
            janitor,
            "delete_managed_worktree",
            side_effect=lambda *args, **kwargs: delete_calls.append(args),
        ):
            code, payload = self.run_managed_janitor_module(
                janitor,
                root,
                worktree,
                cleanup_report="Legacy cleanup fixture.",
                native_delete_called=native_delete_called,
            )

        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("unknown_runtime_entry", payload["cleanup_report"])
        self.assertNotIn("payload.bin", payload["cleanup_report"])
        self.assertEqual(delete_calls, [])
        self.assertEqual(unknown.read_bytes(), b"preserve\n")
        self.assertEqual(
            {path.name for path in runtime.iterdir()},
            {"evidence-ledger.jsonl", "payload.bin"},
        )
        self.assertTrue(worktree.is_dir())

    def test_managed_legacy_runtime_admits_named_entries_before_native_delete(
        self,
    ) -> None:
        root, worktree, scripts, _ = self.make_managed_worktree(with_v2=False)
        runtime = worktree / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        self._write_valid_runtime_file(
            runtime / "evidence-ledger.jsonl",
            b"legacy ledger\n",
        )
        self._write_valid_runtime_file(
            runtime / "fix-checkpoint.json",
            b"legacy checkpoint\n",
        )
        (scripts / "workflow_runtime_contracts.py").unlink()
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_managed_legacy_named",
        )
        delete_calls = []
        native_delete_called = root / "native-delete-called"

        def native_refusal(*args, **kwargs):
            delete_calls.append(args)
            return False, "injected native refusal", "native refused"

        with mock.patch.object(
            janitor,
            "delete_managed_worktree",
            side_effect=native_refusal,
        ):
            code, payload = self.run_managed_janitor_module(
                janitor,
                root,
                worktree,
                cleanup_report="Legacy cleanup fixture.",
                native_delete_called=native_delete_called,
            )

        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("injected native refusal", payload["cleanup_report"])
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(
            {path.name: path.read_bytes() for path in runtime.iterdir()},
            {
                "evidence-ledger.jsonl": b"legacy ledger\n",
                "fix-checkpoint.json": b"legacy checkpoint\n",
            },
        )
        self.assertTrue(worktree.is_dir())

    def test_partial_native_retarget_refusal_stops_before_branch_cleanup(self) -> None:
        root, worktree, scripts, cleanup_marker = self.make_managed_worktree(
            with_v2=True,
        )
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_partial_retarget_refusal",
        )
        head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        remote_oid = self.run_git(root, "rev-parse", "refs/heads/TASK-1").stdout.strip()
        simulated_cleanup_target = {"effective_workdir": str(worktree)}
        deletion_calls = []

        def refuse_after_partial_retarget(*args, **kwargs):
            deletion_calls.append((args, kwargs))
            simulated_cleanup_target["effective_workdir"] = str(root)
            return (
                False,
                "Kent delete failed after partial Session retarget.",
                "Reconcile actual state before retrying.",
            )

        pr_url = "https://github.com/example/repo/pull/1"
        with (
            mock.patch.object(
                janitor,
                "github_pr",
                return_value={
                    "state": "MERGED",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "baseRefName": "main",
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                },
            ),
            mock.patch.object(
                janitor, "merged_pr_contains_head", return_value=True,
            ),
            mock.patch.object(
                janitor, "remote_branch_oid", return_value=remote_oid,
            ) as remote_branch_read,
            mock.patch.object(
                janitor, "cleanup_session_uses_workspace", return_value=False,
            ),
            mock.patch.object(
                janitor,
                "capture_kent_worktree_record",
                return_value={
                    "variant": "registered",
                    "root": str(worktree),
                    "worktree_id": "kent-test-record",
                },
            ),
            mock.patch.object(
                janitor, "_admit_managed_runtime_parent",
                return_value=(True, "runtime parent admitted"),
            ),
            mock.patch.object(
                janitor,
                "delete_managed_worktree",
                side_effect=refuse_after_partial_retarget,
            ),
            mock.patch.object(janitor, "settle_local_branch") as local_cleanup,
        ):
            code, payload = self.run_managed_janitor_module(
                janitor,
                root,
                worktree,
                cleanup_report=cleanup_marker,
                native_delete_called=root / "native-delete-called",
                cleanup_mode="merged",
                pr_url=pr_url,
            )

        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["transition"], "task_janitor_blocked", payload)
        self.assertIn("partial Session retarget", payload["cleanup_report"])
        self.assertEqual(len(deletion_calls), 1)
        self.assertEqual(
            simulated_cleanup_target["effective_workdir"], str(root),
        )
        local_cleanup.assert_not_called()
        remote_branch_read.assert_called_once()
        self.assertEqual(
            remote_branch_read.call_args.args,
            (root.resolve(), "TASK-1"),
        )
        self.assertEqual(
            remote_branch_read.call_args.kwargs["expected_repository"],
            ("example", "repo"),
        )
        self.assertTrue(worktree.is_dir())
        self.assertIn(
            str(worktree),
            self.run_git(root, "worktree", "list", "--porcelain").stdout,
        )
        self.assertEqual(
            self.run_git(root, "rev-parse", "refs/heads/TASK-1").stdout.strip(),
            remote_oid,
        )
        self.assertIn(
            "refs/heads/TASK-1",
            self.run_fixture_git(
                root, "ls-remote", "--heads", "origin", "TASK-1",
            ).stdout,
        )

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
        self._write_valid_runtime_file(runtime / "fix-checkpoint.json", "{}")
        self._write_valid_runtime_file(runtime / "evidence-ledger.jsonl", "{}\n")
        neighbor = root / ".kent" / "runtime" / "TASK-2"
        neighbor.mkdir()
        neighbor_state = neighbor / "fix-checkpoint.json"
        neighbor_bytes = b"neighbor task state\n"
        self._write_valid_runtime_file(neighbor_state, neighbor_bytes)
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
        self.assertEqual(neighbor_state.read_bytes(), neighbor_bytes)

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
        sentinel = runtime_dir / (
            ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
        )
        self._write_valid_runtime_file(sentinel)
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
                    self._write_valid_runtime_file(tombstone / entry, "{}\n")
                sentinel = runtime_dir / (
                    ".evidence-terminal-"
                    + hashlib.sha256(b"TASK-1").hexdigest()
                )
                self._write_valid_runtime_file(sentinel)
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
                tombstone_identity = None
                if name == "ledger":
                    self._write_valid_runtime_file(
                        task / "fix-checkpoint.json",
                        "{}\n",
                    )
                    self._write_valid_runtime_file(
                        task / "smoke-checkpoint.json",
                        "{}\n",
                    )
                fired = False
                runtime_calls = 0

                def injected(fd: int) -> None:
                    nonlocal fired, runtime_calls, tombstone_identity
                    nonlocal tombstone_path
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
                                tombstone_metadata = tombstone_path.stat()
                                tombstone_identity = (
                                    tombstone_metadata.st_dev,
                                    tombstone_metadata.st_ino,
                                )
                        matches = (
                            tombstone_path is not None
                            and tombstone_identity
                            == (metadata.st_dev, metadata.st_ino)
                            and tombstone_path.is_dir()
                            and not any(tombstone_path.iterdir())
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
                        self._write_valid_runtime_file(
                            tombstone / entry,
                            "{}\n",
                        )
                sentinel = runtime_dir / (
                    ".evidence-terminal-"
                    + hashlib.sha256(b"TASK-1").hexdigest()
                )
                self._write_valid_runtime_file(sentinel)
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

    def test_v2_tombstone_preserves_unknown_entries_and_blocks(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        active = runtime_dir / "TASK-1"
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_opaque_entry_test",
        )
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        opaque = {
            "plan-contract.json": b'{"opaque":true}\n',
            "payload.bin": b"\x00\xffopaque\n",
            ".tmp-producer-7": b"temporary",
        }
        for name, content in opaque.items():
            self._write_valid_runtime_file(active / name, content)
        active.rename(tombstone)
        self._write_valid_runtime_file(
            runtime_dir / (
                ".evidence-terminal-" + hashlib.sha256(b"TASK-1").hexdigest()
            ),
        )
        before = {name: (tombstone / name).read_bytes() for name in opaque}
        result = subprocess.run(
            [str(scripts / "workflow-task-janitor")],
            input=self.janitor_input(root, cleanup_report=marker_line),
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
        self.assertTrue(tombstone.is_dir())
        self.assertEqual(
            {name: (tombstone / name).read_bytes() for name in opaque},
            before,
        )

    def test_v2_managed_retention_blocks_unknown_entries_without_mutation(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        active = root / ".kent" / "runtime" / "TASK-1"
        opaque = {
            "plan-contract.json": b'{"opaque":true}\n',
            "payload.bin": b"\x00\xffopaque\n",
            ".tmp-producer-7": b"temporary",
        }
        for name, content in opaque.items():
            self._write_valid_runtime_file(active / name, content)
        before = {name: (active / name).read_bytes() for name in opaque}
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_managed_opaque_test",
        )
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime_dir = root / ".kent" / "runtime"
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_managed_opaque_test",
        )
        result = janitor._prepare_v2_managed_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertEqual(
            result[0],
            False,
            result,
        )
        self.assertTrue(active.is_dir())
        self.assertEqual(
            {name: (active / name).read_bytes() for name in opaque},
            before,
        )
        self.assertFalse(tombstone.exists())

    def test_v2_managed_admission_does_not_write_runtime_bytecode(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        self.run_git(
            root,
            "add",
            ".kent/scripts/workflow-evidence-ledger",
            ".kent/scripts/workflow-task-janitor",
            ".kent/scripts/workflow_runtime_contracts.py",
        )
        self.run_git(root, "commit", "-q", "-m", "Track runtime commands")
        cache = scripts / "__pycache__"
        if cache.exists():
            for bytecode in cache.glob("*.pyc"):
                bytecode.unlink()
            if not any(cache.iterdir()):
                cache.rmdir()
        before_status = self.run_git(
            root,
            "status",
            "--porcelain",
            "--untracked-files=all",
        ).stdout
        self.assertEqual(before_status, "")
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        command = (
            "import json, sys; from pathlib import Path; "
            "from importlib.machinery import SourceFileLoader; "
            "from importlib.util import cache_from_source, module_from_spec, spec_from_loader; "
            "assert not sys.dont_write_bytecode; "
            "path = Path(sys.argv[1]); "
            "loader = SourceFileLoader('janitor_bytecode_test', str(path)); "
            "spec = spec_from_loader(loader.name, loader); "
            "module = module_from_spec(spec); loader.exec_module(module); "
            "cache = Path(cache_from_source(str(path))); cache.unlink(missing_ok=True); "
            "cache.parent.rmdir() if cache.parent.is_dir() and not any(cache.parent.iterdir()) else None; "
            "result = module._prepare_v2_managed_runtime_state("
            "Path(sys.argv[2]), 'TASK-1', cleanup_report=sys.argv[3]); "
            "print(json.dumps(result))"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                command,
                str(scripts / "workflow-task-janitor"),
                str(root),
                marker_line,
            ],
            cwd=root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)[0], result.stdout)
        self.assertEqual(
            self.run_git(
                root,
                "status",
                "--porcelain",
                "--untracked-files=all",
            ).stdout,
            before_status,
        )

    def test_v2_checkpoint_payload_is_not_read_and_metadata_drift_blocks(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        active = root / ".kent" / "runtime" / "TASK-1"
        sparse = active / "fix-checkpoint.json"
        descriptor = os.open(
            sparse,
            os.O_RDWR | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.ftruncate(descriptor, 16 * 1024 * 1024 + 1)
            sparse_inode = os.fstat(descriptor).st_ino
        finally:
            os.close(descriptor)
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_checkpoint_unread_test",
        )
        real_read = janitor.os.read

        def reject_opaque_read(fd: int, size: int) -> bytes:
            if janitor.os.fstat(fd).st_ino == sparse_inode:
                raise AssertionError("Janitor read checkpoint payload bytes")
            return real_read(fd, size)

        with mock.patch.object(janitor.os, "read", side_effect=reject_opaque_read):
            outcome = janitor._remove_v2_runtime_state(
                root.resolve(),
                "TASK-1",
                cleanup_report=marker_line,
            )
        self.assertTrue(outcome[0], outcome)
        self.assertFalse(active.exists())

        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        active = root / ".kent" / "runtime" / "TASK-1"
        checkpoint = active / "smoke-checkpoint.json"
        self._write_valid_runtime_file(checkpoint, b"{}")
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_checkpoint_metadata_drift_test",
        )

        def mutate_metadata(phase: str) -> None:
            if phase == "after_evidence_admission":
                metadata = checkpoint.stat()
                os.utime(
                    checkpoint,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
                )

        outcome = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
            _phase_hook=mutate_metadata,
        )
        self.assertFalse(outcome[0], outcome)
        self.assertTrue(active.exists())
        self.assertTrue(checkpoint.exists())

    def test_v2_checkpoint_partial_unlink_failure_retries(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        active = root / ".kent" / "runtime" / "TASK-1"
        checkpoints = {
            "fix-checkpoint.json": b'{"fix":true}\n',
            "smoke-checkpoint.json": b'{"smoke":true}\n',
        }
        for name, content in checkpoints.items():
            self._write_valid_runtime_file(active / name, content)
        runtime = load_template_module(
            REPO_ROOT / "workflowkit" / "runtime.py",
            "runtime_checkpoint_retry_test",
        )
        marker = json.loads(marker_line.removeprefix("TERMINAL_EVIDENCE_V1 "))
        runtime_dir = root / ".kent" / "runtime"
        tombstone = runtime_dir / (
            ".evidence-cleanup-" + runtime.canonical_sha256(marker)
        )
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_checkpoint_retry_test",
        )
        fired = False

        def fail_after_fix_checkpoint(phase: str) -> None:
            nonlocal fired
            if phase == "after_fix_checkpoint_unlink_fsync" and not fired:
                fired = True
                raise OSError("checkpoint unlink failure")

        first = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
            _phase_hook=fail_after_fix_checkpoint,
        )
        self.assertFalse(first[0], first)
        self.assertTrue(fired)
        self.assertTrue(tombstone.exists())
        self.assertFalse((tombstone / "fix-checkpoint.json").exists())
        self.assertTrue((tombstone / "smoke-checkpoint.json").exists())
        self.assertTrue((tombstone / "evidence-ledger.jsonl").exists())
        retry = janitor._remove_v2_runtime_state(
            root.resolve(),
            "TASK-1",
            cleanup_report=marker_line,
        )
        self.assertTrue(retry[0], retry)
        self.assertFalse(tombstone.exists())

    def test_legacy_cleanup_blocks_unknown_files_without_retirement(
        self,
    ) -> None:
        root = self.create_repository()
        runtime = root / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        for name, content in {
            "plan-contract.json": b'{"opaque":true}\n',
            "payload.bin": b"\x00\xffopaque\n",
            ".tmp-producer-7": b"temporary",
            "evidence-ledger.jsonl": b"{}\n",
        }.items():
            self._write_valid_runtime_file(runtime / name, content)
        legacy_dir = root / "legacy-scripts"
        legacy_dir.mkdir()
        legacy_script = legacy_dir / "workflow-task-janitor"
        shutil.copyfile(
            JANITOR,
            legacy_script,
        )
        legacy_script.chmod(0o755)
        janitor = load_template_module(legacy_script, "legacy_opaque_test")
        before = {
            path.name: path.read_bytes()
            for path in runtime.iterdir()
        }
        outcome = janitor.remove_runtime_state(
            root.resolve(),
            "TASK-1",
        )
        self.assertFalse(outcome[0], outcome)
        self.assertTrue(runtime.is_dir())
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in runtime.iterdir()
            },
            before,
        )

    def test_legacy_cleanup_removes_named_entries_with_ledger_last(self) -> None:
        root = self.create_repository()
        runtime = root / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        self._write_valid_runtime_file(
            runtime / "evidence-ledger.jsonl",
            b"legacy ledger\n",
        )
        self._write_valid_runtime_file(
            runtime / "fix-checkpoint.json",
            b"legacy checkpoint\n",
        )
        legacy_dir = root / "legacy-scripts"
        legacy_dir.mkdir()
        legacy_script = legacy_dir / "workflow-task-janitor"
        shutil.copyfile(JANITOR, legacy_script)
        legacy_script.chmod(0o755)
        janitor = load_template_module(
            legacy_script,
            "legacy_named_cleanup_test",
        )
        phases: list[str] = []

        outcome = janitor.remove_runtime_state(
            root.resolve(),
            "TASK-1",
            _phase_hook=phases.append,
        )

        self.assertTrue(outcome[0], outcome)
        self.assertFalse(runtime.exists())
        self.assertLess(
            phases.index("after_fix_checkpoint_unlink_fsync"),
            phases.index("after_ledger_unlink_fsync"),
        )

    def test_legacy_cleanup_rejects_ci_prefix_as_opaque(self) -> None:
        root = self.create_repository()
        runtime = root / ".kent" / "runtime" / "TASK-1"
        runtime.mkdir(parents=True)
        for name, content in {
            "ci-report-fake.json": b"{}",
            "evidence-ledger.jsonl": b"{}\n",
        }.items():
            self._write_valid_runtime_file(runtime / name, content)
        legacy_dir = root / "legacy-scripts"
        legacy_dir.mkdir()
        legacy_script = legacy_dir / "workflow-task-janitor"
        shutil.copyfile(
            JANITOR,
            legacy_script,
        )
        legacy_script.chmod(0o755)
        janitor = load_template_module(legacy_script, "legacy_ci_prefix_test")
        outcome = janitor.remove_runtime_state(root.resolve(), "TASK-1")
        self.assertFalse(outcome[0], outcome)
        self.assertTrue(runtime.exists())
        self.assertTrue((runtime / "ci-report-fake.json").exists())

    def test_legacy_cleanup_rejects_unsafe_known_entries(self) -> None:
        for case in (
            "unsafe",
            "hardlink",
            "tracked",
            "not_ignored",
            "fifo",
            "directory",
            "symlink",
        ):
            with self.subTest(case=case):
                root = self.create_repository()
                runtime = root / ".kent" / "runtime" / "TASK-1"
                runtime.mkdir(parents=True)
                entry = runtime / "fix-checkpoint.json"
                self._write_valid_runtime_file(entry, b"opaque")
                if case == "unsafe":
                    entry.chmod(0o644)
                elif case == "hardlink":
                    os.link(entry, root / "legacy-hard-link")
                elif case == "tracked":
                    self.run_git(
                        root,
                        "add",
                        "-f",
                        str(entry.relative_to(root)),
                    )
                    self.run_git(
                        root,
                        "commit",
                        "-q",
                        "-m",
                        "track legacy entry",
                    )
                elif case == "not_ignored":
                    (root / ".gitignore").write_text("")
                    self.run_git(root, "add", ".gitignore")
                    self.run_git(
                        root,
                        "commit",
                        "-q",
                        "-m",
                        "unignore legacy entry",
                    )
                elif case == "fifo":
                    entry.unlink()
                    os.mkfifo(entry, 0o600)
                elif case == "directory":
                    entry.unlink()
                    entry.mkdir(mode=0o700)
                elif case == "symlink":
                    entry.unlink()
                    entry.symlink_to(root / "tracked.txt")
                self._write_valid_runtime_file(
                    runtime / "evidence-ledger.jsonl",
                    b"{}\n",
                )
                legacy_dir = root / "legacy-scripts"
                legacy_dir.mkdir()
                legacy_script = legacy_dir / "workflow-task-janitor"
                shutil.copyfile(
                    JANITOR,
                    legacy_script,
                )
                legacy_script.chmod(0o755)
                janitor = load_template_module(
                    legacy_script,
                    "legacy_unsafe_{}".format(case),
                )
                outcome = janitor.remove_runtime_state(
                    root.resolve(),
                    "TASK-1",
                )
                self.assertFalse(outcome[0], outcome)
                if case in {"tracked", "not_ignored"}:
                    self.assertIn("tracked_or_not_ignored", outcome[1])
                else:
                    self.assertIn("unsafe_runtime_entry", outcome[1])
                self.assertNotIn("fix-checkpoint.json", outcome[1])
                self.assertNotIn("unexpected runtime entries", outcome[1])
                self.assertTrue(runtime.exists())
                self.assertTrue(entry.exists())

    def test_v2_entry_safety_matrix_blocks_without_unlink(self) -> None:
        for case in (
            "unsafe", "hardlink", "tracked", "not_ignored",
            "fifo", "directory", "symlink",
        ):
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
                self._write_valid_runtime_file(
                    runtime_dir
                    / (
                        ".evidence-terminal-"
                        + hashlib.sha256(b"TASK-1").hexdigest()
                    ),
                    b"",
                )
                entry = tombstone / "fix-checkpoint.json"
                self._write_valid_runtime_file(entry, b"{}\n")
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
                elif case == "fifo":
                    entry.unlink()
                    os.mkfifo(entry, 0o600)
                elif case == "directory":
                    entry.unlink()
                    entry.mkdir(mode=0o700)
                elif case == "symlink":
                    entry.unlink()
                    entry.symlink_to(root / "tracked.txt")
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
                self.assertNotIn("unexpected tombstone entries", outcome[1])
                if case in {"tracked", "not_ignored"}:
                    self.assertIn("tracked_or_not_ignored", outcome[1])
                else:
                    self.assertIn("unsafe_runtime_entry", outcome[1])
                self.assertNotIn("fix-checkpoint.json", outcome[1])
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

    @mock.patch.dict(os.environ, EVIDENCE_FIXTURE_OWNER)
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
        self.assertIn("sealed", append.stderr)
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


    def test_v2_primary_active_conflict_matrix_blocks_without_deletion(self) -> None:
        for case in ("sentinel", "tombstone", "invalid", "unsealed"):
            with self.subTest(case=case):
                root = self.create_repository()
                scripts, marker_line = self.make_v2_terminal_state(root)
                runtime_dir = root / ".kent" / "runtime"
                task = runtime_dir / "TASK-1"
                if case == "sentinel":
                    sentinel = (
                        runtime_dir
                        / (
                            ".evidence-terminal-"
                            + hashlib.sha256(b"TASK-1").hexdigest()
                        )
                    )
                    self._write_valid_runtime_file(sentinel, b"")
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
                if case == "sentinel":
                    self.assertEqual(sentinel.read_bytes(), b"")

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
        self._write_valid_runtime_file(sentinel)
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
        self._write_valid_runtime_file(sentinel)
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

    def test_managed_retention_rejects_wrong_tombstone_with_opaque_entry(self) -> None:
        root = self.create_repository()
        scripts, marker_line = self.make_v2_terminal_state(root)
        runtime_dir = root / ".kent" / "runtime"
        tombstone = runtime_dir / ".evidence-cleanup-wrong"
        (runtime_dir / "TASK-1").rename(tombstone)
        self._write_valid_runtime_file(tombstone / "opaque-entry", "preserve\n")
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
            (tombstone / "opaque-entry").read_text(),
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
                self._write_valid_runtime_file(entry, b"{}\n")
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
                self.assertIn(
                    "unsafe_runtime_entry" if state == "unsafe"
                    else "tracked_or_not_ignored",
                    result[1],
                )
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
        self.assertIn(
            "could not admit the exact attached branch identity",
            payload["cleanup_report"],
        )
        self.assertIn(
            "attached-branch readback exactly matching",
            payload["blocker_reason"],
        )
        self.assertTrue(worktree.exists())

    def test_exact_merged_pr_invokes_kent_worktree_deletion(self) -> None:
        root = self.create_repository()
        remote_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(remote_temporary.cleanup)
        remote = Path(remote_temporary.name)
        self.run_git(remote, "init", "--bare", "-q")
        self.run_git(root, "remote", "add", "origin", str(remote))
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        self.run_git(
            root,
            "push",
            "-q",
            "origin",
            "refs/heads/TASK-1:refs/heads/TASK-1",
        )
        self.set_fixture_github_origin(root, remote)

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "baseRefName": "main",
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
        self.add_origin_branch(root, worktree)
        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "baseRefName": "main",
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
        fake_wrapper = self.completed_wrapper(root)
        fake_wrapper.write_text(
            fake_wrapper.read_text().replace(
                "git -C \"$KENT_TEST_PRIMARY\" worktree remove --force \"$target\"\n",
                "git -C \"$KENT_TEST_PRIMARY\" worktree remove --force \"$target\"\n"
                "mv \"$KENT_TEST_PRIMARY\" \"$KENT_TEST_PRIMARY.old\"\n"
                "git init -q \"$KENT_TEST_PRIMARY\"\n",
                1,
            )
        )
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
        self.add_origin_branch(root, worktree)

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "baseRefName": "main",
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
        # The admission lookup retries twice, then five fresh PR witnesses
        # each perform one read before their associated cleanup effects.
        self.assertEqual(int(attempts.read_text().strip()), 8)
        self.assertTrue(wrapper_marker.exists())

    def test_merged_pr_lookup_failure_returns_to_cleanup(self) -> None:
        root = self.create_repository()
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        self.add_origin_branch(root, worktree)

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
        self.assertIn(
            "merged PR evidence lookup failed; no deletion claim is made",
            payload["cleanup_report"],
        )
        self.assertIn(
            "Provider, PR-target, or repository-route evidence",
            payload["blocker_reason"],
        )
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
        self.add_origin_branch(root, worktree)

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "baseRefName": "main",
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
        self.add_origin_branch(root, worktree)

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "baseRefName": "main",
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
        fake_wrapper = self.completed_wrapper(root)
        fake_wrapper.write_text(
            fake_wrapper.read_text().replace(
                "for argument in \"$@\"; do target=\"$argument\"; done\n",
                "printf '%s\\n' "
                "'{\"kind\":\"scheduled\",\"scheduled\":"
                "{\"operation_id\":\"operation-test\"}}'\n"
                "exit 0\n",
                1,
            )
        )

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
        self.add_origin_branch(root, worktree)

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "baseRefName": "main",
                    "isCrossRepository": False,
                    "url": "https://github.com/example/repo/pull/1",
                }
            )
        )
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\ncat \"$KENT_TEST_PR_STATE\"\n")
        fake_gh.chmod(0o755)
        fake_wrapper = self.completed_wrapper(root)
        fake_wrapper.write_text(
            fake_wrapper.read_text().replace(
                "for argument in \"$@\"; do target=\"$argument\"; done\n",
                "printf '%s\\n' "
                "'{\"kind\":\"completed\",\"completed\":"
                "{\"cleanup\":{\"kind\":\"retained\"}}}'\n"
                "exit 0\n",
                1,
            )
        )

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
        self.add_origin_branch(root, worktree)
        self.run_fixture_git(
            root,
            "push",
            "-q",
            "origin",
            f"{pr_head}:refs/heads/TASK-1",
        )
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
                    "baseRefName": "main",
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
        self.add_origin_branch(root, worktree)

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": pr_head,
                    "baseRefName": "main",
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

    def test_initially_absent_merged_remote_requires_post_delete_readback(self) -> None:
        for outcome in (
            "absent", "appeared", "lookup_error",
            "lease_lookup_error", "lease_retained", "lease_failure",
        ):
            with self.subTest(outcome=outcome):
                root, worktree, _, _ = self.make_managed_worktree(with_v2=False)
                head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
                remote = Path(self.run_git(
                    root, "config", "--get", "kent.test.githubBareRemote",
                ).stdout.strip())
                if not outcome.startswith("lease_"):
                    self.run_git(remote, "update-ref", "-d", "refs/heads/TASK-1")
                wrapper = self.completed_wrapper(root)
                environment = self.managed_pr_environment(root, worktree, wrapper=wrapper)
                # Change only the isolated remote after native worktree deletion.
                effect = {
                    "absent": ":",
                    "appeared": 'git -C "$KENT_TEST_REMOTE" update-ref refs/heads/TASK-1 "$KENT_TEST_HEAD"',
                    "lookup_error": 'mv "$KENT_TEST_REMOTE" "$KENT_TEST_REMOTE.offline"',
                }.get(outcome, ":")
                wrapper.write_text(wrapper.read_text().replace(
                    'touch "$KENT_TEST_PRIMARY/.kent-test-delete-done"\n',
                    'touch "$KENT_TEST_PRIMARY/.kent-test-delete-done"\n' + effect + "\n",
                ))
                environment.update(KENT_TEST_REMOTE=str(remote), KENT_TEST_HEAD=head)
                if outcome.startswith("lease_"):
                    bin_dir = root / "fake-bin"
                    bin_dir.mkdir()
                    git = bin_dir / "git"
                    git.write_text(
                        "#!/bin/sh\n"
                        "for arg in \"$@\"; do\n"
                        "  if [ \"$arg\" = :refs/heads/TASK-1 ]; then\n"
                        + {
                            "lease_failure": "    exit 1\n",
                            "lease_retained": "    exit 0\n",
                            "lease_lookup_error":
                                '    "$KENT_TEST_REAL_GIT" "$@" || exit $?\n'
                                '    mv "$KENT_TEST_REMOTE" "$KENT_TEST_REMOTE.offline"\n'
                                "    exit 0\n",
                        }[outcome]
                        + "  fi\n"
                        "done\n"
                        'exec "$KENT_TEST_REAL_GIT" "$@"\n'
                    )
                    git.chmod(0o755)
                    environment.update(
                        KENT_TEST_REAL_GIT=str(self._git_shim_path),
                        PATH=str(bin_dir) + os.pathsep + environment["PATH"],
                    )
                try:
                    result = subprocess.run(
                        [str(JANITOR)], cwd=worktree,
                        input=self.janitor_input(
                            worktree, branch_name="TASK-1", cleanup_mode="merged",
                            pr_url="https://github.com/example/repo/pull/1",
                            merge_report="merged",
                        ),
                        text=True, capture_output=True, env=environment, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout)
                    self.assertFalse(worktree.exists())
                    self.assertNotIn(str(worktree), self.run_git(
                        root, "worktree", "list", "--porcelain",
                    ).stdout)
                    self.assertNotEqual(subprocess.run(
                        ["git", "-C", str(root), "show-ref", "--verify", "refs/heads/TASK-1"],
                        capture_output=True, check=False,
                    ).returncode, 0)
                    if outcome == "absent":
                        self.assertEqual(payload["transition"], "task_janitor_done")
                        self.assertIn("Remote branch removed: true", payload["cleanup_report"])
                    else:
                        self.assertEqual(payload["transition"], "task_janitor_blocked")
                        self.assertNotIn("Remote branch removed: true", payload["cleanup_report"])
                        self.assertIn(
                            {
                                "appeared": "changed",
                                "lookup_error": "readback failed",
                                "lease_lookup_error": "postcondition is unknown",
                                "lease_retained": "remained after exact lease",
                                "lease_failure": "uncertain deletion acknowledgement",
                            }[outcome],
                            payload["blocker_reason"],
                        )
                    if outcome in ("appeared", "lease_retained", "lease_failure"):
                        self.assertEqual(self.run_git(
                            remote, "rev-parse", "refs/heads/TASK-1",
                        ).stdout.strip(), head)
                finally:
                    offline = remote.with_name(remote.name + ".offline")
                    if offline.exists():
                        offline.rename(remote)

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
        self.set_fixture_github_origin(root, remote)

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": head,
                    "baseRefName": "main",
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
        self.set_fixture_github_origin(root, remote)

        pr_state = root / "pr-state.json"
        pr_state.write_text(
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T00:00:00Z",
                    "headRefName": "TASK-1",
                    "headRefOid": pr_head,
                    "baseRefName": "main",
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
        self.set_fixture_github_origin(root, remote)
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
        remote_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(remote_temporary.cleanup)
        remote = Path(remote_temporary.name)
        self.run_git(remote, "init", "--bare", "-q")
        self.run_git(root, "remote", "add", "origin", str(remote))
        worktrees = root / ".kent" / "worktrees"
        worktrees.mkdir(parents=True)
        worktree = worktrees / "TASK-1"
        self.run_git(root, "worktree", "add", "-q", "-b", "TASK-1", str(worktree))
        task_head = self.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        self.run_git(
            root,
            "push",
            "-q",
            "origin",
            "refs/heads/TASK-1:refs/heads/TASK-1",
        )
        self.set_fixture_github_origin(root, remote)
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
                    "baseRefName": "main",
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
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("OID changed", payload["cleanup_report"])
        self.assertEqual(
            self.run_git(root, "rev-parse", "refs/heads/TASK-1").stdout.strip(),
            new_oid,
        )


if __name__ == "__main__":
    unittest.main()
