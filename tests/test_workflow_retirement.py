from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import workflowkit.operations as operations
from workflowkit.operations import (
    EffectBlocked,
    JournalError,
    OperationJournal,
    PlanValidationError,
    canonical_bytes,
    load_plan,
    recover_effect,
    run_effect,
)


PROJECT_ID = "123e4567-e89b-12d3-a456-426614174001"
PYTHON = str(Path(sys.executable).resolve())


def plan_file(root: Path, value: dict, name: str = "plan.json") -> tuple[Path, str]:
    path = root / name
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def load_test_plan(root: Path) -> operations.LoadedPlan:
    path, digest = plan_file(
        root,
        {"schema": "effect-test-plan-v1", "test": True},
        "effect-plan.json",
    )
    return load_plan(
        path,
        schema="effect-test-plan-v1",
        expected_sha256=digest,
    )


def write_fake_kent(root: Path, state: dict) -> tuple[Path, Path, Path]:
    state_path = root / "kent-state.json"
    log_path = root / "kent-argv.jsonl"
    executable = root / "fake-kent"
    state_path.write_text(json.dumps(state, sort_keys=True))
    source = f"""#!{PYTHON}
import json
from pathlib import Path
import sys

STATE_PATH = Path({str(state_path)!r})
LOG_PATH = Path({str(log_path)!r})
args = sys.argv[1:]
with LOG_PATH.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args, separators=(",", ":")) + "\\n")
state = json.loads(STATE_PATH.read_text())


def save():
    STATE_PATH.write_text(json.dumps(state, sort_keys=True))


def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def execution_target_policy(value):
    if value.startswith("ref:"):
        return {{"mode": "custom_ref", "custom_ref": value[4:]}}
    if value == "ask-on-first-execution":
        return {{"mode": "ask_on_first_execution"}}
    if value == "default-branch":
        return {{"mode": "default_branch"}}
    return {{"mode": value}}


def emit(rows, key):
    offset = int(option("--offset", "0"))
    limit = int(option("--limit", "100"))
    page = rows[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(rows) else None
    if state.get("incomplete_sessions") and args[:2] == ["task", "sessions"]:
        next_offset = offset + len(page) + 1
    print(json.dumps({{key: page, "next_offset": next_offset}}, sort_keys=True))


def workflow_for_task(task_id):
    for workflow in state["workflows"].values():
        if task_id in workflow.get("details", {{}}):
            return workflow
    raise KeyError(task_id)


if args == ["project", "list"]:
    print(state["project_id"] + "\\tTest Project\\t" + state["project_root"])
elif args[:2] == ["workflow", "list"]:
    rows = []
    for workflow_id, workflow in sorted(state["workflows"].items()):
        if workflow.get("present", True):
            rows.append({{
                "id": workflow_id,
                "name": workflow["metadata"]["name"],
                "description": workflow["metadata"]["description"],
                "version": workflow["version"],
                "execution_target_policy": execution_target_policy(workflow["metadata"]["execution_target"]),
                "project_link": {{"default": workflow["default"]}},
            }})
    emit(rows, "workflows")
elif args == ["worktree", "list", "--json"]:
    print(json.dumps({{"worktrees": state.get("worktrees", [])}}, sort_keys=True))
elif args[:2] == ["workflow", "inspect"]:
    assert args[3:] == ["--summary", "--json"], "summary invocation required"
    workflow_id = args[2]
    workflow = state["workflows"][workflow_id]
    if not workflow.get("present", True):
        print("workflow not found", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({{
        "id": workflow_id,
        "name": workflow["metadata"]["name"],
        "description": workflow["metadata"]["description"],
        "version": workflow["version"],
        "execution_target_policy": execution_target_policy(workflow["metadata"]["execution_target"]),
    }}, sort_keys=True))
elif args[:3] == ["workflow", "graph", "inspect"]:
    workflow_id = args[3]
    workflow = state["workflows"][workflow_id]
    print(json.dumps({{
        "workflow_id": workflow_id,
        "expected_version": workflow["version"],
        "graph": workflow["graph"],
    }}, sort_keys=True))
elif args[:2] == ["workflow", "validate"]:
    print(json.dumps(state.get("validation", {{"valid": True}}), sort_keys=True))
elif args[:2] == ["task", "list"]:
    workflow_id = option("--workflow")
    workflow = state["workflows"][workflow_id]
    rows = workflow.get("tasks", []) if workflow.get("present", True) else []
    emit(rows, "tasks")
elif args[:2] == ["task", "show"]:
    task_id = args[2]
    print(json.dumps(workflow_for_task(task_id)["details"][task_id], sort_keys=True))
elif args[:2] == ["task", "sessions"]:
    task_id = args[2]
    rows = workflow_for_task(task_id).get("sessions", {{}}).get(task_id, [])
    emit(rows, "items")
elif args[:3] == ["workflow", "graph", "apply"]:
    document = json.loads(sys.stdin.read())
    workflow = state["workflows"][document["workflow_id"]]
    if document["expected_version"] != workflow["version"]:
        print("version mismatch", file=sys.stderr)
        raise SystemExit(1)
    state["last_graph_document"] = document
    if not state.get("graph_noop"):
        workflow["version"] += 1
        workflow["graph"] = document["graph"]
    save()
    print(json.dumps({{"version": workflow["version"]}}))
elif args[:2] == ["workflow", "update"]:
    workflow_id = args[2]
    workflow = state["workflows"][workflow_id]
    workflow["metadata"] = {{
        "name": option("--name"),
        "description": option("--description"),
        "execution_target": option("--execution-target"),
    }}
    state["last_update_argv"] = args
    save()
    print(json.dumps({{"updated": True}}))
else:
    print("unsupported fake Kent argv: " + repr(args), file=sys.stderr)
    raise SystemExit(2)
"""
    executable.write_text(source)
    executable.chmod(0o755)
    return executable, state_path, log_path

