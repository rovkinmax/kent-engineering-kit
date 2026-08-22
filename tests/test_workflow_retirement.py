from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
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
                "execution_target_policy": {{"mode": workflow["metadata"]["execution_target"]}},
                "project_link": {{"default": workflow["default"]}},
            }})
    emit(rows, "workflows")
elif args == ["worktree", "list", "--json"]:
    print(json.dumps({{"worktrees": state.get("worktrees", [])}}, sort_keys=True))
elif args[:2] == ["workflow", "inspect"]:
    workflow_id = args[2]
    workflow = state["workflows"][workflow_id]
    if not workflow.get("present", True):
        print("workflow not found", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({{"workflow": {{
        "id": workflow_id,
        "name": workflow["metadata"]["name"],
        "description": workflow["metadata"]["description"],
        "version": workflow["version"],
        "execution_target_policy": {{"mode": workflow["metadata"]["execution_target"]}},
    }}}}, sort_keys=True))
elif args[:3] == ["workflow", "graph", "inspect"]:
    workflow_id = args[3]
    workflow = state["workflows"][workflow_id]
    print(json.dumps({{
        "workflow_id": workflow_id,
        "expected_version": workflow["version"],
        "graph": workflow["graph"],
    }}, sort_keys=True))
elif args[:2] == ["workflow", "validate"]:
    print(json.dumps({{"valid": True}}, sort_keys=True))
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
        print(json.dumps({{"workflow_id": workflow_id, "sha256": workflow["preview_sha256"]}}))
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
            "preview_sha256": chr(ord("c") + index) * 64,
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


class WorkflowRetirementTest(unittest.TestCase):
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
            with OperationJournal(root / "state", "effect-test", plan) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                result = run_effect(
                    journal,
                    effect_key="bounded",
                    command=[PYTHON, "-c", script],
                    cwd=root,
                    timeout=5,
                    stdin_bytes=b"z" * operations.MAX_OUTPUT,
                    preimage_sha256="a" * 64,
                    postimage_sha256="b" * 64,
                    current_sha256=lambda: "b" * 64,
                )
                self.assertEqual(result.settlement, "postimage")

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
