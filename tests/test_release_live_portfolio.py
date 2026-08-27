from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import workflowkit.operations as operations
from workflowkit.operations import (
    EffectBlocked,
    PlanValidationError,
    canonical_bytes,
    load_plan,
    reconcile_release_live_portfolio,
)

PYTHON = str(Path(sys.executable).resolve())
PROJECT_IDS = [f"123e4567-e89b-12d3-a456-{100000000000 + index:012d}" for index in range(4)]
RETIREMENT_IDS = [f"223e4567-e89b-12d3-a456-{100000000000 + index:012d}" for index in range(6)]
CANONICAL_IDS = [f"323e4567-e89b-12d3-a456-{100000000000 + index:012d}" for index in range(4)]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_database(path: Path, task_rows: list[tuple[str, str]]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY);
        CREATE TABLE workspaces (id TEXT PRIMARY KEY);
        CREATE TABLE worktrees (id TEXT PRIMARY KEY);
        CREATE TABLE tasks (id TEXT PRIMARY KEY);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
            worktree_id TEXT REFERENCES worktrees(id) ON DELETE SET NULL,
            artifact_relpath TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
            first_prompt_preview TEXT NOT NULL DEFAULT '', input_draft TEXT NOT NULL DEFAULT '', category TEXT,
            created_at_unix_ms INTEGER NOT NULL, updated_at_unix_ms INTEGER NOT NULL,
            last_sequence INTEGER NOT NULL DEFAULT 0, model_request_count INTEGER NOT NULL DEFAULT 0,
            launch_visible INTEGER NOT NULL DEFAULT 0, cwd_relpath TEXT NOT NULL DEFAULT '.',
            continuation_json TEXT NOT NULL DEFAULT '{}', locked_json TEXT NOT NULL DEFAULT '{}',
            usage_state_json TEXT NOT NULL DEFAULT '{}', metadata_json TEXT NOT NULL DEFAULT '{}',
            previous_session_id TEXT, parent_agent_session_id TEXT,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            completed_compaction_count INTEGER, manual_compact_eligible INTEGER
        );
        CREATE TABLE session_workflow_node_associations (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            node_id BLOB NOT NULL, transition_branch_key TEXT, association_status TEXT NOT NULL,
            source_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT,
            associated_at_unix_ms INTEGER NOT NULL
        );
        """
    )
    for project_id in PROJECT_IDS:
        connection.execute("INSERT INTO projects(id) VALUES (?)", (project_id,))
    for task_id, session_id in task_rows:
        connection.execute("INSERT INTO tasks(id) VALUES (?)", (task_id,))
        connection.execute(
            """INSERT INTO sessions(
                id, project_id, artifact_relpath, created_at_unix_ms, updated_at_unix_ms, task_id
            ) VALUES (?, ?, ?, 1, 1, ?)""",
            (session_id, PROJECT_IDS[0], f"sessions/{session_id}", task_id),
        )
        connection.execute(
            """INSERT INTO session_workflow_node_associations(
                task_id, session_id, node_id, association_status, associated_at_unix_ms
            ) VALUES (?, ?, ?, 'historical', 1)""",
            (task_id, session_id, b"\x01" * 16),
        )
    connection.commit()
    connection.close()


def make_fixture(root: Path) -> dict:
    roots = [root / f"project-{index + 1}" for index in range(4)]
    for project_root in roots:
        project_root.mkdir()
    session_root = root / "sessions"
    session_root.mkdir()
    task_rows: list[tuple[str, str]] = []
    workflows: dict[str, dict] = {}
    retirement_members = []
    for index, workflow_id in enumerate(RETIREMENT_IDS):
        project_index = index % 2
        task_rows_for_member = []
        sessions = []
        for offset in range(2):
            task_id = f"RET-TASK-{index + 1}-{offset + 1}"
            session_id = f"RET-SESSION-{index + 1}-{offset + 1}"
            task_rows.append((task_id, session_id))
            task_rows_for_member.append(task_id)
            directory = session_root / session_id
            directory.mkdir()
            (directory / "events.jsonl").write_text(f"{session_id}\n")
            sessions.append({
                "id": session_id, "status": "idle", "task_id": task_id, "retained": True,
                "live_owner": None, "root": str(session_root), "relative": session_id,
                "manifest": operations._session_manifest(directory),
            })
        workflows[workflow_id] = workflow_state(
            workflow_id, task_rows_for_member, sessions, version=1,
            project_id=PROJECT_IDS[project_index], execution_target="none",
        )
        retirement_members.append(member_plan(
            index, PROJECT_IDS[project_index], roots[project_index], workflow_id,
            task_rows_for_member, sessions, version=1,
        ))
    canonical_workflows = []
    for index, workflow_id in enumerate(CANONICAL_IDS):
        task_id = f"CAN-TASK-{index + 1}"
        workflows[workflow_id] = workflow_state(
            workflow_id, [task_id], [], version=1, project_id=PROJECT_IDS[index], execution_target="none",
        )
        canonical_workflows.append(canonical_plan(
            index, PROJECT_IDS[index], roots[index], workflow_id, task_id,
        ))
    database = root / "kent.sqlite"
    create_database(database, task_rows)
    state_path = root / "kent-state.json"
    log_path = root / "kent-argv.jsonl"
    state = {
        "projects": [{"id": project_id, "name": f"Project {index + 1}", "path": str(roots[index])}
                     for index, project_id in enumerate(PROJECT_IDS)],
        "workflows": workflows,
        "database": str(database),
        "state_path": str(state_path),
        "log_path": str(log_path),
    }
    state_path.write_text(json.dumps(state, sort_keys=True))
    kent = root / "fake-kent"
    kent.write_text(fake_kent_source(state_path, log_path))
    kent.chmod(0o755)
    value = {
        "schema": "release-live-portfolio-plan-v1",
        "state_dir": str(root / "state"),
        "kent": {"path": str(kent), "sha256": digest(kent)},
        "database": {
            "path": str(database), "schema": "kent-2.6.1", "session_roots": [str(session_root)],
        },
        "retirement_members": retirement_members,
        "canonical_workflows": canonical_workflows,
    }
    plan_path = root / "plan.json"
    plan_path.write_bytes(canonical_bytes(value))
    plan = load_plan(
        plan_path, schema="release-live-portfolio-plan-v1", expected_sha256=digest(plan_path),
    )
    return {"root": root, "state_path": state_path, "log_path": log_path, "kent": kent,
            "plan": plan, "value": value, "database": database}


def workflow_state(workflow_id: str, task_ids: list[str], sessions: list[dict], *, version: int,
                   project_id: str, execution_target: str) -> dict:
    status = {"kind": "done", "native_state": "terminal"}
    graph = {
        "version": version, "nodes": [{"id": "terminal", "kind": "terminal"}], "edges": [],
        "node_groups": [], "transition_groups": [],
    }
    return {
        "present": True, "project_id": project_id, "version": version, "default": True,
        "metadata": {"name": workflow_id, "description": "workflow", "execution_target": execution_target},
        "graph": graph, "tasks": [{"task_id": task_id, "status": status} for task_id in task_ids],
        "details": {
            task_id: {"summary": {"id": task_id, "done": True}, "status": status,
                      "current_nodes": [{"node_id": "terminal"}], "pending_approvals": [],
                      "live_sessions": [],
                      "retained_session_count": len([s for s in sessions if s["task_id"] == task_id])}
            for task_id in task_ids
        },
        "sessions": {task_id: [session for session in sessions if session["task_id"] == task_id]
                     for task_id in task_ids},
    }


def member_plan(sequence: int, project_id: str, project_root: Path, workflow_id: str,
                task_ids: list[str], sessions: list[dict], *, version: int) -> dict:
    status = "done"
    return {
        "sequence": sequence, "project_id": project_id, "project_root": str(project_root),
        "workflow_id": workflow_id, "revision": version,
        "links": [{"project_id": project_id, "workflow_id": workflow_id, "is_default": True}],
        "default": workflow_id,
        "tasks": [{"id": task_id, "status": status, "terminal": True, "current_node": None,
                    "approval_pending": False} for task_id in task_ids],
        "sessions": sessions, "worktrees": [], "retained": [], "absent": [],
        "delete_preview": {"workflow_id": workflow_id, "sha256": "a" * 64},
    }


def canonical_plan(sequence: int, project_id: str, project_root: Path, workflow_id: str, task_id: str) -> dict:
    return {
        "sequence": sequence, "project_id": project_id, "project_root": str(project_root),
        "workflow_id": workflow_id, "expected_version": 1, "intent": "graph-and-metadata",
        "graph": {"version": 2, "nodes": [{"id": "terminal", "kind": "terminal"},
                                              {"id": "review", "kind": "agent"}], "edges": [],
                   "node_groups": [], "transition_groups": []},
        "metadata": {"name": f"Canonical {sequence}", "description": "updated", "execution_target": "none"},
        "terminal_tasks": [{"id": task_id, "status": "done"}],
        "terminal_anchors": [{"id": "terminal", "kind": "terminal"}],
        "links": [{"project_id": project_id, "workflow_id": workflow_id, "is_default": True}],
        "default": workflow_id,
    }


def fake_kent_source(state_path: Path, log_path: Path) -> str:
    source = '''#!/usr/bin/env python3
import json, sqlite3, sys
from pathlib import Path
from types import SimpleNamespace
STATE = Path(__STATE_PATH__)
LOG = Path(__LOG_PATH__)
args = sys.argv[1:]
with LOG.open("a") as stream:
    stream.write(json.dumps(args, separators=(",", ":")) + "\\n")
state = json.loads(STATE.read_text())

def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default

def emit(rows, key):
    offset = int(option("--offset", "0")); limit = int(option("--limit", "100"))
    page = rows[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(rows) else None
    print(json.dumps({key: page, "next_offset": next_offset}, sort_keys=True))

def save():
    STATE.write_text(json.dumps(state, sort_keys=True))

def workflow_for_task(task_id):
    for item in state["workflows"].values():
        if task_id in item.get("details", {}):
            return item
    raise KeyError(task_id)

if args == ["project", "list"]:
    for item in state["projects"]:
        print(item["id"] + "\\t" + item["name"] + "\\t" + item["path"])
elif args[:2] == ["workflow", "list"]:
    project_id = option("--project")
    rows = []
    for wid, item in sorted(state["workflows"].items()):
        if item.get("present", True) and item["project_id"] == project_id:
            rows.append({"id": wid, "name": item["metadata"]["name"], "description": item["metadata"]["description"],
                         "version": item["version"],
                         "execution_target_policy": {"mode": item["metadata"]["execution_target"]},
                         "project_link": {"default": item["default"]}})
    emit(rows, "workflows")
elif args == ["worktree", "list", "--json"]:
    print(json.dumps({"worktrees": []}))
elif args[:2] == ["workflow", "inspect"]:
    wid = args[2]; item = state["workflows"][wid]
    if not item.get("present", True):
        print("workflow not found", file=sys.stderr); raise SystemExit(1)
    print(json.dumps({"workflow": {"id": wid, "name": item["metadata"]["name"],
        "description": item["metadata"]["description"],
        "version": item["version"],
        "execution_target_policy": {"mode": item["metadata"]["execution_target"]}}}, sort_keys=True))
elif args[:3] == ["workflow", "graph", "inspect"]:
    wid = args[3]; item = state["workflows"][wid]
    print(json.dumps({"workflow_id": wid, "expected_version": item["version"], "graph": item["graph"]}, sort_keys=True))
elif args[:2] == ["workflow", "validate"]:
    print(json.dumps({"valid": True}, sort_keys=True))
elif args[:2] == ["task", "list"]:
    item = state["workflows"][option("--workflow")]
    emit(item.get("tasks", []) if item.get("present", True) else [], "tasks")
elif args[:2] == ["task", "show"]:
    task_id = args[2]; item = workflow_for_task(task_id)
    print(json.dumps(item["details"][task_id], sort_keys=True))
elif args[:2] == ["task", "sessions"]:
    task_id = args[2]; item = workflow_for_task(task_id)
    emit(item.get("sessions", {}).get(task_id, []), "items")
elif args[:2] == ["workflow", "delete"]:
    wid = args[2]; item = state["workflows"][wid]
    if "--confirm" not in args:
        print(json.dumps({"workflow_id": wid, "sha256": "a" * 64}))
    else:
        task_ids = [row["task_id"] for row in item.get("tasks", [])]
        connection = sqlite3.connect(state["database"])
        for task_id in task_ids:
            connection.execute("UPDATE sessions SET task_id = NULL WHERE task_id = ?", (task_id,))
            connection.execute("DELETE FROM session_workflow_node_associations WHERE task_id = ?", (task_id,))
        connection.commit(); connection.close()
        item["present"] = False; item["tasks"] = []; item["details"] = {}; item["sessions"] = {}
        save(); print(json.dumps({"deleted": True}))
elif args[:3] == ["workflow", "graph", "apply"]:
    document = json.loads(sys.stdin.read()); item = state["workflows"][document["workflow_id"]]
    if document["expected_version"] != item["version"]:
        print("version mismatch", file=sys.stderr); raise SystemExit(1)
    item["version"] += 1; item["graph"] = {"version": item["version"], **document["graph"]}
    save(); print(json.dumps({"version": item["version"]}))
elif args[:2] == ["workflow", "update"]:
    item = state["workflows"][args[2]]
    item["metadata"] = {
        "name": option("--name"), "description": option("--description"),
        "execution_target": option("--execution-target"),
    }
    save(); print(json.dumps({"updated": True}))
else:
    print("unsupported fake Kent argv: " + repr(args), file=sys.stderr); raise SystemExit(2)
'''
    return source.replace("__STATE_PATH__", repr(str(state_path))).replace("__LOG_PATH__", repr(str(log_path)))


def read_state(fixture: dict) -> dict:
    return json.loads(fixture["state_path"].read_text())


def write_state(fixture: dict, state: dict) -> None:
    fixture["state_path"].write_text(json.dumps(state, sort_keys=True))


def read_log(fixture: dict) -> list[list[str]]:
    if not fixture["log_path"].exists():
        return []
    return [json.loads(row) for row in fixture["log_path"].read_text().splitlines()]


def effect_entry(cwd: Path, status: str, identity: dict | None = None) -> dict:
    identity = identity or {
        "command_digest": "a" * 64, "cwd": str(cwd), "environment_sha256": "b" * 64,
        "stdin_sha256": "c" * 64, "preimage_sha256": "d" * 64, "postimage_sha256": "e" * 64,
    }
    return {**identity, "status": status, "attempt": 1, "child": None}


def planned_effect_identity(parsed: dict, state: dict, key: str, applied: dict[str, int] | None = None) -> dict:
    stages = {
        item['workflow_id']: operations._canonical_progress(
            {'kent': parsed['kent'], 'project_root': item['project_root']}, item,
            state['canonical'][item['workflow_id']],
        )
        for item in parsed['canonical_workflows']
    }
    restore = {}
    for item in parsed['canonical_workflows']:
        wid = item['workflow_id']
        count = len(stages[wid]) if applied is None else applied.get(wid, 0)
        start = state['canonical'][wid] if count == 0 else stages[wid][count - 1]['after']
        restore[wid] = operations._canonical_restore_progress(
            {'kent': parsed['kent'], 'project_root': item['project_root']}, item,
            state['canonical'][wid], start,
        ) if count else []
    return operations._portfolio_effect_identity(
        parsed, key, {'members': state['retirement']}, {'members': state['canonical']}, stages, restore,
    )


def planned_effect(parsed: dict, state: dict, key: str, status: str, applied: dict[str, int] | None = None) -> dict:
    return effect_entry(Path(parsed['state_dir']), status, planned_effect_identity(parsed, state, key, applied))


def planned_forward_effects(parsed: dict, state: dict, applied: dict[str, int] | None = None) -> dict:
    effects = {
        f'delete:{wid}': planned_effect(parsed, state, f'delete:{wid}', 'verified')
        for wid in RETIREMENT_IDS
    }
    for item in parsed['canonical_workflows']:
        wid = item['workflow_id']
        stages = operations._canonical_progress(
            {'kent': parsed['kent'], 'project_root': item['project_root']}, item, state['canonical'][wid],
        )
        count = len(stages) if applied is None else applied.get(wid, 0)
        effects.update({
            f'apply:{wid}:{stage["name"]}': planned_effect(
                parsed, state, f'apply:{wid}:{stage["name"]}', 'verified', applied,
            )
            for stage in stages[:count]
        })
    return effects


def _source_validation_copy() -> bool:
    return not (Path(__file__).resolve().parents[1] / '.git').exists()


class ReleaseLivePortfolioTest(unittest.TestCase):
    def _read_fixture(self, fixture: dict) -> tuple[dict, dict]:
        parsed = operations._validate_release_live_portfolio_plan(fixture['plan'])
        live = operations._portfolio_read_state(parsed)
        return parsed, live

    def _prepare_mocked(self, fixture: dict, live: dict) -> Path:
        with (
            mock.patch.object(operations, '_portfolio_read_state', return_value=live),
            mock.patch.object(operations, '_portfolio_project_rows', return_value=live['projects']),
        ):
            reconcile_release_live_portfolio(fixture['plan'], mode='prepare', kent=fixture['kent'])
        return fixture['root'] / 'state' / 'release-live-portfolio.journal.json'

    def _load_journal(self, fixture: dict) -> tuple[Path, dict]:
        path = fixture['root'] / 'state' / 'release-live-portfolio.journal.json'
        return path, json.loads(path.read_text())

    def _write_journal(self, path: Path, journal: dict) -> None:
        path.write_bytes(canonical_bytes(journal) + b'\n')

    def _make_restore_window(
        self, fixture: dict, applied_count: int, restore_verified: int,
        restore_status: str | None = None, member_status: str | None = None,
    ) -> tuple[dict, dict, Path, dict, list[dict], str]:
        parsed, preimage = self._read_fixture(fixture)
        journal_path = self._prepare_mocked(fixture, preimage)
        first = CANONICAL_IDS[0]
        item = next(row for row in parsed['canonical_workflows'] if row['workflow_id'] == first)
        forward = operations._canonical_progress(
            {'kent': parsed['kent'], 'project_root': item['project_root']}, item, preimage['canonical'][first],
        )
        start = forward[applied_count - 1]['after']
        restores = operations._canonical_restore_progress(
            {'kent': parsed['kent'], 'project_root': item['project_root']}, item,
            preimage['canonical'][first], start,
        )
        status = member_status or (forward[applied_count - 1]['status'] if applied_count else 'pending')
        journal = json.loads(journal_path.read_text())
        journal['phase'] = 'canonical_in_progress'
        journal['members'] = [
            {'workflow_id': row['workflow_id'],
             'status': 'verified' if row['workflow_id'] in RETIREMENT_IDS else (
                 status if row['workflow_id'] == first else 'pending')}
            for row in journal['members']
        ]
        journal['effects'] = planned_forward_effects(parsed, preimage, {first: applied_count})
        if applied_count < len(forward):
            key = f'apply:{first}:{forward[applied_count]["name"]}'
            journal['effects'][key] = planned_effect(
                parsed, preimage, key, 'settled_preimage', {first: applied_count},
            )
        for index, stage in enumerate(restores):
            if index < restore_verified:
                status_value = 'verified'
            elif index == restore_verified and restore_status is not None:
                status_value = restore_status
            else:
                continue
            key = f'restore:{first}:{stage["name"]}'
            journal['effects'][key] = planned_effect(
                parsed, preimage, key, status_value, {first: applied_count},
            )
        self._write_journal(journal_path, journal)
        current = operations._portfolio_expected_post(parsed, {
            'projects': preimage['projects'], 'database_schema': preimage['database_schema'],
            'sqlite': preimage['sqlite'], 'retirement': preimage['retirement'], 'canonical': preimage['canonical'],
        })
        current['canonical'] = copy.deepcopy(preimage['canonical'])
        current['canonical'][first] = start
        for stage in restores[:restore_verified]:
            current['canonical'][first] = stage['after']
        return parsed, preimage, journal_path, current, restores, first

    def test_cli_contract_and_preview_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            command = [
                str(Path('scripts/reconcile-release-portfolio')), 'preview',
                '--plan', str(fixture['root'] / 'plan.json'),
                '--expect-plan-sha256', fixture['plan'].sha256, '--kent', str(fixture['kent']),
            ]
            relative = subprocess.run(
                [*command[0:3], 'plan.json', *command[4:]],
                check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(relative.returncode, 0)
            unknown = subprocess.run(
                [*command, '--report', 'report.json'], check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(unknown.returncode, 0)
            parsed, live = self._read_fixture(fixture)
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=live),
                mock.patch.object(operations, '_portfolio_project_rows', return_value=live['projects']),
            ):
                report = reconcile_release_live_portfolio(fixture['plan'], mode='preview', kent=fixture['kent'])
            self.assertEqual(report['schema'], 'release-live-portfolio-report-v1')
            self.assertFalse((fixture['root'] / 'state').exists())
            self.assertEqual(parsed['schema'], 'kent-2.6.1')

    def test_prepare_persists_pre_d9_receipt_and_retire_needs_effect_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=live),
                mock.patch.object(operations, '_portfolio_project_rows', return_value=live['projects']),
            ):
                prepared = reconcile_release_live_portfolio(
                    fixture['plan'], mode='prepare', kent=fixture['kent']
                )
            self.assertEqual(prepared['phase'], 'prepared')
            journal_path = fixture['root'] / 'state' / 'release-live-portfolio.journal.json'
            journal = json.loads(journal_path.read_text())
            self.assertEqual(journal['phase'], 'prepared')
            self.assertEqual(journal['effects'], {})
            self.assertEqual(set(journal['canonical_preimage']['members']), set(CANONICAL_IDS))
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=live),
                mock.patch.object(operations, '_portfolio_project_rows', return_value=live['projects']),
                mock.patch.object(operations, '_settle_or_run', return_value='preimage'),
            ):
                resumed = reconcile_release_live_portfolio(
                    fixture['plan'], mode='retire', kent=fixture['kent'], confirm=fixture['plan'].sha256
                )
            self.assertEqual(resumed['phase'], 'retirement_in_progress')
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256
                )
            self.assertEqual(parsed['project_ids'], sorted(parsed['project_ids']))

    def test_effect_recovery_entries_without_optional_fields_are_closed_and_resumable(self) -> None:
        for status in ('attempted', 'unresolved'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                fixture = make_fixture(Path(temporary))
                parsed, live = self._read_fixture(fixture)
                journal_path = self._prepare_mocked(fixture, live)
                journal = json.loads(journal_path.read_text())
                wid = RETIREMENT_IDS[0]
                journal['phase'] = 'retirement_in_progress'
                journal['effects'] = {
                    f'delete:{wid}': planned_effect(parsed, live, f'delete:{wid}', status),
                }
                self._write_journal(journal_path, journal)
                with (
                    mock.patch.object(operations, '_portfolio_read_state', return_value=live),
                    mock.patch.object(operations, '_settle_or_run', return_value='preimage'),
                ):
                    report = reconcile_release_live_portfolio(
                        fixture['plan'], mode='retire', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )
                self.assertEqual(report['phase'], 'retirement_in_progress')

    def test_phase_member_and_effect_grammar_rejects_impossible_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, live)
            journal = json.loads(journal_path.read_text())
            journal['members'][-1]['status'] = 'graph_verified'
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='retire', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )
            journal['members'][-1]['status'] = 'pending'
            journal['phase'] = 'retirement_in_progress'
            journal['effects'] = {
                f'delete:{RETIREMENT_IDS[0]}': planned_effect(
                    parsed, live, f'delete:{RETIREMENT_IDS[0]}', 'attempted',
                ),
                f'delete:{RETIREMENT_IDS[1]}': planned_effect(
                    parsed, live, f'delete:{RETIREMENT_IDS[1]}', 'unresolved',
                ),
            }
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='retire', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )

    def test_cross_project_and_d9_drift_block_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, live)
            drifted = copy.deepcopy(live)
            drifted['retirement'][RETIREMENT_IDS[1]]['preview_sha256'] = 'f' * 64
            settle = mock.Mock(return_value='postimage')
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=drifted),
                mock.patch.object(operations, '_settle_or_run', settle),
            ):
                with self.assertRaises(EffectBlocked):
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='retire', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )
            settle.assert_not_called()
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'd9_complete'
            journal['members'] = [
                {'workflow_id': row['workflow_id'], 'status': 'verified'}
                if row['workflow_id'] in RETIREMENT_IDS
                else row
                for row in journal['members']
            ]
            journal['effects'] = {
                f'delete:{wid}': planned_effect(parsed, live, f'delete:{wid}', 'verified')
                for wid in RETIREMENT_IDS
            }
            self._write_journal(journal_path, journal)
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=live),
                mock.patch.object(operations, '_settle_or_run', settle),
            ):
                with self.assertRaises(EffectBlocked):
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )
            settle.assert_not_called()

    def test_validation_rejects_unknown_fields_collisions_and_wrong_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            for mutation in (
                lambda value: value.update(unknown=True),
                lambda value: value['retirement_members'][1].update(
                    sequence=value['retirement_members'][0]['sequence']
                ),
                lambda value: value['canonical_workflows'][0].update(
                    workflow_id=value['retirement_members'][0]['workflow_id']
                ),
                lambda value: value['canonical_workflows'][0].update(
                    project_root=value['retirement_members'][0]['project_root'] + '/other'
                ),
                lambda value: value['canonical_workflows'][0].update(
                    project_root=value['canonical_workflows'][1]['project_root']
                ),
            ):
                value = json.loads(json.dumps(fixture['value']))
                mutation(value)
                path = fixture['root'] / 'invalid.json'
                path.write_bytes(canonical_bytes(value))
                plan = load_plan(path, schema='release-live-portfolio-plan-v1', expected_sha256=digest(path))
                with self.assertRaises((PlanValidationError, EffectBlocked)):
                    reconcile_release_live_portfolio(plan, mode='preview', kent=fixture['kent'])
            with self.assertRaises(PlanValidationError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='retire', kent=fixture['kent'], confirm='0' * 64
                )

    def test_foreign_journal_bytes_are_rejected_before_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            _parsed, live = self._read_fixture(fixture)
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=live),
                mock.patch.object(operations, '_portfolio_project_rows', return_value=live['projects']),
            ):
                reconcile_release_live_portfolio(fixture['plan'], mode='prepare', kent=fixture['kent'])
            journal_path = fixture['root'] / 'state' / 'release-live-portfolio.journal.json'
            journal = json.loads(journal_path.read_text())
            journal['members'][0]['status'] = 'foreign'
            journal_path.write_bytes(canonical_bytes(journal) + b'\n')
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='retire', kent=fixture['kent'], confirm=fixture['plan'].sha256
                )


    @unittest.skipIf(_source_validation_copy(), 'source validation copy omits the long integration run')
    def test_end_to_end_retire_apply_preserves_d9_poststate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            prepared = reconcile_release_live_portfolio(
                fixture['plan'], mode='prepare', kent=fixture['kent']
            )
            self.assertEqual(prepared['phase'], 'prepared')
            retired = reconcile_release_live_portfolio(
                fixture['plan'], mode='retire', kent=fixture['kent'], confirm=fixture['plan'].sha256
            )
            self.assertEqual(retired['phase'], 'd9_complete')
            completed = reconcile_release_live_portfolio(
                fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256
            )
            self.assertEqual(completed['phase'], 'complete')
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )
            state = read_state(fixture)
            self.assertTrue(all(not state['workflows'][wid]['present'] for wid in RETIREMENT_IDS))
            self.assertTrue(all(state['workflows'][wid]['present'] for wid in CANONICAL_IDS))
            self.assertTrue(all(state['workflows'][wid]['version'] == 2 for wid in CANONICAL_IDS))
            self.assertEqual(
                [row for row in read_log(fixture) if row[:2] == ['workflow', 'delete'] and '--confirm' in row],
                [['workflow', 'delete', wid, '--confirm', '--json'] for wid in RETIREMENT_IDS],
            )
            graph_calls = [row for row in read_log(fixture) if row[:3] == ['workflow', 'graph', 'apply']]
            update_calls = [row for row in read_log(fixture) if row[:2] == ['workflow', 'update']]
            self.assertEqual(len(graph_calls), 4)
            self.assertEqual(len(update_calls), 4)
            mutation_rows = [
                row for row in read_log(fixture)
                if row[:2] == ['workflow', 'delete'] and '--confirm' in row
                or row[:3] == ['workflow', 'graph', 'apply']
                or row[:2] == ['workflow', 'update']
            ]
            self.assertEqual(len(mutation_rows), 14)
            forbidden = {'create', 'link', 'unlink', 'default', 'task', 'sqlite'}
            self.assertFalse(any(row[0] in forbidden for row in mutation_rows))
            connection = sqlite3.connect(fixture['database'])
            try:
                remaining = connection.execute(
                    'SELECT COUNT(*) FROM sessions WHERE task_id IS NOT NULL'
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(remaining, 0)

    def test_canonical_ack_loss_allows_only_current_stage_pre_or_postimage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            wid = CANONICAL_IDS[0]
            item = next(row for row in parsed['canonical_workflows'] if row['workflow_id'] == wid)
            before = live['canonical'][wid]
            stage = operations._canonical_progress(
                {'kent': parsed['kent'], 'project_root': item['project_root']}, item, before
            )[0]
            journal = SimpleNamespace(state={
                'canonical_preimage': {'members': copy.deepcopy(live['canonical'])},
                'members': [
                    {'workflow_id': row['workflow_id'], 'status': 'pending'}
                    for row in parsed['canonical_workflows']
                ],
                'effects': {f'apply:{wid}:graph': {'status': 'verified'}},
            })
            post = copy.deepcopy(live)
            post['canonical'][wid] = stage['after']
            operations._portfolio_canonical_state_gate(parsed, post, journal, (wid, 'graph'))
            metadata_stage = operations._canonical_progress(
                {'kent': parsed['kent'], 'project_root': item['project_root']}, item, before
            )[1]
            metadata_journal = copy.deepcopy(journal)
            metadata_journal.state['members'][CANONICAL_IDS.index(wid)]['status'] = 'graph_verified'
            metadata_journal.state['effects'] = {
                f'apply:{wid}:metadata': {'status': 'verified'},
            }
            metadata_post = copy.deepcopy(live)
            metadata_post['canonical'][wid] = metadata_stage['after']
            operations._portfolio_canonical_state_gate(
                parsed, metadata_post, metadata_journal, (wid, 'metadata')
            )
            foreign = copy.deepcopy(post)
            foreign['canonical'][CANONICAL_IDS[1]]['metadata']['name'] = 'foreign drift'
            with self.assertRaises(EffectBlocked):
                operations._portfolio_canonical_state_gate(parsed, foreign, journal, (wid, 'graph'))

    def test_nested_journal_fields_and_legacy_v1_grammar_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=live),
                mock.patch.object(operations, '_portfolio_project_rows', return_value=live['projects']),
            ):
                reconcile_release_live_portfolio(fixture['plan'], mode='prepare', kent=fixture['kent'])
            journal_path = fixture['root'] / 'state' / 'release-live-portfolio.journal.json'
            journal = json.loads(journal_path.read_text())
            journal['canonical_preimage']['members'][CANONICAL_IDS[0]]['metadata']['foreign'] = True
            journal_path.write_bytes(canonical_bytes(journal) + b'\n')
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='retire', kent=fixture['kent'], confirm=fixture['plan'].sha256
                )
            legacy_plan_path = fixture['root'] / 'legacy-plan.json'
            legacy_value = {'schema': 'workflow-retirement-batch-plan-v2'}
            legacy_plan_path.write_bytes(canonical_bytes(legacy_value))
            legacy_plan = load_plan(
                legacy_plan_path, schema='workflow-retirement-batch-plan-v2',
                expected_sha256=digest(legacy_plan_path),
            )
            legacy_dir = fixture['root'] / 'legacy-state'
            with operations.OperationJournal(legacy_dir, 'effect-test', legacy_plan) as legacy:
                legacy.persist({'phase': 'in_progress', 'effects': {}})
                with self.assertRaises(operations.JournalError):
                    legacy.persist({'phase': 'd9_complete', 'effects': {}})
                with self.assertRaises(operations.JournalError):
                    legacy.persist({
                        'phase': 'in_progress', 'effects': {}, 'canonical_preimage': {},
                    })

    @unittest.skipIf(_source_validation_copy(), 'source validation copy omits rollback acknowledgement windows')
    @unittest.skipIf(_source_validation_copy(), 'source validation copy omits one-way rollback guard windows')
    def test_apply_is_one_way_after_any_rollback_receipt(self) -> None:
        for restore_status in ('attempted', 'unresolved', 'ambiguous', 'settled_preimage', 'verified'):
            with self.subTest(restore_status=restore_status), tempfile.TemporaryDirectory() as temporary:
                fixture = make_fixture(Path(temporary))
                _parsed, _preimage, journal_path, current, _restores, _first = self._make_restore_window(
                    fixture, 1, 0, restore_status,
                )
                original = journal_path.read_bytes()
                with (
                    mock.patch.object(operations, '_portfolio_read_state', return_value=current),
                    mock.patch.object(operations, '_portfolio_d9_post_gate') as d9,
                    mock.patch.object(operations, '_settle_or_run') as run,
                ):
                    with self.assertRaises(operations.JournalError):
                        reconcile_release_live_portfolio(
                            fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                        )
                self.assertEqual(journal_path.read_bytes(), original)
                d9.assert_not_called()
                run.assert_not_called()

    @unittest.skipIf(_source_validation_copy(), 'source validation copy omits one-way rollback guard windows')
    def test_apply_rejects_rolled_back_member_and_verified_restore_lag(self) -> None:
        for restore_verified, member_status in ((1, 'rolled_back'), (1, 'pending')):
            with self.subTest(member_status=member_status), tempfile.TemporaryDirectory() as temporary:
                fixture = make_fixture(Path(temporary))
                _parsed, _preimage, journal_path, current, _restores, _first = self._make_restore_window(
                    fixture, 1, restore_verified, member_status=member_status,
                )
                original = journal_path.read_bytes()
                with mock.patch.object(operations, '_portfolio_read_state', return_value=current):
                    with self.assertRaises(operations.JournalError):
                        reconcile_release_live_portfolio(
                            fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                        )
                self.assertEqual(journal_path.read_bytes(), original)

    @unittest.skipIf(_source_validation_copy(), 'source validation copy omits one-way rollback guard windows')
    def test_complete_with_restore_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, live)
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'complete'
            journal['members'] = [
                {'workflow_id': row['workflow_id'], 'status': 'verified'} for row in journal['members']
            ]
            journal['effects'] = planned_forward_effects(parsed, live)
            first = CANONICAL_IDS[0]
            key = f'restore:{first}:graph'
            journal['effects'][key] = planned_effect(parsed, live, key, 'verified')
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )

    def test_restore_acknowledgements_recover_with_terminal_apply_noop(self) -> None:
        for restore_status in ('attempted', 'unresolved', 'ambiguous', 'settled_preimage'):
            with self.subTest(restore_status=restore_status), tempfile.TemporaryDirectory() as temporary:
                fixture = make_fixture(Path(temporary))
                parsed, preimage, journal_path, current, restores, first = self._make_restore_window(
                    fixture, 1, 0, restore_status,
                )
                def settle(journal_state, **kwargs):
                    key = kwargs['effect_key']
                    stage = restores[0]
                    self.assertEqual(key, f'restore:{first}:{stage["name"]}')
                    current['canonical'][first] = stage['after']
                    effects = dict(journal_state.state.get('effects') or {})
                    effects[key] = planned_effect(parsed, preimage, key, 'verified', {first: 1})
                    journal_state.persist({**journal_state.state, 'effects': effects})
                    return 'postimage'
                with (
                    mock.patch.object(operations, '_portfolio_read_state', return_value=current),
                    mock.patch.object(operations, '_settle_or_run', side_effect=settle) as run,
                ):
                    self.assertEqual(
                        reconcile_release_live_portfolio(
                            fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                        )['phase'], 'rolled_back',
                    )
                    self.assertEqual(run.call_count, 1)
                with (
                    mock.patch.object(operations, '_portfolio_read_state', return_value=current),
                    mock.patch.object(operations, '_settle_or_run') as rerun,
                ):
                    self.assertEqual(
                        reconcile_release_live_portfolio(
                            fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                        )['phase'], 'rolled_back',
                    )
                rerun.assert_not_called()
                self.assertEqual(
                    json.loads(journal_path.read_text())['effects'][f'apply:{first}:metadata']['status'],
                    'settled_preimage',
                )

    @unittest.skipIf(_source_validation_copy(), 'source validation copy omits rollback acknowledgement windows')
    def test_verified_restore_prefix_ack_lag_reenters_without_replay(self) -> None:
        cases = ((1, 1, 'pending'), (2, 1, 'graph_verified'), (2, 2, 'graph_verified'),
                 (2, 2, 'metadata_verified'))
        for applied_count, restore_verified, member_status in cases:
            with self.subTest(applied_count=applied_count, restore_verified=restore_verified), \
                    tempfile.TemporaryDirectory() as temporary:
                fixture = make_fixture(Path(temporary))
                parsed, preimage, journal_path, current, restores, first = self._make_restore_window(
                    fixture, applied_count, restore_verified, member_status=member_status,
                )
                remaining = restores[restore_verified:]
                def settle(journal_state, **kwargs):
                    key = kwargs['effect_key']
                    stage = next(row for row in remaining if f'restore:{first}:{row["name"]}' == key)
                    current['canonical'][first] = stage['after']
                    effects = dict(journal_state.state.get('effects') or {})
                    effects[key] = planned_effect(
                        parsed, preimage, key, 'verified', {first: applied_count},
                    )
                    journal_state.persist({**journal_state.state, 'effects': effects})
                    return 'postimage'
                with (
                    mock.patch.object(operations, '_portfolio_read_state', return_value=current),
                    mock.patch.object(operations, '_settle_or_run', side_effect=settle) as run,
                ):
                    self.assertEqual(
                        reconcile_release_live_portfolio(
                            fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                        )['phase'], 'rolled_back',
                    )
                    self.assertEqual(run.call_count, len(remaining))
                with (
                    mock.patch.object(operations, '_portfolio_read_state', return_value=current),
                    mock.patch.object(operations, '_settle_or_run') as rerun,
                ):
                    self.assertEqual(
                        reconcile_release_live_portfolio(
                            fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                        )['phase'], 'rolled_back',
                    )
                rerun.assert_not_called()
                self.assertEqual(json.loads(journal_path.read_text())['members'][6]['status'], 'rolled_back')

    @unittest.skipIf(_source_validation_copy(), 'source validation copy omits rollback acknowledgement windows')
    def test_foreign_lag_and_multiple_restore_receipts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, _preimage, journal_path, _current, _restores, _first = self._make_restore_window(
                fixture, 1, 1, member_status='metadata_verified',
            )
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, preimage, journal_path, _current, restores, first = self._make_restore_window(
                fixture, 2, 0, 'attempted', member_status='verified',
            )
            second_key = f'restore:{first}:{restores[1]["name"]}'
            journal = json.loads(journal_path.read_text())
            journal['effects'][second_key] = planned_effect(
                parsed, preimage, second_key, 'attempted', {first: 2},
            )
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )

    def test_settled_apply_receipt_is_a_rollback_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, preimage = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, preimage)
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'canonical_in_progress'
            journal['members'] = [
                {'workflow_id': row['workflow_id'],
                 'status': 'verified' if row['workflow_id'] in RETIREMENT_IDS else 'pending'}
                for row in journal['members']
            ]
            journal['effects'] = planned_forward_effects(parsed, preimage, {})
            key = f'apply:{CANONICAL_IDS[0]}:graph'
            journal['effects'][key] = planned_effect(parsed, preimage, key, 'settled_preimage', {})
            self._write_journal(journal_path, journal)
            d9 = operations._portfolio_expected_post(parsed, {
                'projects': preimage['projects'], 'database_schema': preimage['database_schema'],
                'sqlite': preimage['sqlite'], 'retirement': preimage['retirement'],
                'canonical': preimage['canonical'],
            })
            d9['canonical'] = copy.deepcopy(preimage['canonical'])
            settle = mock.Mock()
            with mock.patch.object(operations, '_portfolio_read_state', return_value=d9), \
                    mock.patch.object(operations, '_settle_or_run', settle):
                self.assertEqual(
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )['phase'], 'rolled_back',
                )
                self.assertEqual(
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )['phase'], 'rolled_back',
                )
            settle.assert_not_called()
            self.assertEqual(json.loads(journal_path.read_text())['effects'][key]['status'], 'settled_preimage')

    def test_settled_apply_after_verified_prefix_restores_only_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, preimage = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, preimage)
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'canonical_in_progress'
            first = CANONICAL_IDS[0]
            journal['members'] = [
                {'workflow_id': row['workflow_id'],
                 'status': 'verified' if row['workflow_id'] in RETIREMENT_IDS else (
                     'graph_verified' if row['workflow_id'] == first else 'pending')}
                for row in journal['members']
            ]
            journal['effects'] = planned_forward_effects(parsed, preimage, {first: 1})
            key = f'apply:{first}:metadata'
            journal['effects'][key] = planned_effect(parsed, preimage, key, 'settled_preimage', {first: 1})
            self._write_journal(journal_path, journal)
            d9 = operations._portfolio_expected_post(parsed, {
                'projects': preimage['projects'], 'database_schema': preimage['database_schema'],
                'sqlite': preimage['sqlite'], 'retirement': preimage['retirement'], 'canonical': preimage['canonical'],
            })
            d9['canonical'] = copy.deepcopy(preimage['canonical'])
            item = next(row for row in parsed['canonical_workflows'] if row['workflow_id'] == first)
            stages = operations._canonical_progress(
                {'kent': parsed['kent'], 'project_root': item['project_root']}, item, preimage['canonical'][first],
            )
            d9['canonical'][first] = stages[0]['after']
            def settle(journal_state, **kwargs):
                restore_key = kwargs['effect_key']
                before = preimage['canonical'][first]
                start = operations._portfolio_forward_state(parsed, item, before, journal_state)
                restore = operations._portfolio_restore_stages(parsed, item, before, start)
                stage = next(row for row in restore if f'restore:{first}:{row["name"]}' == restore_key)
                d9['canonical'][first] = stage['after']
                effects = dict(journal_state.state.get('effects') or {})
                effects[restore_key] = planned_effect(parsed, preimage, restore_key, 'verified', {first: 1})
                journal_state.persist({**journal_state.state, 'effects': effects})
                return 'postimage'
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=d9),
                mock.patch.object(operations, '_settle_or_run', side_effect=settle),
            ):
                self.assertEqual(
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )['phase'], 'rolled_back',
                )
                self.assertEqual(
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )['phase'], 'rolled_back',
                )
            final = json.loads(journal_path.read_text())
            self.assertEqual(final['effects'][key]['status'], 'settled_preimage')
            self.assertEqual(final['members'][6]['status'], 'rolled_back')
            self.assertTrue(all(row['status'] == 'pending' for row in final['members'][7:]))

    def test_settled_receipts_must_be_single_current_and_plan_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, preimage = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, preimage)
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'canonical_in_progress'
            journal['effects'] = planned_forward_effects(parsed, preimage, {})
            first = f'apply:{CANONICAL_IDS[0]}:graph'
            second = f'apply:{CANONICAL_IDS[0]}:metadata'
            journal['effects'][first] = planned_effect(parsed, preimage, first, 'settled_preimage', {})
            journal['effects'][second] = planned_effect(parsed, preimage, second, 'settled_preimage', {})
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )
            journal['effects'] = planned_forward_effects(parsed, preimage, {})
            journal['effects'][second] = planned_effect(parsed, preimage, second, 'settled_preimage', {})
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )
            journal['effects'] = planned_forward_effects(parsed, preimage, {})
            journal['effects'][first] = planned_effect(parsed, preimage, first, 'settled_preimage', {})
            journal['effects'][first]['stdin_sha256'] = 'f' * 64
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )

    def test_verified_effects_are_plan_bound_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, live)
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'd9_complete'
            journal['members'] = [
                {'workflow_id': row['workflow_id'],
                 'status': 'verified' if row['workflow_id'] in RETIREMENT_IDS else 'pending'}
                for row in journal['members']
            ]
            journal['effects'] = {
                f'delete:{wid}': planned_effect(parsed, live, f'delete:{wid}', 'verified')
                for wid in RETIREMENT_IDS
            }
            cases = []
            missing = copy.deepcopy(journal)
            del missing['effects'][f'delete:{RETIREMENT_IDS[-1]}']
            cases.append(missing)
            forged = copy.deepcopy(journal)
            forged['effects'][f'delete:{RETIREMENT_IDS[0]}']['command_digest'] = 'f' * 64
            cases.append(forged)
            extra = copy.deepcopy(journal)
            extra['effects']['apply:' + CANONICAL_IDS[0] + ':graph'] = planned_effect(
                parsed, live, f'apply:{CANONICAL_IDS[0]}:graph', 'verified', {CANONICAL_IDS[0]: 1},
            )
            cases.append(extra)
            for candidate in cases:
                self._write_journal(journal_path, candidate)
                with self.assertRaises(operations.JournalError):
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )

    def test_canonical_stage_provenance_and_complete_effect_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, live)
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'complete'
            journal['members'] = [
                {'workflow_id': row['workflow_id'], 'status': 'verified'} for row in journal['members']
            ]
            journal['effects'] = planned_forward_effects(parsed, live)
            metadata_key = f'apply:{CANONICAL_IDS[0]}:metadata'
            journal['effects'][metadata_key]['stdin_sha256'] = 'f' * 64
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )
            journal['effects'] = planned_forward_effects(parsed, live)
            del journal['effects'][metadata_key]
            self._write_journal(journal_path, journal)
            with self.assertRaises(operations.JournalError):
                reconcile_release_live_portfolio(
                    fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )

    def test_deleted_d9_row_blocks_apply_before_any_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, live = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, live)
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'd9_complete'
            journal['members'] = [
                {'workflow_id': row['workflow_id'],
                 'status': 'verified' if row['workflow_id'] in RETIREMENT_IDS else 'pending'}
                for row in journal['members']
            ]
            journal['effects'] = {
                f'delete:{wid}': planned_effect(parsed, live, f'delete:{wid}', 'verified')
                for wid in RETIREMENT_IDS
            }
            self._write_journal(journal_path, journal)
            d9 = operations._portfolio_expected_post(parsed, {
                'projects': live['projects'], 'database_schema': live['database_schema'],
                'sqlite': live['sqlite'], 'retirement': live['retirement'], 'canonical': live['canonical'],
            })
            d9['canonical'] = live['canonical']
            d9['retirement'][RETIREMENT_IDS[0]]['workflow']['present'] = True
            settle = mock.Mock(return_value='postimage')
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=d9),
                mock.patch.object(operations, '_settle_or_run', settle),
            ):
                with self.assertRaises(EffectBlocked):
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='apply', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )
            settle.assert_not_called()

    def test_physical_project_root_alias_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            alias = fixture['root'] / 'project-alias'
            alias.symlink_to(fixture['root'] / 'project-1', target_is_directory=True)
            value = copy.deepcopy(fixture['value'])
            value['canonical_workflows'][1]['project_root'] = str(alias)
            path = fixture['root'] / 'alias-plan.json'
            path.write_bytes(canonical_bytes(value))
            plan = load_plan(path, schema='release-live-portfolio-plan-v1', expected_sha256=digest(path))
            with self.assertRaises(PlanValidationError):
                operations._validate_release_live_portfolio_plan(plan)

    def test_canonical_rollback_is_a_forward_restore_after_partial_progression(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, preimage = self._read_fixture(fixture)
            with (
                mock.patch.object(operations, '_portfolio_read_state', return_value=preimage),
                mock.patch.object(operations, '_portfolio_project_rows', return_value=preimage['projects']),
            ):
                reconcile_release_live_portfolio(fixture['plan'], mode='prepare', kent=fixture['kent'])
            journal_path = fixture['root'] / 'state' / 'release-live-portfolio.journal.json'
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'canonical_in_progress'
            journal['members'] = [
                {'workflow_id': row['workflow_id'],
                 'status': 'verified' if row['workflow_id'] in RETIREMENT_IDS + [CANONICAL_IDS[0]] else 'pending'}
                for row in journal['members']
            ]
            journal['effects'] = planned_forward_effects(
                parsed, preimage, {CANONICAL_IDS[0]: 2},
            )
            journal_path.write_bytes(canonical_bytes(journal) + b'\n')
            d9_preimage = {
                'projects': preimage['projects'], 'database_schema': preimage['database_schema'],
                'retirement': preimage['retirement'], 'canonical': preimage['canonical'],
                'sqlite': preimage['sqlite'],
            }
            current = operations._portfolio_expected_post(parsed, d9_preimage)
            current['canonical'] = copy.deepcopy(preimage['canonical'])
            item = parsed['canonical_workflows'][0]
            stages = operations._canonical_progress(
                {'kent': parsed['kent'], 'project_root': item['project_root']},
                item, preimage['canonical'][item['workflow_id']],
            )
            current['canonical'][item['workflow_id']] = stages[-1]['after']

            def read(*_args):
                return current

            def settle(journal_state, **kwargs):
                _, workflow_id, name = kwargs['effect_key'].split(':')
                item = next(row for row in parsed['canonical_workflows'] if row['workflow_id'] == workflow_id)
                before = preimage['canonical'][workflow_id]
                start = operations._portfolio_forward_state(parsed, item, before, journal_state)
                stages = operations._portfolio_restore_stages(parsed, item, before, start)
                stage = next(row for row in stages if row['name'] == name)
                current['canonical'][workflow_id] = stage['after']
                effects = dict(journal_state.state.get('effects') or {})
                effects[kwargs['effect_key']] = planned_effect(
                    parsed, preimage, kwargs['effect_key'], 'verified', {CANONICAL_IDS[0]: 2},
                )
                journal_state.persist({**journal_state.state, 'effects': effects})
                return 'postimage'

            with (
                mock.patch.object(operations, '_portfolio_read_state', side_effect=read),
                mock.patch.object(operations, '_settle_or_run', side_effect=settle),
            ):
                report = reconcile_release_live_portfolio(
                    fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                )
            self.assertEqual(report['phase'], 'rolled_back')
            with mock.patch.object(operations, '_portfolio_read_state', side_effect=read):
                self.assertEqual(
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )['phase'], 'rolled_back',
                )

    def test_rollback_rejects_unjournaled_postimage_and_revalidates_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            parsed, preimage = self._read_fixture(fixture)
            journal_path = self._prepare_mocked(fixture, preimage)
            journal = json.loads(journal_path.read_text())
            journal['phase'] = 'canonical_in_progress'
            journal['members'] = [
                {'workflow_id': row['workflow_id'], 'status': 'verified'} for row in journal['members']
            ]
            journal['effects'] = {
                **{
                    f'delete:{wid}': planned_effect(parsed, preimage, f'delete:{wid}', 'verified')
                    for wid in RETIREMENT_IDS
                },
                **{
                    f'apply:{wid}:{stage["name"]}': planned_effect(
                        parsed, preimage, f'apply:{wid}:{stage["name"]}', 'verified',
                    )
                    for item in parsed['canonical_workflows']
                    for wid in [item['workflow_id']]
                    for stage in operations._canonical_progress(
                        {'kent': parsed['kent'], 'project_root': item['project_root']},
                        item, preimage['canonical'][wid],
                    )
                },
            }
            self._write_journal(journal_path, journal)
            d9_preimage = {
                'projects': preimage['projects'], 'database_schema': preimage['database_schema'],
                'retirement': preimage['retirement'], 'canonical': preimage['canonical'],
                'sqlite': preimage['sqlite'],
            }
            current = operations._portfolio_expected_post(parsed, d9_preimage)
            current['canonical'] = copy.deepcopy(preimage['canonical'])
            for item in parsed['canonical_workflows']:
                before = preimage['canonical'][item['workflow_id']]
                stages = operations._canonical_progress(
                    {'kent': parsed['kent'], 'project_root': item['project_root']}, item, before,
                )
                start = stages[-1]['after'] if stages else before
                restore = operations._portfolio_restore_stages(parsed, item, before, start)
                current['canonical'][item['workflow_id']] = restore[0]['after'] if restore else start
            with mock.patch.object(operations, '_portfolio_read_state', return_value=current):
                with self.assertRaises(EffectBlocked):
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )

            journal['phase'] = 'rolled_back'
            journal['members'] = [
                {'workflow_id': row['workflow_id'], 'status': 'pending'} for row in journal['members']
            ]
            journal['effects'] = {}
            self._write_journal(journal_path, journal)
            drifted = copy.deepcopy(preimage)
            drifted['projects'][0]['path'] += '/foreign'
            with mock.patch.object(operations, '_portfolio_read_state', return_value=drifted):
                with self.assertRaises(EffectBlocked):
                    reconcile_release_live_portfolio(
                        fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256,
                    )

    def test_prepared_rollback_has_no_live_effect_and_complete_needs_new_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_fixture(Path(temporary))
            reconcile_release_live_portfolio(fixture['plan'], mode='prepare', kent=fixture['kent'])
            report = reconcile_release_live_portfolio(
                fixture['plan'], mode='rollback', kent=fixture['kent'], confirm=fixture['plan'].sha256
            )
            self.assertEqual(report['phase'], 'rolled_back')
            self.assertEqual(
                [row for row in read_log(fixture) if '--confirm' in row], []
            )



if __name__ == "__main__":
    unittest.main()