def workflow_id(index: int) -> str:
    return f"123e4567-e89b-12d3-a456-{426614174000 + index:012d}"

def make_workflow_fixture(root: Path, count: int = 1) -> dict:
    """Shared fake Kent inventory for canonical reconciliation tests."""
    session_root = root / "sessions"
    session_root.mkdir()
    workflows = {}
    for index in range(count):
        wid = workflow_id(index)
        task_id = f"TASK-{index + 1}"
        session_id = f"SESSION-{index + 1}"
        session_dir = session_root / session_id
        session_dir.mkdir()
        (session_dir / "events.jsonl").write_text(f'{{"session":{index + 1}}}\n')
        task_status = {"kind": "done", "native_state": "terminal"}
        workflows[wid] = {
            "present": True,
            "version": 1,
            "default": True,
            "metadata": {
                "name": f"Workflow {index + 1}",
                "description": "Retained workflow",
                "execution_target": "none",
            },
            "graph": {
                "nodes": [{"id": "terminal", "kind": "terminal"}],
                "edges": [],
                "node_groups": [],
                "transition_groups": [],
            },
            "tasks": [{"task_id": task_id, "status": task_status}],
            "details": {
                task_id: {
                    "summary": {"id": task_id, "done": True},
                    "status": task_status,
                    "current_nodes": [{"node_id": "terminal"}],
                    "pending_approvals": [],
                    "live_sessions": [],
                    "retained_session_count": 1,
                }
            },
            "sessions": {
                task_id: [{"session_id": session_id, "status": "idle"}]
            },
        }
    state = {
        "project_id": PROJECT_ID,
        "project_root": str(root),
        "workflows": workflows,
        "worktrees": [],
    }
    kent, state_path, log_path = write_fake_kent(root, state)
    return {
        "root": root,
        "kent": kent,
        "state_path": state_path,
        "log_path": log_path,
    }


def read_state(fixture: dict) -> dict:
    return json.loads(fixture["state_path"].read_text())


def write_state(fixture: dict, state: dict) -> None:
    fixture["state_path"].write_text(json.dumps(state, sort_keys=True))


def read_log(fixture: dict) -> list[list[str]]:
    if not fixture["log_path"].exists():
        return []
    return [json.loads(line) for line in fixture["log_path"].read_text().splitlines()]


