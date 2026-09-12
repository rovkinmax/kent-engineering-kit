from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import signal
import socket
import stat
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
    canonical_sha256,
    load_plan,
    recover_effect,
    retire_workflow_batch,
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
        {"schema": "workflow-retirement-batch-plan-v1", "test": True},
        "effect-plan.json",
    )
    return load_plan(
        path,
        schema="workflow-retirement-batch-plan-v1",
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
import sqlite3
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
elif args[:2] == ["workflow", "delete"]:
    workflow_id = args[2]
    workflow = state["workflows"][workflow_id]
    if "--confirm" not in args:
        print(json.dumps({{
            "deleted": False,
            "impact": {{
                "workflow_id": workflow_id,
                "version": workflow["version"],
                "project_count": 1,
                "link_count": 1,
                "task_count": len(workflow.get("tasks", [])),
                "current_node_count": 0,
                "pending_approval_count": 0,
                "blocked_task_count": 0,
                "default_replacement_project_count": 0,
            }},
        }}))
    else:
        task_ids = [row["task_id"] for row in workflow.get("tasks", [])]
        if not state.get("delete_noop"):
            database = sqlite3.connect(state["database"])
            for task_id in task_ids:
                database.execute("UPDATE sessions SET task_id = NULL WHERE task_id = ?", (task_id,))
                database.execute(
                    "DELETE FROM session_workflow_node_associations WHERE task_id = ?",
                    (task_id,),
                )
            database.commit()
            database.close()
            workflow["present"] = False
            workflow["tasks"] = []
            save()
        print(json.dumps({{"deleted": not state.get("delete_noop", False)}}))
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


def create_database(root: Path, sessions: list[tuple[str, str]]) -> Path:
    path = root / "kent.sqlite"
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY);
        CREATE TABLE workspaces (id TEXT PRIMARY KEY);
        CREATE TABLE worktrees (id TEXT PRIMARY KEY);
        CREATE TABLE tasks (id TEXT PRIMARY KEY);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
            worktree_id TEXT REFERENCES worktrees(id) ON DELETE SET NULL,
            artifact_relpath TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            first_prompt_preview TEXT NOT NULL DEFAULT '',
            input_draft TEXT NOT NULL DEFAULT '',
            category TEXT,
            created_at_unix_ms INTEGER NOT NULL,
            updated_at_unix_ms INTEGER NOT NULL,
            last_sequence INTEGER NOT NULL DEFAULT 0,
            model_request_count INTEGER NOT NULL DEFAULT 0,
            launch_visible INTEGER NOT NULL DEFAULT 0,
            cwd_relpath TEXT NOT NULL DEFAULT '.',
            continuation_json TEXT NOT NULL DEFAULT '{}',
            locked_json TEXT NOT NULL DEFAULT '{}',
            usage_state_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            previous_session_id TEXT,
            parent_agent_session_id TEXT,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            completed_compaction_count INTEGER,
            manual_compact_eligible INTEGER
        );
        CREATE TABLE session_workflow_node_associations (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            node_id BLOB NOT NULL,
            transition_branch_key TEXT,
            association_status TEXT NOT NULL,
            source_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT,
            associated_at_unix_ms INTEGER NOT NULL
        );
        """
    )
    database.execute("INSERT INTO projects(id) VALUES (?)", (PROJECT_ID,))
    for session_id, task_id in sessions:
        database.execute("INSERT INTO tasks(id) VALUES (?)", (task_id,))
        database.execute(
            """
            INSERT INTO sessions(
                id, project_id, artifact_relpath, created_at_unix_ms,
                updated_at_unix_ms, task_id
            ) VALUES (?, ?, ?, 1, 1, ?)
            """,
            (session_id, PROJECT_ID, f"sessions/{session_id}", task_id),
        )
        database.execute(
            """
            INSERT INTO session_workflow_node_associations(
                task_id, session_id, node_id, association_status, associated_at_unix_ms
            ) VALUES (?, ?, ?, 'historical', 1)
            """,
            (task_id, session_id, b"\x01" * 16),
        )
    database.commit()
    database.close()
    return path


def workflow_id(index: int) -> str:
    return f"123e4567-e89b-12d3-a456-{426614174000 + index:012d}"


def make_d9_fixture(root: Path, count: int = 1) -> dict:
    session_root = root / "sessions"
    session_root.mkdir()
    session_rows: list[tuple[str, str]] = []
    workflows = {}
    members = []
    for index in range(count):
        wid = workflow_id(index)
        task_id = f"TASK-{index + 1}"
        session_id = f"SESSION-{index + 1}"
        session_dir = session_root / session_id
        session_dir.mkdir()
        (session_dir / "events.jsonl").write_text(f"{{\"session\":{index + 1}}}\n")
        session_rows.append((session_id, task_id))
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
            "preview_sha256": canonical_sha256({
                "deleted": False,
                "impact": {
                    "workflow_id": wid,
                    "version": 1,
                    "project_count": 1,
                    "link_count": 1,
                    "task_count": 1,
                    "current_node_count": 0,
                    "pending_approval_count": 0,
                    "blocked_task_count": 0,
                    "default_replacement_project_count": 0,
                },
            }),
        }
        members.append(
            {
                "workflow_id": wid,
                "revision": 1,
                "links": [
                    {
                        "project_id": PROJECT_ID,
                        "workflow_id": wid,
                        "is_default": True,
                    }
                ],
                "default": wid,
                "tasks": [
                    {
                        "id": task_id,
                        "status": "done",
                        "terminal": True,
                        "current_node": None,
                        "approval_pending": False,
                    }
                ],
                "sessions": [
                    {
                        "id": session_id,
                        "status": "idle",
                        "task_id": task_id,
                        "retained": True,
                        "live_owner": None,
                        "root": str(session_root),
                        "relative": session_id,
                        "manifest": operations._session_manifest(session_dir),
                    }
                ],
                "worktrees": [],
                "retained": [],
                "absent": [],
                "delete_preview": {
                    "workflow_id": wid,
                    "sha256": workflows[wid]["preview_sha256"],
                },
            }
        )
    database = create_database(root, session_rows)
    state = {
        "project_id": PROJECT_ID,
        "project_root": str(root),
        "database": str(database),
        "workflows": workflows,
        "worktrees": [],
    }
    kent, state_path, log_path = write_fake_kent(root, state)
    plan_value = {
        "schema": "workflow-retirement-batch-plan-v1",
        "project_id": PROJECT_ID,
        "state_dir": str(root / "state"),
        "kent": {
            "path": str(kent),
            "sha256": hashlib.sha256(kent.read_bytes()).hexdigest(),
        },
        "database": {
            "path": str(database),
            "schema": "kent-2.6.1",
            "project_root": str(root),
            "session_roots": [str(session_root)],
        },
        "members": members,
    }
    path, digest = plan_file(root, plan_value)
    plan = load_plan(
        path,
        schema="workflow-retirement-batch-plan-v1",
        expected_sha256=digest,
    )
    return {
        "root": root,
        "plan": plan,
        "value": plan_value,
        "kent": kent,
        "state_path": state_path,
        "log_path": log_path,
        "database": database,
        "session_root": session_root,
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


class ManifestIOCapture:
    """Observe owned FDs/content reads without disabling platform capabilities."""

    def __init__(self, *, forbid_content: bool = False) -> None:
        self.real_open = os.open
        self.real_dup = os.dup
        self.real_close = os.close
        self.real_read = os.read
        self.forbid_content = forbid_content
        self.before_open = None
        self.after_open = None
        self.opened: list[int] = []
        self.active: set[int] = set()
        self.peak = 0
        self.content_opens: list[str] = []
        self.reads: list[tuple[tuple[int, int], int]] = []
        self.stack = ExitStack()

    def track(self, descriptor: int) -> int:
        self.opened.append(descriptor)
        self.active.add(descriptor)
        self.peak = max(self.peak, len(self.active))
        return descriptor

    def open(self, path, flags, *args, **kwargs):
        if path != "/" and (Path(path).name != path or kwargs.get("dir_fd") not in self.active):
            raise AssertionError("manifest open was not relative to an owned directory")
        if not flags & os.O_NOFOLLOW or not flags & os.O_NONBLOCK:
            raise AssertionError("manifest open omitted safety flags")
        if not flags & os.O_DIRECTORY:
            self.content_opens.append(str(path))
            if self.forbid_content:
                raise AssertionError("file content opened")
        if self.before_open is not None:
            self.before_open(path, flags, kwargs.get("dir_fd"))
        descriptor = self.track(self.real_open(path, flags, *args, **kwargs))
        if self.after_open is not None:
            self.after_open(descriptor)
        return descriptor

    def dup(self, descriptor: int) -> int:
        return self.track(self.real_dup(descriptor))

    def close(self, descriptor: int) -> None:
        self.real_close(descriptor)
        self.active.remove(descriptor)

    def read(self, descriptor: int, size: int) -> bytes:
        info = os.fstat(descriptor)
        result = self.real_read(descriptor, size)
        self.reads.append(((info.st_dev, info.st_ino), len(result)))
        return result

    def __enter__(self) -> "ManifestIOCapture":
        for name in ("open", "dup", "close", "read"):
            self.stack.enter_context(mock.patch.object(operations.os, name, side_effect=getattr(self, name)))
        return self

    def __exit__(self, *args) -> None:
        self.stack.__exit__(*args)

    def assert_closed(self, test: unittest.TestCase) -> None:
        test.assertEqual(self.active, set())
        test.assertGreater(len(self.opened), 0)
        test.assertLessEqual(self.peak, 3)
        assert_fds_closed(test, self.opened)


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

    def test_delete_preview_accepts_kent_272_exit_codes_without_confirm(self) -> None:
        workflow = workflow_id(0)
        impact = {
            "workflow_id": workflow,
            "version": 7,
            "project_count": 1,
            "link_count": 1,
            "task_count": 0,
            "current_node_count": 0,
            "pending_approval_count": 0,
            "blocked_task_count": 0,
            "default_replacement_project_count": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kent = root / "kent"
            for optional in ({}, {"blockers": []}):
                body = {"deleted": False, "impact": impact, **optional}
                for code, stderr in (
                    (1, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
                    (0, b""),
                ):
                    with self.subTest(code=code, optional=optional), mock.patch.object(
                        operations,
                        "_run",
                        return_value=(code, canonical_bytes(body), stderr),
                    ) as run:
                        result = operations._kent_delete_preview(kent, workflow, cwd=root)
                    self.assertEqual(result, body)
                    self.assertEqual(canonical_sha256(result), canonical_sha256(body))
                    self.assertEqual(
                        run.call_args.args[0],
                        [str(kent), "workflow", "delete", workflow, "--json"],
                    )
                    self.assertNotIn("--confirm", run.call_args.args[0])

    def test_delete_preview_rejection_matrix_is_closed(self) -> None:
        workflow = workflow_id(0)
        impact = {
            "workflow_id": workflow,
            "version": 1,
            "project_count": 1,
            "link_count": 1,
            "task_count": 0,
            "current_node_count": 0,
            "pending_approval_count": 0,
            "blocked_task_count": 0,
            "default_replacement_project_count": 0,
        }
        valid = {"deleted": False, "impact": impact, "blockers": []}
        cases = [
            (2, valid, b""),
            (1, b"not-json", operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, b"[]", operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, valid, b"wrong\n"),
            (0, {"workflow_id": workflow, "sha256": "a" * 64}, b""),
            (0, valid, b"unexpected\n"),
            (1, {**valid, "unknown": True}, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "deleted": True}, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "blockers": [{"code": "blocked"}]}, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "blockers": None}, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "blockers": {}}, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "blockers": ""}, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "blockers": False}, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "impact": {**impact, "workflow_id": workflow_id(1)}},
             operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "impact": []}, operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "impact": {**impact, "unknown": 0}},
             operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "impact": {**impact, "task_count": -1}},
             operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "impact": {**impact, "task_count": "0"}},
             operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, {**valid, "impact": {**impact, "task_count": True}},
             operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            (1, valid, b""),
            (1, {**valid, "impact": {key: value for key, value in impact.items() if key != "version"}},
             operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (code, value, stderr) in enumerate(cases):
                with self.subTest(index=index), mock.patch.object(
                    operations,
                    "_run",
                    return_value=(
                        code,
                        value if isinstance(value, bytes) else canonical_bytes(value),
                        stderr,
                    ),
                ):
                    with self.assertRaises(EffectBlocked):
                        operations._kent_delete_preview(root / "kent", workflow, cwd=root)
            with mock.patch.object(
                operations,
                "_run",
                return_value=(1, b'{"deleted":false,"deleted":false}', operations.DELETE_PREVIEW_DIAGNOSTIC.encode()),
            ):
                with self.assertRaises(EffectBlocked):
                    operations._kent_delete_preview(root / "kent", workflow, cwd=root)

    def test_session_manifest_streams_large_files_under_session_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            payload = b"session" * (operations.MAX_OUTPUT // 7 + 1)
            (root / "nested" / "events.jsonl").write_bytes(payload)
            reads: list[int] = []
            real_read = operations.os.read

            def checked_read(fd: int, size: int) -> bytes:
                reads.append(size)
                return real_read(fd, size)

            with (
                mock.patch.object(operations.Path, "read_bytes", side_effect=AssertionError("read_bytes used")),
                mock.patch.object(operations.os, "read", side_effect=checked_read),
            ):
                manifest = operations._session_manifest(root)
            self.assertEqual(max(reads), operations.MANIFEST_READ_CHUNK)
            self.assertEqual(
                next(row for row in manifest if row["type"] == "file")["bytes"],
                len(payload),
            )
            self.assertNotIn(
                "sha256",
                next(row for row in manifest if row["type"] == "directory"),
            )

    def test_session_and_resource_manifest_limits_reject_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oversized = root / "oversized"
            oversized.touch()
            os.truncate(oversized, operations.SESSION_MANIFEST_FILE_LIMIT + 1)
            with ManifestIOCapture(forbid_content=True) as capture:
                with self.assertRaisesRegex(operations.OperationError, "file exceeds the bound"):
                    operations._session_manifest(root)
            self.assertEqual(capture.content_opens, [])
            capture.assert_closed(self)

            aggregate = root / "aggregate"
            aggregate.mkdir()
            for name in ("one", "two", "three"):
                path = aggregate / name
                path.touch()
                os.truncate(path, 24 * 1024 * 1024)
            with ManifestIOCapture(forbid_content=True) as capture:
                with self.assertRaisesRegex(operations.OperationError, "manifest exceeds the byte bound"):
                    operations._session_manifest(aggregate)
            self.assertEqual(capture.content_opens, [])
            self.assertEqual(capture.reads, [])
            capture.assert_closed(self)

            retained = root / "retained"
            retained.mkdir()
            retained_file = retained / "file"
            retained_file.touch()
            os.truncate(retained_file, operations.MAX_OUTPUT + 1)
            with ManifestIOCapture(forbid_content=True) as capture:
                with self.assertRaisesRegex(operations.OperationError, "file exceeds the bound"):
                    operations._resource_state({
                        "kind": "directory",
                        "id": "retained",
                        "path": str(retained),
                        "sha256": "a" * 64,
                    })
            self.assertEqual(capture.content_opens, [])
            capture.assert_closed(self)

    def test_session_and_retained_resource_wrappers_use_distinct_bounds(self) -> None:
        root = Path("/tmp/manifest-wrapper-test")
        with mock.patch.object(operations, "_directory_manifest", return_value=[]) as engine:
            self.assertEqual(operations._session_manifest(root), [])
            self.assertEqual(operations._retained_resource_manifest(root), [])
        self.assertEqual(
            engine.call_args_list,
            [
                mock.call(
                    root,
                    file_limit=operations.SESSION_MANIFEST_FILE_LIMIT,
                    total_limit=operations.SESSION_MANIFEST_TOTAL_LIMIT,
                    label="Session",
                ),
                mock.call(
                    root,
                    file_limit=operations.MAX_OUTPUT,
                    total_limit=operations.MAX_OUTPUT,
                    label="retained resource",
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            retained = Path(temporary) / "retained"
            retained.mkdir()
            with mock.patch.object(
                operations,
                "_retained_resource_manifest",
                return_value=[],
            ) as wrapper:
                result = operations._resource_state({
                    "kind": "directory",
                    "id": "retained",
                    "path": str(retained),
                    "sha256": "a" * 64,
                })
            wrapper.assert_called_once_with(retained)
            self.assertEqual(result["sha256"], canonical_sha256([]))

    def test_manifest_opens_preflighted_files_nonblocking_before_fifo_swap(self) -> None:
        if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
            self.skipTest("FIFO or nonblocking open is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "events.jsonl"
            target.write_text("events")
            real_open = operations.os.open
            observed_flags: list[int] = []

            def swap_before_open(path, flags, *args, **kwargs):
                if path == target.name and kwargs.get("dir_fd") is not None:
                    target.unlink()
                    os.mkfifo(target)
                    observed_flags.append(flags)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(operations.os, "open", side_effect=swap_before_open):
                with self.assertRaises(operations.OperationError):
                    operations._session_manifest(root)
            self.assertEqual(len(observed_flags), 1)
            self.assertNotEqual(observed_flags[0] & os.O_NONBLOCK, 0)

    def test_manifest_bounds_synthetic_siblings_before_sort_stat_open_or_read(self) -> None:
        class SyntheticEntry:
            def __init__(self, index: int) -> None:
                self._index = index
                self.name_reads = 0
                self.stat_calls = 0

            @property
            def name(self) -> str:
                self.name_reads += 1
                return f"entry-{self._index:04d}"

            def stat(self, *, follow_symlinks: bool) -> os.stat_result:
                self.stat_calls += 1
                raise AssertionError("synthetic entry was statted")

        class SyntheticStream:
            def __init__(self, entries: list[SyntheticEntry]) -> None:
                self.entries = entries
                self.consumed = 0

            def __enter__(self) -> "SyntheticStream":
                return self

            def __exit__(self, *args) -> None:
                return None

            def __iter__(self):
                for entry in self.entries:
                    self.consumed += 1
                    yield entry

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entries = [
                SyntheticEntry(index)
                for index in range(operations.MAX_LIST + 8)
            ]
            stream = SyntheticStream(entries)
            with (
                mock.patch.object(operations.os, "scandir", return_value=stream),
                ManifestIOCapture(forbid_content=True) as capture,
            ):
                with self.assertRaisesRegex(operations.OperationError, "manifest is too large"):
                    operations._session_manifest(root)
            self.assertEqual(stream.consumed, operations.MAX_LIST + 1)
            self.assertEqual(sum(entry.name_reads for entry in entries), 0)
            self.assertEqual(sum(entry.stat_calls for entry in entries), 0)
            self.assertEqual(capture.content_opens, [])
            self.assertEqual(capture.reads, [])
            capture.assert_closed(self)

    def test_manifest_bounds_nested_directories_before_overbudget_recursion(self) -> None:
        temporary = Path(tempfile.mkdtemp())
        created: list[Path] = []
        try:
            root = temporary
            current = root
            for _ in range(operations.MAX_LIST + 1):
                current = current / "d"
                current.mkdir()
                created.append(current)
            scanned: list[tuple[int, int]] = []
            real_scandir = operations.os.scandir

            def counted_scandir(path):
                info = os.fstat(path)
                scanned.append((info.st_dev, info.st_ino))
                return real_scandir(path)

            with (
                mock.patch.object(
                    operations.os,
                    "scandir",
                    side_effect=counted_scandir,
                ),
                ManifestIOCapture(forbid_content=True) as capture,
            ):
                with self.assertRaisesRegex(operations.OperationError, "manifest is too large"):
                    operations._session_manifest(root)
            self.assertEqual(len(scanned), operations.MAX_LIST + 1)
            expected_last = created[operations.MAX_LIST - 1].stat()
            self.assertEqual(scanned[-1], (expected_last.st_dev, expected_last.st_ino))
            self.assertEqual(capture.content_opens, [])
            self.assertEqual(capture.reads, [])
            capture.assert_closed(self)
        finally:
            for directory in reversed(created):
                directory.rmdir()
            temporary.rmdir()

    def test_manifest_rejects_first_enumeration_directory_snapshot_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            (nested / "events.jsonl").write_text("events")
            with operations._manifest_root(root, "Session") as (descriptor, _):
                entries, snapshot = operations._manifest_entries(descriptor, "Session")
            altered = dict(snapshot)
            kind, fingerprint = altered["nested"]
            altered["nested"] = (
                kind,
                (*fingerprint[:5], fingerprint[5] + 1, *fingerprint[6:]),
            )
            with (
                mock.patch.object(
                    operations,
                    "_manifest_entries",
                    return_value=(entries, altered),
                ),
                ManifestIOCapture(forbid_content=True) as capture,
            ):
                with self.assertRaisesRegex(operations.OperationError, "entry changed during enumeration"):
                    operations._session_manifest(root)
            self.assertEqual(capture.content_opens, [])
            capture.assert_closed(self)

    def test_manifest_rejects_symlink_fifo_and_socket_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text("target")
            (root / "link").symlink_to(target)
            with self.assertRaises(operations.OperationError):
                operations._session_manifest(root)

        if hasattr(os, "mkfifo"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                os.mkfifo(root / "pipe")
                with self.assertRaises(operations.OperationError):
                    operations._session_manifest(root)

        if hasattr(socket, "AF_UNIX"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                endpoint = root / "socket"
                created = subprocess.run(
                    [
                        sys.executable, "-c",
                        "import socket\n"
                        "with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:\n"
                        "    listener.bind('socket')\n",
                    ],
                    cwd=root, capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                self.assertTrue(stat.S_ISSOCK(endpoint.lstat().st_mode))
                with self.assertRaises(operations.OperationError):
                    operations._session_manifest(root)

    def test_manifest_directory_swaps_never_read_outside_and_close_owned_descriptors(self) -> None:
        cases = [
            (location, milestone, replacement)
            for location in ("ancestor", "root", "child")
            for milestone in ("stat", "open", "opened", "enumerate", "enumerated")
            for replacement in ("symlink", "directory")
        ] + [
            (location, "final_open", replacement)
            for location in ("ancestor", "root")
            for replacement in ("symlink", "directory")
        ]
        for manifest in (operations._session_manifest, operations._retained_resource_manifest):
            for location, milestone, replacement in cases:
                with self.subTest(wrapper=manifest.__name__, location=location,
                                  milestone=milestone, replacement=replacement):
                    with tempfile.TemporaryDirectory() as temporary:
                        fixture = Path(temporary).resolve()
                        ancestor = fixture / "ancestor"
                        root = ancestor / "root"
                        child = root / "child"
                        child.mkdir(parents=True)
                        local_files = [root / "events", child / "events"]
                        for path in local_files:
                            path.write_bytes(b"local fixture")
                        target = {"ancestor": ancestor, "root": root, "child": child}[location]
                        outside = fixture / "outside"
                        outside.mkdir()
                        sentinels = []
                        for path in local_files:
                            if path.is_relative_to(target):
                                relative = path.relative_to(target)
                                sentinel = outside / relative
                                sentinel.parent.mkdir(parents=True, exist_ok=True)
                                sentinel.write_bytes(b"external disposable sentinel")
                                sentinels.append(relative)
                        outside_ids = {
                            (info.st_dev, info.st_ino)
                            for info in ((outside / relative).stat() for relative in sentinels)
                        }
                        target_info = target.stat()
                        target_id = (target_info.st_dev, target_info.st_ino)
                        parent_info = target.parent.stat()
                        parent_id = (parent_info.st_dev, parent_info.st_ino)
                        scan_info = (root if location == "ancestor" else target).stat()
                        scan_id = (scan_info.st_dev, scan_info.st_ino)
                        real_stat, real_scandir = os.stat, os.scandir
                        real_entries = operations._manifest_entries
                        swapped = False
                        enumerations = 0
                        final_check = False

                        def swap() -> None:
                            nonlocal swapped
                            target.rename(fixture / "retained")
                            if replacement == "symlink":
                                target.symlink_to(outside, target_is_directory=True)
                            else:
                                outside.rename(target)
                            swapped = True

                        def swapped_stat(path, *args, **kwargs):
                            info = real_stat(path, *args, **kwargs)
                            if (
                                milestone == "stat" and not swapped
                                and kwargs.get("dir_fd") is not None
                                and (info.st_dev, info.st_ino) == target_id
                            ):
                                swap()
                            return info

                        def before_open(path, flags, parent):
                            if milestone not in {"open", "final_open"} or swapped:
                                return
                            if milestone == "final_open" and not final_check:
                                return
                            if parent is not None and path == target.name:
                                info = os.fstat(parent)
                                if (info.st_dev, info.st_ino) == parent_id:
                                    swap()

                        def after_open(descriptor):
                            info = os.fstat(descriptor)
                            if milestone == "opened" and not swapped and (info.st_dev, info.st_ino) == target_id:
                                swap()

                        @contextmanager
                        def swapped_scandir(descriptor):
                            self.assertIsInstance(descriptor, int)
                            info = os.fstat(descriptor)
                            chosen = (info.st_dev, info.st_ino) == scan_id
                            if milestone == "enumerate" and not swapped and chosen:
                                swap()
                            with real_scandir(descriptor) as stream:
                                yield stream
                            if milestone == "enumerated" and not swapped and chosen:
                                swap()

                        def counted_entries(*args):
                            nonlocal enumerations, final_check
                            result = real_entries(*args)
                            enumerations += 1
                            final_check = enumerations == 2
                            return result

                        capture = ManifestIOCapture()
                        capture.before_open = before_open
                        capture.after_open = after_open
                        with (
                            capture,
                            mock.patch.object(operations.os, "stat", side_effect=swapped_stat),
                            mock.patch.object(operations.os, "scandir", side_effect=swapped_scandir),
                            mock.patch.object(operations, "_manifest_entries", side_effect=counted_entries),
                            mock.patch.object(operations, "_run", side_effect=AssertionError("live call")) as run,
                            mock.patch.object(operations, "run_effect", side_effect=AssertionError("effect")) as effect,
                        ):
                            with self.assertRaises(operations.OperationError):
                                manifest(root)
                        self.assertTrue(swapped, "the intended race milestone was not exercised")
                        self.assertEqual(sum(size for identity, size in capture.reads if identity in outside_ids), 0)
                        self.assertFalse(any(identity in outside_ids for identity, _ in capture.reads))
                        capture.assert_closed(self)
                        run.assert_not_called()
                        effect.assert_not_called()
                        sentinel_root = outside if replacement == "symlink" else target
                        for relative in sentinels:
                            self.assertEqual((sentinel_root / relative).read_bytes(), b"external disposable sentinel")

    def test_manifest_accepts_stable_ancestor_aliases_but_not_symlink_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary).resolve()
            physical = fixture / "physical"
            root = physical / "root"
            root.mkdir(parents=True)
            (root / "events").write_text("events")
            holder = fixture / "holder"
            holder.mkdir()
            for index, target in enumerate((physical, Path("../physical"))):
                alias = holder / f"alias-{index}"
                alias.symlink_to(target, target_is_directory=True)
                for manifest in (operations._session_manifest, operations._retained_resource_manifest):
                    with self.subTest(target=target, wrapper=manifest.__name__):
                        with ManifestIOCapture() as capture:
                            result = manifest(alias / "root")
                        capture.assert_closed(self)
                        self.assertEqual(result, manifest(root))
            link = fixture / "root-link"
            link.symlink_to(root, target_is_directory=True)
            for manifest in (operations._session_manifest, operations._retained_resource_manifest):
                with ManifestIOCapture(forbid_content=True) as capture:
                    with self.assertRaisesRegex(operations.OperationError, "symlink root"):
                        manifest(link)
                capture.assert_closed(self)

    def test_manifest_resolves_nested_alias_parent_segments_in_filesystem_order(self) -> None:
        for bridge_kind in ("absolute", "relative", "nested-absolute", "nested-relative"):
            for alias_kind in ("absolute", "relative", "leading-parent", "repeated-parent"):
                with self.subTest(bridge=bridge_kind, alias=alias_kind), tempfile.TemporaryDirectory() as temporary:
                    fixture = Path(temporary).resolve()
                    deep = fixture / "inside" / "deep"
                    deep.mkdir(parents=True)
                    correct = fixture / "inside" / "root"
                    incorrect = fixture / "root"
                    correct.mkdir()
                    incorrect.mkdir()
                    correct_bytes = b"correct-os-resolved-root"
                    incorrect_bytes = b"incorrect-lexically-resolved-root"
                    (correct / "sentinel").write_bytes(correct_bytes)
                    (incorrect / "sentinel").write_bytes(incorrect_bytes)
                    bridge = fixture / "bridge"
                    if bridge_kind.startswith("nested-"):
                        middle = fixture / "middle"
                        middle.symlink_to(
                            "inside/deep" if bridge_kind == "nested-absolute" else deep,
                            target_is_directory=True,
                        )
                        bridge.symlink_to(
                            middle if bridge_kind == "nested-absolute" else "middle",
                            target_is_directory=True,
                        )
                        self.assertTrue(middle.is_symlink())
                    else:
                        bridge.symlink_to(
                            deep if bridge_kind == "absolute" else "inside/deep",
                            target_is_directory=True,
                        )
                    alias = fixture / "alias"
                    if alias_kind == "leading-parent":
                        holder = fixture / "holder"
                        holder.mkdir()
                        alias = holder / "alias"
                    target = {
                        "absolute": str(bridge / ".."),
                        "relative": "bridge/..",
                        "leading-parent": "../bridge/..",
                        "repeated-parent": "bridge/../deep/..",
                    }[alias_kind]
                    alias.symlink_to(target, target_is_directory=True)
                    selected = alias / "root"
                    self.assertTrue(bridge.is_symlink())
                    self.assertTrue(alias.is_symlink())
                    self.assertEqual(os.readlink(alias), target)
                    self.assertEqual(selected.resolve(strict=True), correct)
                    correct_info = (correct / "sentinel").stat()
                    incorrect_info = (incorrect / "sentinel").stat()
                    correct_id = (correct_info.st_dev, correct_info.st_ino)
                    incorrect_id = (incorrect_info.st_dev, incorrect_info.st_ino)
                    self.assertNotEqual(correct_id, incorrect_id)
                    for manifest in (operations._session_manifest, operations._retained_resource_manifest):
                        with self.subTest(wrapper=manifest.__name__):
                            parent_steps = 0

                            def before_open(path, flags, parent):
                                nonlocal parent_steps
                                if path == "..":
                                    parent_steps += 1
                                    self.assertIsNotNone(parent)
                                    self.assertTrue(flags & os.O_DIRECTORY)

                            capture = ManifestIOCapture()
                            capture.before_open = before_open
                            with capture:
                                result = manifest(selected)
                            capture.assert_closed(self)
                            self.assertGreater(parent_steps, 0)
                            self.assertEqual(result, [{
                                "path": "sentinel", "type": "file",
                                "mode": stat.S_IMODE(correct_info.st_mode),
                                "bytes": len(correct_bytes),
                                "sha256": hashlib.sha256(correct_bytes).hexdigest(),
                            }])
                            self.assertEqual(
                                sum(size for identity, size in capture.reads if identity == correct_id),
                                len(correct_bytes),
                            )
                            self.assertFalse(any(identity == incorrect_id for identity, _ in capture.reads))

    def test_manifest_parent_segment_directory_moves_cannot_read_the_new_parent_tree(self) -> None:
        for milestone in (
            "before_stat", "after_stat", "before_open", "after_open", "verified", "final_open",
        ):
            for manifest in (operations._session_manifest, operations._retained_resource_manifest):
                with self.subTest(milestone=milestone, wrapper=manifest.__name__):
                    with tempfile.TemporaryDirectory() as temporary:
                        fixture = Path(temporary).resolve()
                        inside = fixture / "inside"
                        deep = inside / "deep"
                        deep.mkdir(parents=True)
                        outside = fixture / "outside"
                        outside.mkdir()
                        for parent in (inside, outside):
                            (parent / "root").mkdir()
                            (parent / "root" / "sentinel").write_bytes(parent.name.encode())
                        (fixture / "bridge").symlink_to(deep, target_is_directory=True)
                        (fixture / "alias").symlink_to("bridge/..", target_is_directory=True)
                        selected = fixture / "alias" / "root"
                        self.assertEqual(selected.resolve(strict=True), inside / "root")
                        deep_info = deep.stat()
                        deep_id = (deep_info.st_dev, deep_info.st_ino)
                        outside_info = (outside / "root" / "sentinel").stat()
                        outside_id = (outside_info.st_dev, outside_info.st_ino)
                        real_stat, real_fstat = os.stat, os.fstat
                        swapped = False
                        opening_parent = False
                        parent_opens = 0

                        def is_parent_step(path, parent):
                            if path != ".." or parent is None:
                                return False
                            info = real_fstat(parent)
                            return (info.st_dev, info.st_ino) == deep_id

                        def swap() -> None:
                            nonlocal swapped
                            deep.rename(outside / "deep")
                            swapped = True

                        def swapped_stat(path, *args, **kwargs):
                            chosen = is_parent_step(path, kwargs.get("dir_fd"))
                            if chosen and not swapped and milestone == "before_stat":
                                swap()
                            info = real_stat(path, *args, **kwargs)
                            if chosen and not swapped and milestone == "after_stat":
                                swap()
                            return info

                        def before_open(path, flags, parent):
                            nonlocal opening_parent, parent_opens
                            opening_parent = is_parent_step(path, parent)
                            if opening_parent:
                                parent_opens += 1
                                if not swapped and (
                                    milestone == "before_open"
                                    or (milestone == "final_open" and parent_opens == 2)
                                ):
                                    swap()

                        def after_open(descriptor):
                            if opening_parent and not swapped and milestone == "after_open":
                                swap()

                        def swapped_fstat(descriptor):
                            info = real_fstat(descriptor)
                            if opening_parent and not swapped and milestone == "verified":
                                swap()
                            return info

                        capture = ManifestIOCapture()
                        capture.before_open = before_open
                        capture.after_open = after_open
                        with (
                            capture,
                            mock.patch.object(operations.os, "stat", side_effect=swapped_stat),
                            mock.patch.object(operations.os, "fstat", side_effect=swapped_fstat),
                        ):
                            with self.assertRaises(operations.OperationError):
                                manifest(selected)
                        self.assertTrue(swapped, "the intended parent-segment race was not exercised")
                        self.assertFalse(any(identity == outside_id for identity, _ in capture.reads))
                        capture.assert_closed(self)
                        self.assertEqual((outside / "root" / "sentinel").read_bytes(), b"outside")

    def test_manifest_alias_cycles_and_expansion_budget_close_descriptors(self) -> None:
        for target in ("alias", "../" * (operations.MAX_LIST + 1) + "inside"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary)
                alias = fixture / "alias"
                alias.symlink_to(target, target_is_directory=True)
                self.assertEqual(os.readlink(alias), target)
                for manifest in (operations._session_manifest, operations._retained_resource_manifest):
                    with ManifestIOCapture(forbid_content=True) as capture:
                        with self.assertRaisesRegex(operations.OperationError, "manifest root path is too large"):
                            manifest(alias / "root")
                    self.assertEqual(capture.content_opens, [])
                    capture.assert_closed(self)

    def test_manifest_ancestor_alias_swaps_are_bound_before_content_reads(self) -> None:
        for milestone in ("before_readlink", "after_readlink", "verified"):
            for manifest in (operations._session_manifest, operations._retained_resource_manifest):
                with self.subTest(milestone=milestone, wrapper=manifest.__name__):
                    with tempfile.TemporaryDirectory() as temporary:
                        fixture = Path(temporary).resolve()
                        for name in ("inside", "outside"):
                            (fixture / name / "root").mkdir(parents=True)
                            (fixture / name / "root" / "events").write_text(name)
                        alias = fixture / "alias"
                        alias.symlink_to("inside", target_is_directory=True)
                        real_readlink, real_stat = os.readlink, os.stat
                        observed_link = False
                        swapped = False
                        external = (fixture / "outside" / "root" / "events").stat()

                        def swap() -> None:
                            nonlocal swapped
                            alias.rename(fixture / "retained-alias")
                            alias.symlink_to("outside", target_is_directory=True)
                            swapped = True

                        def swapped_readlink(path, *args, **kwargs):
                            nonlocal observed_link
                            chosen = path == "alias" and kwargs.get("dir_fd") is not None and not swapped
                            if chosen and milestone == "before_readlink":
                                swap()
                            result = real_readlink(path, *args, **kwargs)
                            if chosen:
                                observed_link = True
                                if milestone == "after_readlink":
                                    swap()
                            return result

                        def swapped_stat(path, *args, **kwargs):
                            info = real_stat(path, *args, **kwargs)
                            if (
                                milestone == "verified" and observed_link and not swapped
                                and path == "alias" and kwargs.get("dir_fd") is not None
                            ):
                                swap()
                            return info

                        with (
                            ManifestIOCapture() as capture,
                            mock.patch.object(operations.os, "readlink", side_effect=swapped_readlink),
                            mock.patch.object(operations.os, "stat", side_effect=swapped_stat),
                        ):
                            with self.assertRaises(operations.OperationError):
                                manifest(alias / "root")
                        self.assertTrue(observed_link)
                        self.assertTrue(swapped)
                        self.assertFalse(any(
                            identity == (external.st_dev, external.st_ino) for identity, _ in capture.reads
                        ))
                        capture.assert_closed(self)

    def test_manifest_missing_required_primitives_fails_before_acquiring_descriptors(self) -> None:
        for name, value in (
            ("supports_dir_fd", set()),
            ("supports_fd", set()),
            ("supports_follow_symlinks", set()),
            ("O_NOFOLLOW", 0),
            ("O_DIRECTORY", 0),
            ("O_NONBLOCK", 0),
        ):
            with self.subTest(primitive=name), mock.patch.object(operations.os, name, value):
                with mock.patch.object(operations.os, "open", side_effect=AssertionError("opened")) as opened:
                    with self.assertRaisesRegex(operations.OperationError, "primitives are unavailable"):
                        operations._session_manifest(Path("/unused"))
                opened.assert_not_called()

    def test_manifest_io_errors_release_root_directory_and_content_descriptors(self) -> None:
        for stage in ("root_fstat", "directory_fstat", "file_fstat", "scandir", "read"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                nested = root / "nested"
                nested.mkdir()
                payload = nested / "events"
                payload.write_text("events")
                identities = {
                    "root_fstat": root.stat().st_ino,
                    "directory_fstat": nested.stat().st_ino,
                    "file_fstat": payload.stat().st_ino,
                }
                real_fstat = os.fstat
                injected = False

                def failed_fstat(descriptor):
                    nonlocal injected
                    info = real_fstat(descriptor)
                    if not injected and info.st_ino == identities.get(stage):
                        injected = True
                        raise OSError(errno.EIO, "injected fstat error")
                    return info

                def failed_io(*args):
                    nonlocal injected
                    injected = True
                    raise OSError(errno.EIO, "injected I/O error")

                with ManifestIOCapture() as capture, ExitStack() as patches:
                    patches.enter_context(mock.patch.object(operations.os, "fstat", side_effect=failed_fstat))
                    if stage in {"scandir", "read"}:
                        patches.enter_context(mock.patch.object(operations.os, stage, side_effect=failed_io))
                    with self.assertRaisesRegex(operations.OperationError, "cannot be accessed safely"):
                        operations._session_manifest(root)
                self.assertTrue(injected)
                capture.assert_closed(self)

    def test_manifest_file_symlink_swap_and_growth_are_rejected_with_closed_descriptors(self) -> None:
        for stage in ("symlink", "growth"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary)
                root = fixture / "root"
                root.mkdir()
                target = root / "events"
                target.write_bytes(b"inside")
                outside = fixture / "external"
                outside.write_bytes(b"external sentinel")
                external = outside.stat()
                mutated = False
                capture = ManifestIOCapture()
                real_read = os.read

                def before_open(path, flags, parent):
                    nonlocal mutated
                    if stage == "symlink" and not mutated and path == "events":
                        target.unlink()
                        target.symlink_to(outside)
                        mutated = True

                def grown_read(descriptor, size):
                    nonlocal mutated
                    if stage == "growth" and not mutated:
                        # Path writes are confined to the disposable fixture.
                        target.write_bytes(b"inside" * 4)
                        mutated = True
                    return real_read(descriptor, size)

                capture.before_open = before_open
                capture.real_read = grown_read
                with capture:
                    with self.assertRaises(operations.OperationError):
                        operations._session_manifest(root)
                self.assertTrue(mutated)
                self.assertFalse(any(
                    identity == (external.st_dev, external.st_ino) for identity, _ in capture.reads
                ))
                capture.assert_closed(self)

    def test_manifest_rejects_file_mutation_and_replacement_during_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "events.jsonl"
            path.write_bytes(b"x" * (operations.MANIFEST_READ_CHUNK + 1))
            real_read = operations.os.read
            calls = 0

            def mutate_after_read(fd: int, size: int) -> bytes:
                nonlocal calls
                chunk = real_read(fd, size)
                calls += 1
                if calls == 1:
                    path.write_bytes(b"mutation")
                return chunk

            with mock.patch.object(operations.os, "read", side_effect=mutate_after_read):
                with self.assertRaises(operations.OperationError):
                    operations._session_manifest(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "events.jsonl"
            path.write_bytes(b"x" * (operations.MANIFEST_READ_CHUNK + 1))
            real_read = operations.os.read
            calls = 0

            def replace_after_read(fd: int, size: int) -> bytes:
                nonlocal calls
                chunk = real_read(fd, size)
                calls += 1
                if calls == 1:
                    path.unlink()
                    path.write_bytes(b"replacement")
                return chunk

            with mock.patch.object(operations.os, "read", side_effect=replace_after_read):
                with self.assertRaises(operations.OperationError):
                    operations._session_manifest(root)

    def test_manifest_compares_complete_second_file_and_directory_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            path = nested / "events.jsonl"
            path.write_text("events")
            with operations._manifest_root(root, "Session") as (descriptor, _):
                entries, snapshot = operations._manifest_entries(descriptor, "Session")
            altered = dict(snapshot)
            kind, fingerprint = altered["nested/events.jsonl"]
            altered["nested/events.jsonl"] = (
                kind,
                (*fingerprint[:-1], fingerprint[-1] + 1),
            )
            with mock.patch.object(
                operations,
                "_manifest_entries",
                side_effect=[(entries, snapshot), (entries, altered)],
            ):
                with self.assertRaises(operations.OperationError):
                    operations._session_manifest(root)
            topology = dict(snapshot)
            topology["nested/new-file"] = (
                "file",
                (0, 0, stat.S_IFREG, 0o644, 0, 0, 0, 1),
            )
            with mock.patch.object(
                operations,
                "_manifest_entries",
                side_effect=[(entries, snapshot), (entries, topology)],
            ):
                with self.assertRaises(operations.OperationError):
                    operations._session_manifest(root)
            directory_mutation = dict(snapshot)
            kind, fingerprint = directory_mutation["nested"]
            directory_mutation["nested"] = (
                kind,
                (*fingerprint[:5], fingerprint[5] + 1, *fingerprint[6:]),
            )
            with mock.patch.object(
                operations,
                "_manifest_entries",
                side_effect=[(entries, snapshot), (entries, directory_mutation)],
            ):
                with self.assertRaises(operations.OperationError):
                    operations._session_manifest(root)

    def test_plan_rejects_nonterminal_and_raw_protocol_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = make_d9_fixture(root)
            for mutate in (
                lambda value: value["members"][0]["tasks"][0].update(terminal=False),
                lambda value: value["members"][0].update(command=["/bin/echo"]),
                lambda value: value["members"][0].update(sql="DELETE"),
            ):
                value = json.loads(json.dumps(fixture["value"]))
                mutate(value)
                path, digest = plan_file(root, value, "invalid.json")
                plan = load_plan(
                    path,
                    schema="workflow-retirement-batch-plan-v1",
                    expected_sha256=digest,
                )
                with self.assertRaises(PlanValidationError):
                    retire_workflow_batch(plan, mode="apply")

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

    def test_d9_task_detail_current_node_blocks_before_confirmed_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            state = read_state(fixture)
            detail = state["workflows"][workflow_id(0)]["details"]["TASK-1"]
            detail["summary"]["done"] = False
            detail["status"] = {"kind": "doing", "native_state": "active"}
            detail["current_nodes"] = [{"node_id": "implement"}]
            write_state(fixture, state)
            with self.assertRaises(EffectBlocked):
                retire_workflow_batch(fixture["plan"], mode="apply")
            self.assertNotIn("--confirm", [arg for row in read_log(fixture) for arg in row])

    def test_d9_running_session_blocks_before_confirmed_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            workflow["sessions"]["TASK-1"][0]["status"] = "running"
            workflow["details"]["TASK-1"]["live_sessions"] = ["SESSION-1"]
            write_state(fixture, state)
            with self.assertRaises(EffectBlocked):
                retire_workflow_batch(fixture["plan"], mode="apply")
            confirmed = [row for row in read_log(fixture) if "--confirm" in row]
            self.assertEqual(confirmed, [])

    def test_d9_incomplete_session_pagination_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            state = read_state(fixture)
            state["incomplete_sessions"] = True
            write_state(fixture, state)
            with self.assertRaises(operations.OperationError):
                retire_workflow_batch(fixture["plan"], mode="preview")

    def test_d9_exact_argv_and_sqlite_cascade_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            report = retire_workflow_batch(fixture["plan"], mode="apply")
            self.assertEqual(report["phase"], "complete")
            log = read_log(fixture)
            self.assertIn(
                ["task", "show", "TASK-1", "--project", PROJECT_ID, "--json"],
                log,
            )
            self.assertIn(
                [
                    "task",
                    "sessions",
                    "TASK-1",
                    "--project",
                    PROJECT_ID,
                    "--offset",
                    "0",
                    "--limit",
                    "100",
                    "--json",
                ],
                log,
            )
            self.assertEqual(
                [row for row in log if "--confirm" in row],
                [["workflow", "delete", workflow_id(0), "--confirm", "--json"]],
            )
            database = sqlite3.connect(fixture["database"])
            self.assertIsNone(
                database.execute("SELECT task_id FROM sessions").fetchone()[0]
            )
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM session_workflow_node_associations"
                ).fetchone()[0],
                0,
            )
            database.close()

    def test_d9_worktree_owner_registration_and_resources_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = make_d9_fixture(root)
            managed = root / "managed"
            managed.mkdir()
            subprocess.run(["/usr/bin/git", "-C", str(managed), "init", "-q"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(managed), "branch", "-M", "main"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(managed), "config", "user.name", "Kent"], check=True)
            subprocess.run(
                ["/usr/bin/git", "-C", str(managed), "config", "user.email", "kent@example.invalid"],
                check=True,
            )
            (managed / "tracked").write_text("one\n")
            subprocess.run(["/usr/bin/git", "-C", str(managed), "add", "."], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(managed), "commit", "-q", "-m", "one"], check=True)
            head = subprocess.run(
                ["/usr/bin/git", "-C", str(managed), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            retained = root / "retained.txt"
            retained.write_text("retained\n")
            value = json.loads(json.dumps(fixture["value"]))
            value["members"][0]["worktrees"] = [
                {
                    "path": str(managed),
                    "branch": "main",
                    "head": head,
                    "dirty": False,
                    "owner_session": None,
                    "registered": True,
                    "retained": True,
                }
            ]
            value["members"][0]["retained"] = [
                {
                    "kind": "file",
                    "id": "retained-file",
                    "path": str(retained),
                    "sha256": hashlib.sha256(retained.read_bytes()).hexdigest(),
                }
            ]
            value["members"][0]["absent"] = [
                {
                    "kind": "file",
                    "id": "absent-file",
                    "path": str(root / "absent.txt"),
                    "sha256": None,
                }
            ]
            path, digest = plan_file(root, value, "worktree-plan.json")
            plan = load_plan(
                path,
                schema="workflow-retirement-batch-plan-v1",
                expected_sha256=digest,
            )
            state = read_state(fixture)
            state["worktrees"] = [
                {
                    "topology": {
                        "variant": "registered",
                        "registered": {
                            "git": {
                                "canonical_root": str(managed),
                                "branch_name": "main",
                                "head_object": head,
                            },
                            "kent": {"origin_session_id": None},
                        },
                    }
                }
            ]
            write_state(fixture, state)
            self.assertEqual(retire_workflow_batch(plan, mode="preview")["phase"], "preview")
            state["worktrees"][0]["topology"]["registered"]["kent"][
                "origin_session_id"
            ] = "SESSION-LIVE"
            write_state(fixture, state)
            with self.assertRaises(EffectBlocked):
                retire_workflow_batch(plan, mode="preview")

    def test_d9_verified_effect_resume_does_not_replay_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            parsed = operations._validate_d9_plan(fixture["plan"])
            prepared = operations._d9_read_inventory(parsed)
            subprocess.run(
                [str(fixture["kent"]), "workflow", "delete", workflow_id(0), "--confirm", "--json"],
                check=True,
                capture_output=True,
            )
            before = prepared["members"][workflow_id(0)]
            after = operations._d9_expected_post(parsed["members"][0], before)
            command = operations._kent_delete_command(fixture["kent"], workflow_id(0), True)
            identity = operations._effect_inputs(
                command,
                fixture["root"],
                None,
                None,
                canonical_sha256(before),
                canonical_sha256(after),
            )[4]
            with OperationJournal(
                fixture["root"] / "state",
                "workflow-retirement-batch",
                fixture["plan"],
            ) as journal:
                journal.persist(
                    {
                        "phase": "in_progress",
                        "inventory": prepared,
                        "inventory_sha256": canonical_sha256(prepared),
                        "members": [
                            {"workflow_id": workflow_id(0), "status": "pending"}
                        ],
                        "effects": {
                            f"delete:{workflow_id(0)}": {
                                **identity,
                                "status": "verified",
                                "attempt": 1,
                                "child": None,
                            }
                        },
                    }
                )
            report = retire_workflow_batch(fixture["plan"], mode="resume")
            self.assertEqual(report["phase"], "complete")
            self.assertEqual(
                len([row for row in read_log(fixture) if "--confirm" in row]),
                1,
            )

    def test_d9_multi_member_progresses_against_each_exact_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary), count=2)
            report = retire_workflow_batch(fixture["plan"], mode="apply")
            self.assertEqual(report["members_verified"], 2)
            confirmed = [row for row in read_log(fixture) if "--confirm" in row]
            self.assertEqual(len(confirmed), 2)

    def test_d9_final_convergence_rechecks_retained_session_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            retire_workflow_batch(fixture["plan"], mode="apply")
            session_file = fixture["session_root"] / "SESSION-1" / "events.jsonl"
            session_file.write_text("changed\n")
            with self.assertRaises(EffectBlocked):
                retire_workflow_batch(fixture["plan"], mode="resume")

    def test_d9_rejects_an_inexact_sqlite_schema_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            database = sqlite3.connect(fixture["database"])
            database.execute("ALTER TABLE sessions ADD COLUMN foreign_value TEXT")
            database.commit()
            database.close()
            with self.assertRaises(operations.OperationError):
                retire_workflow_batch(fixture["plan"], mode="preview")


if __name__ == "__main__":
    unittest.main()