class EffectResourceCapture:
    def __init__(self) -> None:
        self._pipe = os.pipe
        self._popen = subprocess.Popen
        self.effect_pipes: list[tuple[int, int]] = []
        self.guardians: list[subprocess.Popen] = []
        self.stream_fds: list[int] = []

    def pipe(self) -> tuple[int, int]:
        pair = self._pipe()
        if len(self.effect_pipes) < 2:
            self.effect_pipes.append(pair)
        return pair

    def popen(self, *args, **kwargs) -> subprocess.Popen:
        process = self._popen(*args, **kwargs)
        self.guardians.append(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                self.stream_fds.append(stream.fileno())
        return process

    def parent_fds(self) -> list[int]:
        return list(dict.fromkeys([fd for pair in self.effect_pipes for fd in pair] + self.stream_fds))


class EffectTransitionCapture(EffectResourceCapture):
    def __init__(self, release_mode: str = "delegate", close_after_release_error: bool = False) -> None:
        super().__init__()
        self._acknowledgement = operations._read_guardian_ack
        self._close = os.close
        self._terminate = operations._terminate_owned_during_setup
        self._write = os.write
        self.release_mode = release_mode
        self.close_after_release_error = close_after_release_error
        self.child_pids: list[int] = []
        self.release_attempts: list[tuple[int, bytes]] = []
        self.release_returns: list[int] = []
        self.close_errors = 0
        self.terminated_guardians: list[int] = []

    def acknowledgement(self, *args, **kwargs) -> int:
        child_pid = self._acknowledgement(*args, **kwargs)
        self.child_pids.append(child_pid)
        return child_pid

    def write(self, fd: int, data) -> int:
        control_write = self.effect_pipes[1][1] if len(self.effect_pipes) == 2 else None
        if fd == control_write and isinstance(data, bytes) and data == b"1":
            self.release_attempts.append((fd, data))
            if self.release_mode == "error":
                raise BrokenPipeError(errno.EPIPE, "injected gate release failure")
            if self.release_mode == "zero":
                self.release_returns.append(0)
                return 0
            result = self._write(fd, data)
            self.release_returns.append(result)
            return result
        return self._write(fd, data)

    def close(self, fd: int) -> None:
        control_write = self.effect_pipes[1][1] if len(self.effect_pipes) == 2 else None
        if (
            self.close_after_release_error
            and fd == control_write
            and self.release_returns == [1]
            and self.close_errors == 0
        ):
            self._close(fd)
            self.close_errors += 1
            raise OSError(errno.EIO, "injected close after confirmed release")
        self._close(fd)

    def terminate(self, process: subprocess.Popen, deadline: float) -> None:
        self.terminated_guardians.append(process.pid)
        self._terminate(process, deadline)


def assert_fds_closed(test: unittest.TestCase, fds: list[int]) -> None:
    for fd in fds:
        try:
            os.fstat(fd)
        except OSError as error:
            test.assertEqual(error.errno, errno.EBADF)
        else:
            test.fail(f'descriptor {fd} remains open')

class WorkflowRetirementTest(unittest.TestCase):
    def assert_recoverable_preimage(
        self,
        root: Path,
        plan: operations.LoadedPlan,
        effect_key: str,
        command: list[str],
        *,
        stdin_bytes: bytes | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        with OperationJournal(root / "state", "effect-test", plan) as journal:
            with mock.patch.object(operations.os, "kill", side_effect=AssertionError("PID signal")):
                settlement = recover_effect(
                    journal,
                    effect_key=effect_key,
                    command=command,
                    cwd=root,
                    stdin_bytes=stdin_bytes,
                    extra_env=extra_env,
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    current_sha256=lambda: "a" * 64,
                )
        self.assertEqual(settlement, "preimage")

    def assert_pre_release_cleanup(
        self,
        capture: EffectTransitionCapture,
        journal: OperationJournal,
        effect_key: str,
        marker: Path,
    ) -> None:
        self.assertEqual(len(capture.guardians), 1)
        self.assertIsNotNone(capture.guardians[0].poll())
        self.assertEqual(capture.terminated_guardians, [capture.guardians[0].pid])
        self.assertEqual(len(capture.child_pids), 1)
        self.assertFalse(operations._pid_alive(capture.child_pids[0]))
        assert_fds_closed(self, capture.parent_fds())
        entry = journal.state["effects"][effect_key]
        self.assertEqual(entry["status"], "unresolved")
        self.assertIsNone(entry["child"])
        self.assertFalse(marker.exists())

    def test_journal_lock_temp_and_noncanonical_readback_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_test_plan(root)
            with OperationJournal(root / "state", "effect-test", plan) as first:
                with self.assertRaises(JournalError):
                    with OperationJournal(root / "state", "other", plan):
                        pass
                first.temp_path.write_text("stale")
                with self.assertRaises(JournalError):
                    first.persist({"phase": "prepared", "effects": {}})
            state_dir = root / "malformed"
            state_dir.mkdir(mode=0o700)
            journal = OperationJournal(state_dir, "effect-test", plan)
            journal.path.write_text(
                json.dumps(
                    {
                        "schema": operations.JOURNAL_SCHEMA,
                        "operation": "effect-test",
                        "plan_sha256": plan.sha256,
                        "phase": "prepared",
                    },
                    indent=2,
                )
            )
            with self.assertRaises(JournalError):
                with journal:
                    pass

    def test_child_starts_after_durable_identity_and_inherits_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                script = textwrap.dedent(
                    """
                    import json, os
                    from pathlib import Path
                    state = json.loads(Path(os.environ["JOURNAL_PATH"]).read_text())
                    child = state["effects"]["gate"]["child"]
                    assert child["child_pid"] == os.getpid()
                    os.fstat(int(os.environ["INHERITED_LOCK_FD"]))
                    Path(os.environ["MARKER"]).write_text("started")
                    """
                )
                result = run_effect(
                    journal,
                    effect_key="gate",
                    command=[PYTHON, "-c", script],
                    cwd=root,
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    extra_env={
                        "JOURNAL_PATH": str(journal.path),
                        "INHERITED_LOCK_FD": str(journal._lock_fd),
                        "MARKER": str(marker),
                    },
                    current_sha256=lambda: "b" * 64 if marker.exists() else "a" * 64,
                )
                self.assertEqual(result.settlement, "postimage")
                self.assertEqual(marker.read_text(), "started")

    def test_nonreading_stdin_and_bounded_output_do_not_wedge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_test_plan(root)
            script = "import os; os.write(1,b'x'*400000); os.write(2,b'y'*400000)"
            stdin_bytes = b"z" * operations.MAX_OUTPUT
            guardian_vectors: list[tuple[list[str], dict[str, str]]] = []
            real_popen = operations.subprocess.Popen

            def checked_popen(argv, *args, **kwargs):
                if len(argv) > 2 and argv[2] == operations._GUARDIAN:
                    env = kwargs["env"]
                    guardian_vectors.append((list(argv), dict(env)))
                    sizes, aggregate = operations._exec_vector_size(argv, env)
                    argv_sizes = [len(os.fsencode(value)) + 1 for value in argv]
                    vector_text = "\0".join(
                        [*argv, *(f"{key}={value}" for key, value in sorted(env.items()))]
                    )
                    self.assertLess(max(argv_sizes), operations.EXEC_ENTRY_LIMIT)
                    self.assertLessEqual(max(sizes), operations.EXEC_ENTRY_LIMIT)
                    self.assertLessEqual(aggregate, operations.EXEC_AGGREGATE_LIMIT)
                    self.assertNotIn(stdin_bytes.decode(), vector_text)
                    self.assertNotIn(base64.b64encode(stdin_bytes).decode(), vector_text)
                    self.assertIs(kwargs["stdin"], subprocess.PIPE)
                return real_popen(argv, *args, **kwargs)

            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with mock.patch.object(operations.subprocess, "Popen", side_effect=checked_popen):
                    result = run_effect(
                        journal,
                        effect_key="bounded",
                        command=[PYTHON, "-c", script],
                        cwd=root,
                        timeout=5,
                        stdin_bytes=stdin_bytes,
                        preimage_sha256="a" * 64,
                        postimage_sha256="b" * 64,
                        current_sha256=lambda: "b" * 64,
                    )
                self.assertEqual(result.settlement, "postimage")
            self.assertEqual(len(guardian_vectors), 1)

    def test_child_receives_exact_binary_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "stdin.bin"
            plan = load_test_plan(root)
            payload = bytes(range(256)) * 257 + b"\x00\xffexact-tail"
            script = textwrap.dedent(
                """
                import os, sys
                from pathlib import Path
                Path(os.environ["MARKER"]).write_bytes(sys.stdin.buffer.read())
                """
            )
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                result = run_effect(
                    journal,
                    effect_key="binary-stdin",
                    command=[PYTHON, "-c", script],
                    cwd=root,
                    stdin_bytes=payload,
                    extra_env={"MARKER": str(marker)},
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    current_sha256=lambda: (
                        "b" * 64 if marker.exists() and marker.read_bytes() == payload else "a" * 64
                    ),
                )
            self.assertEqual(result.settlement, "postimage")
            self.assertEqual(marker.read_bytes(), payload)

    def test_exec_entry_budgets_reject_before_journal_or_pipe(self) -> None:
        cases = (
            (
                "command",
                [PYTHON, "-c", "pass", "x" * operations.EXEC_ENTRY_LIMIT],
                None,
            ),
            (
                "environment",
                [PYTHON, "-c", "pass"],
                {"TOO_LARGE": "x" * operations.EXEC_ENTRY_LIMIT},
            ),
        )
        for name, command, extra_env in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan = load_test_plan(root)
                with OperationJournal(root / "state", "effect-test", plan) as journal:
                    journal.persist({"phase": "in_progress", "effects": {}})
                    with (
                        mock.patch.object(
                            operations.os,
                            "pipe",
                            side_effect=AssertionError("pipe allocated"),
                        ) as pipe_call,
                        mock.patch.object(
                            operations.subprocess,
                            "Popen",
                            side_effect=AssertionError("process launched"),
                        ) as popen_call,
                    ):
                        with self.assertRaises(PlanValidationError):
                            run_effect(
                                journal,
                                effect_key=f"budget-{name}",
                                command=command,
                                cwd=root,
                                extra_env=extra_env,
                                preimage_sha256="a" * 64,
                                postimage_sha256="b" * 64,
                                current_sha256=lambda: "a" * 64,
                            )
                    pipe_call.assert_not_called()
                    popen_call.assert_not_called()
                    self.assertEqual(journal.state["effects"], {})

    def test_exec_aggregate_budget_rejects_individually_valid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_test_plan(root)
            command = [PYTHON, *("x" * 9000 for _ in range(operations.MAX_COMMAND - 1))]
            sizes, aggregate = operations._exec_vector_size(command, {})
            self.assertTrue(all(size < operations.EXEC_ENTRY_LIMIT for size in sizes))
            self.assertGreater(aggregate, operations.EXEC_AGGREGATE_LIMIT)
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with (
                    mock.patch.object(
                        operations.os,
                        "pipe",
                        side_effect=AssertionError("pipe allocated"),
                    ) as pipe_call,
                    mock.patch.object(
                        operations.subprocess,
                        "Popen",
                        side_effect=AssertionError("process launched"),
                    ) as popen_call,
                ):
                    with self.assertRaisesRegex(PlanValidationError, "aggregate"):
                        run_effect(
                            journal,
                            effect_key="aggregate-budget",
                            command=command,
                            cwd=root,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                pipe_call.assert_not_called()
                popen_call.assert_not_called()
                self.assertEqual(journal.state["effects"], {})

    def test_attempted_persist_failure_allocates_no_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                real_persist = journal.persist

                def fail_attempted(state) -> None:
                    entry = (state.get("effects") or {}).get("attempted-persist")
                    if isinstance(entry, dict) and entry.get("status") == "attempted":
                        raise JournalError("injected attempted persist failure")
                    real_persist(state)

                with (
                    mock.patch.object(journal, "persist", side_effect=fail_attempted),
                    mock.patch.object(
                        operations.os,
                        "pipe",
                        side_effect=AssertionError("pipe allocated"),
                    ) as pipe_call,
                    mock.patch.object(
                        operations.subprocess,
                        "Popen",
                        side_effect=AssertionError("process launched"),
                    ) as popen_call,
                ):
                    with self.assertRaisesRegex(JournalError, "attempted persist"):
                        run_effect(
                            journal,
                            effect_key="attempted-persist",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                pipe_call.assert_not_called()
                popen_call.assert_not_called()
                self.assertEqual(journal.state["effects"], {})
            self.assertFalse(marker.exists())
            with OperationJournal(root / "state", "effect-test", plan) as reopened:
                self.assertEqual(reopened.state["effects"], {})

    def test_first_pipe_failure_is_closed_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with (
                    mock.patch.object(
                        operations.os,
                        "pipe",
                        side_effect=OSError(errno.EMFILE, "injected first pipe failure"),
                    ) as pipe_call,
                    mock.patch.object(
                        operations.subprocess,
                        "Popen",
                        side_effect=AssertionError("process launched"),
                    ) as popen_call,
                ):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="first-pipe",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                self.assertEqual(pipe_call.call_count, 1)
                popen_call.assert_not_called()
                entry = journal.state["effects"]["first-pipe"]
                self.assertEqual(entry["status"], "unresolved")
                self.assertIsNone(entry["child"])
            self.assertFalse(marker.exists())
            self.assert_recoverable_preimage(
                root,
                plan,
                "first-pipe",
                command,
                extra_env=extra_env,
            )

    def test_second_pipe_failure_closes_first_pipe_and_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            real_pipe = operations.os.pipe
            captured: list[tuple[int, int]] = []

            def fail_second_pipe() -> tuple[int, int]:
                if not captured:
                    pair = real_pipe()
                    captured.append(pair)
                    return pair
                raise OSError(errno.EMFILE, "injected second pipe failure")

            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with (
                    mock.patch.object(operations.os, "pipe", side_effect=fail_second_pipe),
                    mock.patch.object(
                        operations.subprocess,
                        "Popen",
                        side_effect=AssertionError("process launched"),
                    ) as popen_call,
                ):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="second-pipe",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                popen_call.assert_not_called()
                self.assertEqual(len(captured), 1)
                assert_fds_closed(self, list(captured[0]))
                entry = journal.state["effects"]["second-pipe"]
                self.assertEqual(entry["status"], "unresolved")
                self.assertIsNone(entry["child"])
            self.assertFalse(marker.exists())
            self.assert_recoverable_preimage(
                root,
                plan,
                "second-pipe",
                command,
                extra_env=extra_env,
            )

    def test_guardian_popen_failure_closes_all_parent_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            capture = EffectResourceCapture()
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with (
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(
                        operations.subprocess,
                        "Popen",
                        side_effect=OSError(errno.EAGAIN, "injected guardian launch failure"),
                    ),
                ):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="guardian-launch",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                self.assertEqual(len(capture.effect_pipes), 2)
                assert_fds_closed(self, capture.parent_fds())
                entry = journal.state["effects"]["guardian-launch"]
                self.assertEqual(entry["status"], "unresolved")
                self.assertIsNone(entry["child"])
            self.assertFalse(marker.exists())
            self.assert_recoverable_preimage(
                root,
                plan,
                "guardian-launch",
                command,
                extra_env=extra_env,
            )

    def test_stalled_guardian_stdin_transfer_is_bounded_and_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            payload = b"s" * operations.MAX_OUTPUT
            capture = EffectResourceCapture()
            guardian = "import time; time.sleep(30)"
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                started = time.monotonic()
                with (
                    mock.patch.object(operations, "_GUARDIAN", guardian),
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                ):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="stalled-guardian",
                            command=command,
                            cwd=root,
                            timeout=0.2,
                            stdin_bytes=payload,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 3)
                self.assertEqual(len(capture.guardians), 1)
                self.assertIsNotNone(capture.guardians[0].poll())
                assert_fds_closed(self, capture.parent_fds())
                entry = journal.state["effects"]["stalled-guardian"]
                self.assertEqual(entry["status"], "unresolved")
                self.assertIsNone(entry["child"])
            self.assertFalse(marker.exists())
            self.assert_recoverable_preimage(
                root,
                plan,
                "stalled-guardian",
                command,
                stdin_bytes=payload,
                extra_env=extra_env,
            )

    def test_stalled_sigterm_ignoring_guardian_is_killed_within_setup_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            payload = b"s" * operations.MAX_OUTPUT
            timeout = 0.2
            capture = EffectResourceCapture()
            ready = False
            guardian = textwrap.dedent(
                """
                import os, signal, sys, time
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                os.write(int(sys.argv[2]), b"R")
                time.sleep(30)
                """
            )

            def ready_popen(*args, **kwargs) -> subprocess.Popen:
                nonlocal ready
                process = capture.popen(*args, **kwargs)
                selector = operations.selectors.DefaultSelector()
                try:
                    selector.register(capture.effect_pipes[0][0], operations.selectors.EVENT_READ)
                    if not selector.select(2):
                        process.kill()
                        process.wait(timeout=2)
                        self.fail("guardian did not confirm SIGTERM handler readiness")
                    ready = os.read(capture.effect_pipes[0][0], 1) == b"R"
                finally:
                    selector.close()
                return process

            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                started = time.monotonic()
                with (
                    mock.patch.object(operations, "_GUARDIAN", guardian),
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=ready_popen),
                ):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="sigterm-ignoring-guardian",
                            command=command,
                            cwd=root,
                            timeout=timeout,
                            stdin_bytes=payload,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                elapsed = time.monotonic() - started
                self.assertTrue(ready)
                self.assertLess(elapsed, timeout + operations.SETUP_KILL_REAP_GRACE + 1)
                self.assertEqual(len(capture.guardians), 1)
                self.assertEqual(capture.guardians[0].returncode, -signal.SIGKILL)
                assert_fds_closed(self, capture.parent_fds())
                entry = journal.state["effects"]["sigterm-ignoring-guardian"]
                self.assertEqual(entry["status"], "unresolved")
                self.assertIsNone(entry["child"])
            self.assertFalse(marker.exists())
            self.assert_recoverable_preimage(
                root,
                plan,
                "sigterm-ignoring-guardian",
                command,
                stdin_bytes=payload,
                extra_env=extra_env,
            )

    def test_guardian_early_stdin_close_is_bounded_and_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            payload = b"e" * operations.MAX_OUTPUT
            capture = EffectResourceCapture()
            guardian = "import os,time; os.close(0); time.sleep(30)"
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                started = time.monotonic()
                with (
                    mock.patch.object(operations, "_GUARDIAN", guardian),
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                ):
                    with self.assertRaises(EffectBlocked) as caught:
                        run_effect(
                            journal,
                            effect_key="early-close",
                            command=command,
                            cwd=root,
                            timeout=1,
                            stdin_bytes=payload,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                elapsed = time.monotonic() - started
                causes: list[BaseException] = []
                cause = caught.exception.__cause__
                while cause is not None:
                    causes.append(cause)
                    cause = cause.__cause__
                self.assertTrue(
                    any(isinstance(error, OSError) and error.errno == errno.EPIPE for error in causes)
                )
                self.assertLess(elapsed, 3)
                self.assertEqual(len(capture.guardians), 1)
                self.assertIsNotNone(capture.guardians[0].poll())
                assert_fds_closed(self, capture.parent_fds())
                entry = journal.state["effects"]["early-close"]
                self.assertEqual(entry["status"], "unresolved")
                self.assertIsNone(entry["child"])
            self.assertFalse(marker.exists())
            self.assert_recoverable_preimage(
                root,
                plan,
                "early-close",
                command,
                stdin_bytes=payload,
                extra_env=extra_env,
            )

    def test_guardian_rejects_short_and_digest_mismatched_payloads(self) -> None:
        payload = b"guardian-protocol-payload"
        cases = {
            "short": payload[:-1],
            "digest": bytes([payload[0] ^ 1]) + payload[1:],
        }
        for name, delivered in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / "marker"
                plan = load_test_plan(root)
                command = [
                    PYTHON,
                    "-c",
                    "open(__import__('os').environ['MARKER'],'w').write('bad')",
                ]
                extra_env = {"MARKER": str(marker)}
                capture = EffectResourceCapture()

                def transfer(process, _payload, _deadline, _timeout) -> None:
                    self.assertEqual(process.stdin.write(delivered), len(delivered))
                    process.stdin.close()

                with OperationJournal(root / "state", "effect-test", plan) as journal:
                    journal.persist({"phase": "in_progress", "effects": {}})
                    with (
                        mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                        mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                        mock.patch.object(operations, "_transfer_guardian_stdin", side_effect=transfer),
                    ):
                        with self.assertRaises(EffectBlocked):
                            run_effect(
                                journal,
                                effect_key=f"protocol-{name}",
                                command=command,
                                cwd=root,
                                timeout=1,
                                stdin_bytes=payload,
                                extra_env=extra_env,
                                preimage_sha256="a" * 64,
                                postimage_sha256="b" * 64,
                                current_sha256=lambda: "a" * 64,
                            )
                    self.assertEqual(len(capture.guardians), 1)
                    self.assertIsNotNone(capture.guardians[0].poll())
                    assert_fds_closed(self, capture.parent_fds())
                    entry = journal.state["effects"][f"protocol-{name}"]
                    self.assertEqual(entry["status"], "unresolved")
                    self.assertIsNone(entry["child"])
                self.assertFalse(marker.exists())
                self.assert_recoverable_preimage(
                    root,
                    plan,
                    f"protocol-{name}",
                    command,
                    stdin_bytes=payload,
                    extra_env=extra_env,
                )

    def test_child_ownership_persist_failure_never_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            capture = EffectTransitionCapture()
            persist_failed = False
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                real_persist = journal.persist

                def fail_ownership(state) -> None:
                    nonlocal persist_failed
                    entry = (state.get("effects") or {}).get("ownership-persist")
                    child = entry.get("child") if isinstance(entry, dict) else None
                    if isinstance(child, dict) and not persist_failed:
                        persist_failed = True
                        raise JournalError("injected child ownership persist failure")
                    real_persist(state)

                with (
                    mock.patch.object(journal, "persist", side_effect=fail_ownership),
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                    mock.patch.object(
                        operations,
                        "_read_guardian_ack",
                        side_effect=capture.acknowledgement,
                    ),
                    mock.patch.object(operations.os, "write", side_effect=capture.write),
                    mock.patch.object(
                        operations,
                        "_terminate_owned_during_setup",
                        side_effect=capture.terminate,
                    ),
                ):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="ownership-persist",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                self.assertTrue(persist_failed)
                self.assertEqual(capture.release_attempts, [])
                self.assert_pre_release_cleanup(capture, journal, "ownership-persist", marker)
            self.assert_recoverable_preimage(
                root,
                plan,
                "ownership-persist",
                command,
                extra_env=extra_env,
            )

    def test_child_ownership_readback_mismatch_never_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            capture = EffectTransitionCapture()
            readback_mismatched = False
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                real_persist = journal.persist

                def mismatch_ownership(state) -> None:
                    nonlocal readback_mismatched
                    real_persist(state)
                    entry = (journal.state.get("effects") or {}).get("ownership-readback")
                    child = entry.get("child") if isinstance(entry, dict) else None
                    if isinstance(child, dict) and not readback_mismatched:
                        child["child_pid"] += 1
                        readback_mismatched = True

                with (
                    mock.patch.object(journal, "persist", side_effect=mismatch_ownership),
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                    mock.patch.object(
                        operations,
                        "_read_guardian_ack",
                        side_effect=capture.acknowledgement,
                    ),
                    mock.patch.object(operations.os, "write", side_effect=capture.write),
                    mock.patch.object(
                        operations,
                        "_terminate_owned_during_setup",
                        side_effect=capture.terminate,
                    ),
                ):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="ownership-readback",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                self.assertTrue(readback_mismatched)
                self.assertEqual(capture.release_attempts, [])
                self.assert_pre_release_cleanup(capture, journal, "ownership-readback", marker)
            self.assert_recoverable_preimage(
                root,
                plan,
                "ownership-readback",
                command,
                extra_env=extra_env,
            )

    def test_ownership_and_repair_persist_failures_leave_recoverable_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            capture = EffectTransitionCapture()
            ownership_persisted = False
            repair_failed = False
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                real_persist = journal.persist

                def fail_ownership_and_repair(state) -> None:
                    nonlocal ownership_persisted, repair_failed
                    entry = (state.get("effects") or {}).get("ownership-repair")
                    child = entry.get("child") if isinstance(entry, dict) else None
                    status = entry.get("status") if isinstance(entry, dict) else None
                    if isinstance(child, dict) and status == "attempted" and not ownership_persisted:
                        real_persist(state)
                        ownership_persisted = True
                        raise JournalError("injected post-durable ownership failure")
                    if ownership_persisted and status == "unresolved" and not repair_failed:
                        repair_failed = True
                        raise JournalError("injected repair persist failure")
                    real_persist(state)

                with (
                    mock.patch.object(journal, "persist", side_effect=fail_ownership_and_repair),
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                    mock.patch.object(
                        operations,
                        "_read_guardian_ack",
                        side_effect=capture.acknowledgement,
                    ),
                    mock.patch.object(operations.os, "write", side_effect=capture.write),
                    mock.patch.object(
                        operations,
                        "_terminate_owned_during_setup",
                        side_effect=capture.terminate,
                    ),
                ):
                    with self.assertRaisesRegex(EffectBlocked, "journal repair"):
                        run_effect(
                            journal,
                            effect_key="ownership-repair",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                self.assertTrue(ownership_persisted)
                self.assertTrue(repair_failed)
                self.assertEqual(capture.release_attempts, [])
                self.assertEqual(len(capture.guardians), 1)
                self.assertIsNotNone(capture.guardians[0].poll())
                self.assertEqual(capture.terminated_guardians, [capture.guardians[0].pid])
                self.assertEqual(len(capture.child_pids), 1)
                self.assertFalse(operations._pid_alive(capture.child_pids[0]))
                assert_fds_closed(self, capture.parent_fds())
                self.assertFalse(marker.exists())
                entry = journal.state["effects"]["ownership-repair"]
                self.assertEqual(entry["status"], "attempted")
                self.assertEqual(
                    entry["child"],
                    {
                        "guardian_pid": capture.guardians[0].pid,
                        "child_pid": capture.child_pids[0],
                    },
                )
            with OperationJournal(root / "state", "effect-test", plan) as reopened:
                entry = reopened.state["effects"]["ownership-repair"]
                self.assertEqual(entry["status"], "attempted")
                self.assertEqual(entry["child"]["child_pid"], capture.child_pids[0])
            self.assert_recoverable_preimage(
                root,
                plan,
                "ownership-repair",
                command,
                extra_env=extra_env,
            )

    def test_gate_release_epipe_is_single_attempt_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            capture = EffectTransitionCapture(release_mode="error")
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with (
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                    mock.patch.object(
                        operations,
                        "_read_guardian_ack",
                        side_effect=capture.acknowledgement,
                    ),
                    mock.patch.object(operations.os, "write", side_effect=capture.write),
                    mock.patch.object(
                        operations,
                        "_terminate_owned_during_setup",
                        side_effect=capture.terminate,
                    ),
                ):
                    with self.assertRaises(EffectBlocked) as caught:
                        run_effect(
                            journal,
                            effect_key="gate-epipe",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                causes: list[BaseException] = []
                cause = caught.exception.__cause__
                while cause is not None:
                    causes.append(cause)
                    cause = cause.__cause__
                self.assertTrue(
                    any(isinstance(error, OSError) and error.errno == errno.EPIPE for error in causes)
                )
                self.assertEqual(len(capture.release_attempts), 1)
                self.assertEqual(capture.release_attempts[0][1], b"1")
                self.assertEqual(capture.release_returns, [])
                self.assert_pre_release_cleanup(capture, journal, "gate-epipe", marker)
            self.assert_recoverable_preimage(
                root,
                plan,
                "gate-epipe",
                command,
                extra_env=extra_env,
            )

    def test_gate_release_zero_is_single_attempt_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "open(__import__('os').environ['MARKER'],'w').write('bad')"]
            extra_env = {"MARKER": str(marker)}
            capture = EffectTransitionCapture(release_mode="zero")
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with (
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                    mock.patch.object(
                        operations,
                        "_read_guardian_ack",
                        side_effect=capture.acknowledgement,
                    ),
                    mock.patch.object(operations.os, "write", side_effect=capture.write),
                    mock.patch.object(
                        operations,
                        "_terminate_owned_during_setup",
                        side_effect=capture.terminate,
                    ),
                ):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="gate-zero",
                            command=command,
                            cwd=root,
                            extra_env=extra_env,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                self.assertEqual(len(capture.release_attempts), 1)
                self.assertEqual(capture.release_attempts[0][1], b"1")
                self.assertEqual(capture.release_returns, [0])
                self.assert_pre_release_cleanup(capture, journal, "gate-zero", marker)
            self.assert_recoverable_preimage(
                root,
                plan,
                "gate-zero",
                command,
                extra_env=extra_env,
            )

    def test_confirmed_gate_release_survives_close_error_and_executes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            counter = root / "counter"
            plan = load_test_plan(root)
            script = textwrap.dedent(
                """
                import os
                from pathlib import Path
                path = Path(os.environ["COUNTER"])
                count = int(path.read_text()) if path.exists() else 0
                path.write_text(str(count + 1))
                """
            )
            command = [PYTHON, "-c", script]
            extra_env = {"COUNTER": str(counter)}
            capture = EffectTransitionCapture(close_after_release_error=True)
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with (
                    mock.patch.object(operations.os, "pipe", side_effect=capture.pipe),
                    mock.patch.object(operations.subprocess, "Popen", side_effect=capture.popen),
                    mock.patch.object(
                        operations,
                        "_read_guardian_ack",
                        side_effect=capture.acknowledgement,
                    ),
                    mock.patch.object(operations.os, "write", side_effect=capture.write),
                    mock.patch.object(operations.os, "close", side_effect=capture.close),
                    mock.patch.object(
                        operations,
                        "_terminate_owned_during_setup",
                        side_effect=capture.terminate,
                    ),
                ):
                    result = run_effect(
                        journal,
                        effect_key="gate-success",
                        command=command,
                        cwd=root,
                        extra_env=extra_env,
                        preimage_sha256="a" * 64,
                        postimage_sha256="b" * 64,
                        current_sha256=lambda: (
                            "b" * 64 if counter.exists() and counter.read_text() == "1" else "a" * 64
                        ),
                    )
                self.assertEqual(result.settlement, "postimage")
                self.assertEqual(len(capture.release_attempts), 1)
                self.assertEqual(capture.release_attempts[0][1], b"1")
                self.assertEqual(capture.release_returns, [1])
                self.assertEqual(capture.close_errors, 1)
                self.assertEqual(capture.terminated_guardians, [])
                self.assertEqual(counter.read_text(), "1")
                self.assertEqual(len(capture.guardians), 1)
                self.assertIsNotNone(capture.guardians[0].poll())
                self.assertEqual(len(capture.child_pids), 1)
                assert_fds_closed(self, capture.parent_fds())
                entry = journal.state["effects"]["gate-success"]
                self.assertEqual(entry["status"], "verified")

    def test_timeout_terminates_and_reaps_owned_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_test_plan(root)
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                result = run_effect(
                    journal,
                    effect_key="timeout",
                    command=[PYTHON, "-c", "import time; time.sleep(30)"],
                    cwd=root,
                    timeout=0.2,
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    current_sha256=lambda: "a" * 64,
                )
                self.assertEqual(result.settlement, "preimage")
                self.assertFalse(operations._pid_alive(result.child_pid))

    def test_post_release_termination_retains_five_then_two_second_escalation(self) -> None:
        streams = [mock.Mock(), mock.Mock(), mock.Mock()]
        process = mock.Mock()
        process.args = ["guardian"]
        process.stdin, process.stdout, process.stderr = streams
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired(process.args, 5), 0]

        operations._terminate_owned(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(
            process.wait.call_args_list,
            [mock.call(timeout=5), mock.call(timeout=2)],
        )
        for stream in streams:
            stream.close.assert_called_once_with()
        self.assertIn("child.wait(timeout=5)", operations._GUARDIAN)

    def test_post_release_termination_oserror_retains_wait_without_kill(self) -> None:
        streams = [mock.Mock(), mock.Mock(), mock.Mock()]
        process = mock.Mock()
        process.stdin, process.stdout, process.stderr = streams
        process.poll.return_value = None
        process.terminate.side_effect = OSError(errno.ESRCH, "injected terminate failure")
        process.wait.return_value = 0

        operations._terminate_owned(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()
        process.wait.assert_called_once_with(timeout=2)
        for stream in streams:
            stream.close.assert_called_once_with()

    def test_acknowledgement_loss_never_releases_the_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker"
            plan = load_test_plan(root)
            guardian = "import os,sys; os.close(int(sys.argv[2]))"
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with mock.patch.object(operations, "_GUARDIAN", guardian):
                    with self.assertRaises(EffectBlocked):
                        run_effect(
                            journal,
                            effect_key="lost",
                            command=[PYTHON, "-c", f"open({str(marker)!r},'w').write('bad')"],
                            cwd=root,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64,
                        )
                self.assertFalse(marker.exists())
                self.assertEqual(journal.state["effects"]["lost"]["status"], "unresolved")

    def test_exact_preimage_blocks_same_cycle_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "pass"]
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                result = run_effect(
                    journal,
                    effect_key="same-cycle",
                    command=command,
                    cwd=root,
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    current_sha256=lambda: "a" * 64,
                )
                self.assertEqual(result.settlement, "preimage")
                with self.assertRaises(JournalError):
                    run_effect(
                        journal,
                        effect_key="same-cycle",
                        command=command,
                        cwd=root,
                        preimage_sha256="a" * 64,
                        postimage_sha256="b" * 64,
                        current_sha256=lambda: "b" * 64,
                    )

    def test_later_retry_rejects_changed_command_and_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "pass"]
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                run_effect(
                    journal,
                    effect_key="retry",
                    command=command,
                    cwd=root,
                    stdin_bytes=b"one",
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    current_sha256=lambda: "a" * 64,
                )
            for changed_command, changed_stdin in (
                ([PYTHON, "-c", "print('changed')"], b"one"),
                (command, b"two"),
            ):
                with OperationJournal(root / "state", "effect-test", plan) as journal:
                    with self.assertRaises(JournalError):
                        run_effect(
                            journal,
                            effect_key="retry",
                            command=changed_command,
                            cwd=root,
                            stdin_bytes=changed_stdin,
                            preimage_sha256="a" * 64,
                            postimage_sha256="b" * 64,
                            current_sha256=lambda: "b" * 64,
                        )

    def test_later_retry_increments_attempt_and_completes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            counter = root / "counter"
            plan = load_test_plan(root)
            script = textwrap.dedent(
                f"""
                from pathlib import Path
                path = Path({str(counter)!r})
                count = int(path.read_text()) if path.exists() else 0
                path.write_text(str(count + 1))
                """
            )
            command = [PYTHON, "-c", script]
            current = lambda: "b" * 64 if counter.exists() and counter.read_text() == "2" else "a" * 64
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                first = run_effect(
                    journal,
                    effect_key="retry",
                    command=command,
                    cwd=root,
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    current_sha256=current,
                )
                self.assertEqual(first.settlement, "preimage")
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                second = run_effect(
                    journal,
                    effect_key="retry",
                    command=command,
                    cwd=root,
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    current_sha256=current,
                )
                self.assertEqual(second.settlement, "postimage")
                self.assertEqual(journal.state["effects"]["retry"]["attempt"], 2)

    def test_recovery_never_signals_a_recorded_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "pass"]
            identity = operations._effect_inputs(
                command, root, None, None, "a" * 64, "b" * 64
            )[4]
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist(
                    {
                        "phase": "in_progress",
                        "effects": {
                            "recover": {
                                **identity,
                                "status": "unresolved",
                                "attempt": 1,
                                "child": {"guardian_pid": 999999, "child_pid": 999998},
                            }
                        },
                    }
                )
                with mock.patch.object(operations.os, "kill", side_effect=AssertionError):
                    settled = recover_effect(
                        journal,
                        effect_key="recover",
                        command=command,
                        cwd=root,
                        preimage_sha256="a" * 64,
                        postimage_sha256="b" * 64,
                        current_sha256=lambda: "b" * 64,
                    )
                self.assertEqual(settled, "postimage")

    def test_ambiguous_settlement_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_test_plan(root)
            command = [PYTHON, "-c", "pass"]
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                with self.assertRaises(EffectBlocked):
                    run_effect(
                        journal,
                        effect_key="ambiguous",
                        command=command,
                        cwd=root,
                        preimage_sha256="a" * 64,
                        postimage_sha256="b" * 64,
                        current_sha256=lambda: "c" * 64,
                    )
                with self.assertRaises(EffectBlocked):
                    recover_effect(
                        journal,
                        effect_key="ambiguous",
                        command=command,
                        cwd=root,
                        preimage_sha256="a" * 64,
                        postimage_sha256="b" * 64,
                        current_sha256=lambda: "c" * 64,
                    )

if __name__ == "__main__":
    unittest.main()
