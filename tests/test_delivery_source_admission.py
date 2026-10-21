"""Disposable, offline publication/source admission fixtures."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import marshal
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import py_compile
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/inspect-delivery-source"
REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_TEMPLATE = REPO_ROOT / "templates/project/workflow-project-memory"
MEMORY_CORE = REPO_ROOT / "workflowkit/project_memory.py"
MEMORY_CLI = REPO_ROOT / "scripts/project-memory"
FIXTURE_SESSION = "6c724730-01ea-49e7-a2b1-a64d7fbe3ba1"
FIXTURE_WORKFLOW = "a2c38b63-6e80-4b4a-9762-64f25a45cfa7"
FIXTURE_PLAN_REFERENCE = "question_answer(fixture-session/approved-plan)"
FIXTURE_SCRIPT_CONTEXTS = {
    "plan_contract_accept": ".kent/scripts/workflow-plan-contract",
    "branch_identity": ".kent/scripts/workflow-branch-identity",
}


def install_r2_lifecycle_sources(owner, *, coder_selection: str) -> None:
    """Repin one disposable published Kit/project closure before Task effects."""
    owner.native_adapter_fixture()
    project_scripts = {
        "workflow-checkpoint": REPO_ROOT / "templates/project/workflow-checkpoint",
        "workflow-plan-contract": REPO_ROOT / "templates/project/workflow-plan-contract",
        "workflow-branch-identity": REPO_ROOT / "templates/project/workflow-branch-identity",
        "workflow-evidence-ledger": REPO_ROOT / "templates/project/workflow-evidence-ledger",
        "workflow-task-janitor": REPO_ROOT / "templates/project/workflow-task-janitor",
        "workflow_runtime_contracts.py": REPO_ROOT / ".kent/scripts/workflow_runtime_contracts.py",
        "workflow-prepare-cleanup": REPO_ROOT / ".kent/scripts/workflow-prepare-cleanup",
    }
    for name, source in project_scripts.items():
        target = owner.project / ".kent/scripts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if name != "workflow_runtime_contracts.py" else 0o644)
    kit_scripts = {
        "workflow-task-janitor": REPO_ROOT / "templates/project/workflow-task-janitor",
        "workflow-evidence-ledger": REPO_ROOT / "templates/project/workflow-evidence-ledger",
        "workflow_runtime_contracts.py": REPO_ROOT / ".kent/scripts/workflow_runtime_contracts.py",
    }
    for name, source in kit_scripts.items():
        target = owner.kit / ".kent/scripts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if name != "workflow_runtime_contracts.py" else 0o644)
    for source in (REPO_ROOT / "workflowkit").glob("*.py"):
        target = owner.kit / "workflowkit" / source.name
        shutil.copyfile(source, target)
        target.chmod(0o644)
    shutil.copyfile(REPO_ROOT / "scripts/prepare-github-ci",
                    owner.kit / "scripts/prepare-github-ci")
    (owner.kit / "scripts/prepare-github-ci").chmod(0o755)
    (owner.project / ".kent/context/delivery.md").parent.mkdir(parents=True, exist_ok=True)
    (owner.project / ".kent/context/delivery.md").write_text(
        "# Disposable delivery context\n"
    )
    (owner.project / ".kent/commands/cleanup-task.md").parent.mkdir(
        parents=True, exist_ok=True,
    )
    (owner.project / ".kent/commands/cleanup-task.md").write_text(
        "# Disposable cleanup procedure\n"
    )
    (owner.project / ".kent/workflow-profile.toml").write_text(
        '[policies]\n'
        'delivery_reliability = "v1"\n'
        'branch_identity = "task"\n'
        f'coder_selection = "{coder_selection}"\n'
        'writer_sessions = "continuous"\n'
        '[project_memory]\n'
        f'root = "{owner.memory_root}"\n'
        'project_key = "fixture"\n'
        'project_uid = "project-uid"\n'
        'mode = "r2"\n'
        '[work_kinds.test]\n'
        'description = "Disposable integration qualification."\n'
        '[commands]\n'
        'checkpoint = ".kent/scripts/workflow-checkpoint"\n'
        'plan_contract = ".kent/scripts/workflow-plan-contract"\n'
        'plan_contract_accept = ".kent/scripts/workflow-plan-contract"\n'
        'branch_identity = ".kent/scripts/workflow-branch-identity"\n'
        'evidence = ".kent/scripts/workflow-evidence-ledger"\n'
        'prepare_cleanup = ".kent/scripts/workflow-prepare-cleanup"\n'
        'janitor = ".kent/scripts/workflow-task-janitor"\n'
        'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n'
    )
    (owner.kit / ".kent/workflow-profile.toml").write_text(
        '[commands]\n'
        'janitor = ".kent/scripts/workflow-task-janitor"\n'
        'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n'
    )
    (owner.project / ".kent/workflow-profile.toml").chmod(0o644)
    (owner.kit / ".kent/workflow-profile.toml").chmod(0o644)
    owner.git(owner.project, "branch", "-m", "OSM-130")
    owner.native_task = owner.make_native_task(workflow_id=FIXTURE_WORKFLOW)
    owner.native_task["current_nodes"][0]["session_id"] = FIXTURE_SESSION
    owner.write_fake_kent(owner.native_task)
    owner.repin_native_sources()


def r2_script_environment(owner, **extra):
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("KENT_")
    }
    environment.update(
        HOME=str(owner.home),
        PATH=str(owner.bin) + os.pathsep + os.environ.get("PATH", ""),
        KENT_BIN=str(owner.native_bin),
        PYTHONDONTWRITEBYTECODE="1",
    )
    environment.update(extra)
    return environment


def accept_r2_plan(owner, *, report_only: bool = False):
    initialized = owner.adapter_call(
        "workflow", "initialize-task", session_id=FIXTURE_SESSION,
    )
    if initialized.returncode != 0:
        raise AssertionError(initialized.stderr)
    if report_only:
        profile = (owner.project / ".kent/workflow-profile.toml").read_text()
        if 'coder_selection = "disabled"' not in profile:
            raise AssertionError("report-only fixture must disable coder selection before admission")
        configured_path = "not-applicable"
        content = None
        normalized = "not-applicable\n"
    else:
        choice = {
            "schema": "coder-selection-v1",
            "task_short_id": owner.native_task["summary"]["short_id"],
            "complexity": "complex",
            "rationale": "Exercise the accepted R2 branch-selection boundary.",
        }
        content = (
            "# Accepted fixture plan\n"
            "- [ ] Implement the bounded fixture scenario\n\n"
            "```json\n"
            + json.dumps(choice, sort_keys=True)
            + "\n```\n"
            + FIXTURE_PLAN_REFERENCE
            + "\n"
        )
        published = owner.memory_cli({
            "schema": "r2-request-v1",
            "operation": "publish",
            "document_key": "tasks/OSM-130/plan",
            "expected_revision": None,
            "content": content,
            "operation_id": "fixture-plan-publication",
            "provenance": [FIXTURE_PLAN_REFERENCE],
        })
        if published.returncode != 0:
            raise AssertionError(published.stderr)
        revision = json.loads(published.stdout)["result"]
        configured_path = revision["path"]
        normalized = "\n".join(
            line.rstrip() for line in content.replace("\r\n", "\n").split("\n")
        )
        normalized = normalized.rstrip("\n") + "\n"
    owner.native_task["current_scripts"] = [{
        "current_node": {
            "node_id": "plan_contract_accept",
            "transition_branch_key": None,
        },
        "path": FIXTURE_SCRIPT_CONTEXTS["plan_contract_accept"],
    }]
    owner.write_fake_kent(owner.native_task)
    payload = {
        "_kent": {
            "task_id": "task-native",
            "node_id": "plan_contract_accept",
        },
        "workspace_path": str(owner.project),
        "task_short_id": "OSM-130",
        "plan_path": configured_path,
        "work_kind": "test",
        "review_context": "Reviewed fixture; " + FIXTURE_PLAN_REFERENCE,
        "plan_route": "start",
        "plan_route_context": "not-applicable",
        "accepted_plan_sha256": "not-applicable",
        "reviewed_normalized_sha256": hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest(),
    }
    accepted = subprocess.run(
        [sys.executable, str(owner.project / ".kent/scripts/workflow-plan-contract")],
        cwd=owner.project,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
        env=r2_script_environment(owner, KENT_PLAN_CONTRACT_MODE="accept"),
    )
    if accepted.returncode != 0:
        raise AssertionError(accepted.stderr)
    handoff = json.loads(accepted.stdout)
    if handoff.get("transition") != "plan_contract_start":
        raise AssertionError(f"plan acceptance did not complete: {handoff!r}")
    cache_path = owner.project / ".kent/runtime/OSM-130/plan-contract.json"
    cache_raw = cache_path.read_bytes()
    locator = json.loads(cache_raw)
    accepted_path = locator["accepted_snapshot_path"]
    accepted_raw = Path(accepted_path).read_bytes()
    accepted_snapshot = json.loads(accepted_raw)
    return {
        "cache_path": cache_path,
        "cache_raw": cache_raw,
        "locator": locator,
        "accepted_path": Path(accepted_path),
        "accepted_raw": accepted_raw,
        "accepted_snapshot": accepted_snapshot,
        "handoff": handoff,
        "plan_content": content,
    }


def branch_r2_plan(owner, accepted):
    owner.native_task["current_scripts"] = [{
        "current_node": {
            "node_id": "branch_identity",
            "transition_branch_key": None,
        },
        "path": FIXTURE_SCRIPT_CONTEXTS["branch_identity"],
    }]
    owner.write_fake_kent(owner.native_task)
    handoff = accepted["handoff"]
    payload = {
        "_kent": {
            "task_id": "task-native",
            "node_id": "branch_identity",
        },
        "workspace_path": str(owner.project),
        "plan_path": handoff["plan_path"],
        "work_kind": "test",
        "task_short_id": "OSM-130",
        "accepted_plan_sha256": accepted["accepted_snapshot"]["normalized_sha256"],
        "plan_route": "start",
        "plan_route_context": "not-applicable",
        "review_context": handoff["review_context"],
    }
    result = subprocess.run(
        [sys.executable, str(owner.project / ".kent/scripts/workflow-branch-identity")],
        cwd=owner.project,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
        env=r2_script_environment(owner),
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def run_r2_cleanup(
    owner,
    accepted,
    *,
    branch_result=None,
    remove_cache=False,
    symlink_cache=False,
):
    task = owner.native_task["summary"]["short_id"]
    root = owner.project
    if symlink_cache:
        accepted["cache_path"].unlink()
        accepted["cache_path"].symlink_to(accepted["accepted_path"])
    elif remove_cache:
        accepted["cache_path"].unlink()
    report_path = root / "build/kent-workflow" / task / "redaction-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_raw = b"offline fixture redaction report\n"
    report_path.write_bytes(report_raw)
    report_path.chmod(0o600)
    artifacts = [str(report_path.relative_to(root))]
    for result in [accepted["handoff"], *([branch_result] if branch_result else [])]:
        artifacts.extend(
            line for line in result.get("commentary", "").splitlines()
            if line.startswith("KENT_PACKET_SOURCE_V1 ")
        )
    event = {
        "node_key": "plan_contract",
        "evidence_type": "plan_contract_acceptance",
        "summary": "Retain the accepted R2 fixture plan and source references",
        "artifacts": artifacts,
        "checks": ["The disposable accepted plan was verified."],
        "decisions": ["Preserve the explicit fixture scope reference."],
        "context": {
            "manifest_path": ".kent/context/delivery.md",
            "files_read": [".kent/commands/cleanup-task.md"],
        },
    }
    ledger_environment = r2_script_environment(
        owner,
        KENT_TASK_ID="task-native",
        KENT_SESSION_ID=FIXTURE_SESSION,
        KENT_RUN_ID="d64897e3-46ad-4f50-8cbf-408bf7806b55",
        KENT_STEP_ID="82fb5946-8b8d-4f76-9688-bab0939058b9",
    )
    cleanup_environment = r2_script_environment(
        owner,
        KENT_TASK_ID="task-native",
        KENT_SESSION_ID=FIXTURE_SESSION,
        KENT_RUN_ID="cc4cf28b-e1b4-4811-b68b-d2d395a17855",
        KENT_STEP_ID="7c8fe9aa-b2b3-4215-9af7-bfa278ec4fba",
    )
    appended = subprocess.run(
        [sys.executable, str(root / ".kent/scripts/workflow-evidence-ledger"),
         "append", "--task", task, "--workspace", str(root)],
        cwd=root,
        input=json.dumps(event),
        text=True,
        capture_output=True,
        timeout=60,
        env=ledger_environment,
    )
    if appended.returncode != 0:
        raise AssertionError(appended.stderr)

    snapshot_text = accepted["cache_raw"].decode("utf-8")
    data = {
        "snapshot_utf8": snapshot_text,
        "snapshot_sha256": hashlib.sha256(accepted["cache_raw"]).hexdigest(),
        "scope_sha256": accepted["accepted_snapshot"]["normalized_sha256"],
        "review_refs": [
            "session_message(fixture-session/plan-review-one)",
            "session_message(fixture-session/plan-review-two)",
        ],
        "approval_ref": FIXTURE_PLAN_REFERENCE,
    }
    canonical_data = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    request = {
        "workspace_path": str(root),
        "task_short_id": task,
        "snapshot_sha256": data["snapshot_sha256"],
        "scope_sha256": data["scope_sha256"],
        "retention_receipt": {
            "task_short_id": task,
            "record_ref": "task_comment(fixture-session/retention-record)",
            "readback_sha256": hashlib.sha256(canonical_data).hexdigest(),
            "data": data,
        },
        "final_event": {
            **event,
            "node_key": "cleanup",
            "evidence_type": "cleanup_preparation",
            "summary": "Final cleanup preparation for the accepted R2 fixture plan",
            "artifacts": [],
        },
        "seal_request": {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {
                "status": "passed",
                "report_sha256": hashlib.sha256(report_raw).hexdigest(),
            },
            "retention_class": "cleanup_report_only",
        },
        "cleanup_report_prefix": "Accepted R2 fixture plan retained; cleanup completed.",
    }
    result = subprocess.run(
        [sys.executable, str(root / ".kent/scripts/workflow-prepare-cleanup")],
        cwd=root,
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=90,
        env=cleanup_environment,
    )
    return result, request


class DeliverySourceAdmissionTests(unittest.TestCase):
    def native_worktree_status(self, *, name=None, root=None, effective=None,
                               recorded_root=None, observed_root=None,
                               problems=None, omit_observed_root=False):
        root = root or str(self.project)
        effective = effective or root
        primary_status = {"recorded_root": str(self.project)}
        if not omit_observed_root:
            primary_status["observed_root"] = str(self.project)
        status = {
            "target": {
                "workspace_id": "workspace-test",
                "workspace_name": "Fixture",
                "workspace_root": str(self.project),
                "workspace_availability": "available",
                "worktree": None,
                "cwd_relpath": ".",
                "effective_workdir": effective,
            },
            "worktree": primary_status,
            "problems": [] if problems is None else problems,
        }
        if name is not None:
            status["target"]["worktree"] = {
                "id": "worktree-test", "name": name,
                "root": root, "availability": "available",
            }
            status["worktree"] = {
                "recorded_root": recorded_root or root,
                "display_name": name,
                "recorded_branch_ref": "refs/heads/not-an-authority",
            }
            if not omit_observed_root:
                status["worktree"]["observed_root"] = observed_root or root
        return status

    def native_session_lookup(self, records, *, include_next_offset=False,
                              bad_scope_at=None, bad_next_offset_at=None,
                              status=None, task_errors=()):
        import runpy
        adapter = runpy.run_path(str(ADAPTER_TEMPLATE))
        lookup = adapter["native_task_for_session"]
        project_id = "project-test"
        inventory = [{
            "task_id": item["summary"]["id"],
            "short_id": item["summary"].get("short_id"),
        } for item in records]
        by_id = {item["summary"]["id"]: item for item in records}
        list_pages = []
        task_reads = []
        status_reads = []
        self.native_task_reads = task_reads
        self.native_status_reads = status_reads
        task_errors = set(task_errors)
        status = status if status is not None else self.native_worktree_status()

        def native_json(arguments, _workspace):
            if arguments[:2] == ["worktree", "status"]:
                status_reads.append(arguments)
                if arguments != [
                        "worktree", "status", "--session", "session-native", "--json"]:
                    raise AssertionError(f"unexpected native worktree status query: {arguments!r}")
                return status
            if arguments[:2] == ["task", "list"]:
                limit = int(arguments[arguments.index("--limit") + 1])
                offset = int(arguments[arguments.index("--offset") + 1])
                if not 1 <= limit <= 100:
                    raise AssertionError(f"Kent list page limit out of range: {limit}")
                list_pages.append((offset, limit))
                page = inventory[offset:offset + limit]
                response = {
                    "scope": {"project_id": "foreign" if offset == bad_scope_at else project_id},
                    "tasks": page,
                }
                if include_next_offset and offset + len(page) < len(inventory):
                    response["next_offset"] = offset + len(page)
                if offset == bad_next_offset_at:
                    response["next_offset"] = offset + len(page) + 1
                return response
            if arguments[:2] == ["task", "show"]:
                task_id = arguments[2]
                task_reads.append(task_id)
                if task_id in task_errors:
                    raise ValueError("selected native task read failed")
                return by_id[task_id]
            raise AssertionError(f"unexpected native read: {arguments!r}")

        with mock.patch.dict(lookup.__globals__, {"native_json": native_json}):
            result = lookup(self.project, project_id, "session-native")
        return result, list_pages, task_reads

    def test_task_locator_is_only_a_hint_verified_against_current_session(self):
        import runpy
        adapter = runpy.run_path(str(ADAPTER_TEMPLATE))
        resolve = adapter["native_task_context"]
        task = {"summary": {"id": "task-native", "project_id": "project-test"},
                "current_nodes": [{"session_id": "session-native"}]}
        read = mock.Mock(return_value=task)
        scan = mock.Mock(side_effect=AssertionError("locator must not scan inventory"))
        with mock.patch.dict(os.environ, {"KENT_SESSION_ID": "session-native"}, clear=True), \
                mock.patch.dict(resolve.__globals__, {
                    "native_json": read, "native_task_for_session": scan}):
            result = resolve({"task_locator": "task-native"}, Path("/workspace"), "project-test")
            self.assertEqual(result[0], task)
            self.assertEqual(read.call_count, 1)
            with self.assertRaisesRegex(ValueError, "identity differs"):
                resolve({"task_locator": "foreign"}, Path("/workspace"), "project-test")
            task["current_nodes"] = []
            with self.assertRaisesRegex(ValueError, "ownership differs"):
                resolve({"task_locator": "task-native"}, Path("/workspace"), "project-test")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "inherited ownership"):
                resolve({"task_locator": "task-native"}, Path("/workspace"), "project-test")

    def test_native_session_inventory_uses_bounded_pages_and_probes_exhaustion(self):
        records = []
        for index in range(101):
            session = "session-native" if index == 100 else f"other-{index}"
            records.append({
                "summary": {"id": f"task-{index:03}", "project_id": "project-test"},
                "current_nodes": [{"session_id": session}],
            })
        result, pages, task_reads = self.native_session_lookup(records)
        self.assertEqual(result["summary"]["id"], "task-100")
        self.assertEqual(pages, [(0, 100), (100, 100), (101, 100)])
        self.assertEqual(len(task_reads), 101)

    def test_native_session_inventory_follows_next_offset_and_short_missing_page(self):
        records = []
        for index in range(101):
            session = "session-native" if index == 35 else f"other-{index}"
            records.append({
                "summary": {"id": f"task-{index:03}", "project_id": "project-test"},
                "current_nodes": [{"session_id": session}],
            })
        result, pages, _ = self.native_session_lookup(
            records, include_next_offset=True)
        self.assertEqual(result["summary"]["id"], "task-035")
        self.assertEqual(pages, [(0, 100), (100, 100), (101, 100)])

    def test_native_session_inventory_accepts_exact_bound_after_empty_probe(self):
        records = [{
            "summary": {"id": f"task-{index:03}", "project_id": "project-test"},
            "current_nodes": [{"session_id": f"other-{index}"}],
        } for index in range(256)]
        result, pages, task_reads = self.native_session_lookup(records)
        self.assertIsNone(result)
        self.assertEqual(pages, [(0, 100), (100, 100), (200, 100), (256, 100)])
        self.assertEqual(len(task_reads), 256)

    def test_native_session_inventory_rejects_overflow_duplicates_scope_and_bad_offsets(self):
        def records(count):
            return [{
                "summary": {"id": f"task-{index:03}", "project_id": "project-test"},
                "current_nodes": [{"session_id": f"other-{index}"}],
            } for index in range(count)]

        overflow = records(257)
        with self.assertRaisesRegex(ValueError, "inventory incomplete"):
            self.native_session_lookup(overflow)

        duplicate = records(101)
        duplicate[100]["summary"]["id"] = duplicate[0]["summary"]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate native task"):
            self.native_session_lookup(duplicate)

        with self.assertRaisesRegex(ValueError, "inventory incomplete"):
            self.native_session_lookup(records(101), include_next_offset=True,
                                        bad_scope_at=100)

        with self.assertRaisesRegex(ValueError, "page offset differs"):
            self.native_session_lookup(records(101), include_next_offset=True,
                                        bad_next_offset_at=0)

    def test_native_worktree_hint_reads_only_unique_current_task_detail(self):
        records = [
            {"summary": {"id": "task-current", "project_id": "project-test",
                         "short_id": "OSM-130"},
             "worktree_path": str(self.project),
             "current_nodes": [{"session_id": "session-native"}]},
            {"summary": {"id": "task-unrelated", "project_id": "project-test",
                         "short_id": "OSM-102"},
             "worktree_path": None,
             "current_nodes": []},
        ]
        result, pages, task_reads = self.native_session_lookup(
            records, status=self.native_worktree_status(name="OSM-130"))
        self.assertEqual(result["summary"]["id"], "task-current")
        self.assertEqual(pages, [(0, 100), (2, 100)])
        self.assertEqual(task_reads, ["task-current"])
        self.assertEqual(self.native_status_reads, [
            ["worktree", "status", "--session", "session-native", "--json"]])

    def test_native_worktree_hint_rejects_wrong_root_duplicate_short_id_and_stale_session(self):
        record = {
            "summary": {"id": "task-current", "project_id": "project-test",
                        "short_id": "OSM-130"},
            "worktree_path": str(self.kit),
            "current_nodes": [{"session_id": "session-native"}],
        }
        with self.assertRaisesRegex(ValueError, "worktree differs"):
            self.native_session_lookup(
                [record], status=self.native_worktree_status(name="OSM-130"))

        duplicate = [
            {**record, "summary": {**record["summary"], "id": "task-one"}},
            {**record, "summary": {**record["summary"], "id": "task-two"}},
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous native worktree Task hint"):
            self.native_session_lookup(
                duplicate, status=self.native_worktree_status(name="OSM-130"))

        stale = {
            **record,
            "worktree_path": str(self.project),
            "current_nodes": [{"session_id": "session-stale"}],
        }
        with self.assertRaisesRegex(ValueError, "Session ownership differs"):
            self.native_session_lookup(
                [stale], status=self.native_worktree_status(name="OSM-130"))

    def test_native_worktree_hint_propagates_selected_read_errors_and_status_conflicts(self):
        record = {
            "summary": {"id": "task-current", "project_id": "project-test",
                        "short_id": "OSM-130"},
            "worktree_path": str(self.project),
            "current_nodes": [{"session_id": "session-native"}],
        }
        with self.assertRaisesRegex(ValueError, "selected native task read failed"):
            self.native_session_lookup(
                [record], status=self.native_worktree_status(name="OSM-130"),
                task_errors={"task-current"})
        self.assertEqual(self.native_task_reads, ["task-current"])

        conflicting = self.native_worktree_status(
            name="OSM-130", effective=str(self.kit))
        with self.assertRaisesRegex(ValueError, "native worktree status conflicts"):
            self.native_session_lookup([record], status=conflicting)

        malformed = {"target": {"worktree": None}, "worktree": None}
        with self.assertRaisesRegex(ValueError, "native worktree status incomplete"):
            self.native_session_lookup([record], status=malformed)

        problem_status = self.native_worktree_status(
            problems=[{"kind": "git_binding_missing", "root": str(self.project)}])
        with self.assertRaisesRegex(ValueError, "native worktree status incomplete"):
            self.native_session_lookup([record], status=problem_status)

    def test_native_primary_and_nonmatching_worktree_hints_keep_bounded_scan(self):
        records = [
            {"summary": {"id": "task-current", "project_id": "project-test",
                         "short_id": "OSM-130"},
             "current_nodes": [{"session_id": "session-native"}]},
            {"summary": {"id": "task-other", "project_id": "project-test",
                         "short_id": "OSM-102"},
             "current_nodes": []},
        ]
        primary, _, primary_reads = self.native_session_lookup(
            records, status=self.native_worktree_status())
        self.assertEqual(primary["summary"]["id"], "task-current")
        self.assertEqual(primary_reads, ["task-current", "task-other"])

        primary_without_observed_root, _, primary_without_observed_reads = (
            self.native_session_lookup(
                records, status=self.native_worktree_status(omit_observed_root=True)))
        self.assertEqual(primary_without_observed_root["summary"]["id"], "task-current")
        self.assertEqual(primary_without_observed_reads, ["task-current", "task-other"])

        nonmatching, _, nonmatching_reads = self.native_session_lookup(
            records, status=self.native_worktree_status(name="OSM-999"))
        self.assertEqual(nonmatching["summary"]["id"], "task-current")
        self.assertEqual(nonmatching_reads, ["task-current", "task-other"])

        wrong_root, _, wrong_root_reads = self.native_session_lookup(
            records,
            status=self.native_worktree_status(
                name="OSM-130", root=str(self.kit), effective=str(self.kit)))
        self.assertEqual(wrong_root["summary"]["id"], "task-current")
        self.assertEqual(wrong_root_reads, ["task-current", "task-other"])

    def test_native_session_lookup_does_not_trust_environment_worktree_name(self):
        records = [
            {"summary": {"id": "task-current", "project_id": "project-test",
                         "short_id": "OSM-130"},
             "current_nodes": [{"session_id": "session-native"}]},
            {"summary": {"id": "task-other", "project_id": "project-test",
                         "short_id": "OSM-102"},
             "current_nodes": []},
        ]
        with mock.patch.dict(os.environ, {"KENT_WORKTREE_NAME": "OSM-130"}, clear=True):
            result, _, task_reads = self.native_session_lookup(
                records, status=self.native_worktree_status())
        self.assertEqual(result["summary"]["id"], "task-current")
        self.assertEqual(task_reads, ["task-current", "task-other"])

    def test_native_explicit_task_id_path_does_not_use_worktree_hint(self):
        import runpy
        adapter = runpy.run_path(str(ADAPTER_TEMPLATE))
        resolve = adapter["native_task_context"]
        task = {
            "summary": {"id": "task-explicit", "project_id": "project-test"},
            "current_nodes": [{"session_id": "session-native"}],
        }
        read = mock.Mock(return_value=task)
        scan = mock.Mock(side_effect=AssertionError("explicit Task ID must not scan"))
        with mock.patch.dict(
                os.environ,
                {"KENT_TASK_ID": "task-explicit", "KENT_SESSION_ID": "session-native",
                 "KENT_WORKTREE_NAME": "forged-short-id"},
                clear=True), \
                mock.patch.dict(resolve.__globals__, {
                    "native_json": read, "native_task_for_session": scan}):
            result = resolve({}, self.project, "project-test")
        self.assertEqual(result[0], task)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[0], [
            "task", "show", "task-explicit", "--project", "project-test", "--json"])
        scan.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.kit = self.base / "kit"
        self.primary = self.project
        for root in (self.project, self.kit):
            root.mkdir()
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Fixture")
            self.git(root, "config", "user.email", "fixture@example.invalid")
        self.put(self.project, "AGENTS.md", "Project instructions\n")
        self.put(self.project, ".kent/workflow-profile.toml",
                 '[policies]\ndelivery_reliability = "v1"\n'
                 '[commands]\ncheckpoint = ".kent/scripts/workflow-checkpoint"\n'
                 f'[project_memory]\nroot = "{self.base / "memory"}"\n'
                 'mode = "r2"\nproject_key = "fixture"\nproject_uid = "project-uid"\n')
        self.put(self.project, ".kent/project-contract.md", "Contract\n")
        self.put(self.project, ".kent/scripts/workflow-checkpoint", "#!/bin/sh\nexit 0\n", 0o755)
        self.put(self.project, "product.txt", "original\n")
        self.put(self.project, ".gitignore", "build/\n.kent/runtime/\n.kent/retained-evidence/\n__pycache__/\n")
        self.put(self.kit, ".gitignore", "__pycache__/\n")
        self.put(self.kit, "scripts/inspect-delivery-source", SCRIPT.read_text(), 0o755)
        self.put(self.kit, "scripts/prepare-github-ci", "#!/usr/bin/env python3\nfrom workflowkit import ci_contract\n", 0o755)
        for name in ("__init__", "ci_contract", "delivery", "runtime", "profile",
                     "model", "release", "revision", "kent", "graph", "project_memory"):
            self.put(self.kit, f"workflowkit/{name}.py", f'"""Fixture {name}."""\n')
        self.commits = {}
        for root in (self.project, self.kit):
            self.commit(root)
            head = self.git(root, "rev-parse", "HEAD").strip()
            self.commits[root] = head
            self.git(root, "update-ref", "refs/remotes/origin/main", head)
        self.helper = self.base / "bin/kent-prepare-github-ci"
        self.helper.parent.mkdir()
        self.helper.symlink_to(self.kit / "scripts/prepare-github-ci")
        self.evidence = self.base / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.receipt_path = self.evidence / "publication.json"
        self.receipt = {
            "schema": "delivery-runtime-publication-v1",
            "project_id": "project-test", "workflow_id": "workflow-test",
            "source_commit": self.commits[self.project], "kit_commit": self.commits[self.kit],
            "runtime_roots": [str(self.project), str(self.kit)],
            "files": [], "policy": "delivery_reliability_v1",
            "project_memory": {"root": str(self.base / "memory"), "project_key": "fixture",
                               "project_uid": "project-uid", "legacy_task_ids": ["old-task"]},
        }
        self.refresh_receipt_files()
        self.pin()
        loader = importlib.machinery.SourceFileLoader("source_admission_fixture", str(SCRIPT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.module = importlib.util.module_from_spec(spec)
        loader.exec_module(self.module)

    def refresh_receipt_files(self):
        """Bind each tracked infrastructure input, excluding mutable product data."""
        self.receipt["files"] = []
        for root in (self.project, self.kit):
            for relative in self.git(root, "ls-files").splitlines():
                if relative == "product.txt":
                    continue
                path = root / relative
                raw = path.read_bytes()
                self.receipt["files"].append({
                    "root": str(root), "relative_path": relative,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "byte_size": len(raw), "mode": stat.S_IMODE(path.stat().st_mode)})

    @staticmethod
    def put(root, relative, text, mode=0o644):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    @staticmethod
    def git(root, *arguments):
        return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                              capture_output=True, text=True).stdout

    def commit(self, root):
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "fixture")

    def pin(self, raw=None):
        raw = raw if raw is not None else json.dumps(self.receipt).encode()
        self.receipt_path.write_bytes(raw)
        self.receipt_path.chmod(0o600)
        self.digest = hashlib.sha256(raw).hexdigest()

    def inspect(self, **overrides):
        arguments = dict(receipt_path=self.receipt_path, receipt_sha256=self.digest,
                         project_id="project-test", workflow_id="workflow-test",
                         project_root=self.project, kit_root=self.kit,
                         primary_root=self.primary, workspace_path=self.project,
                         source_ref="refs/remotes/origin/main", kit_ref="refs/remotes/origin/main",
                         ci_helper=self.helper)
        arguments.update(overrides)
        return self.module.inspect_source(**arguments)

    def native_adapter_fixture(self, *, legacy_task_ids=("old-task",)):
        """Install disposable project/Kit copies with pinned native admission."""
        for root in (self.project, self.kit):
            self.put(root, ".kent/scripts/workflow-project-memory",
                     ADAPTER_TEMPLATE.read_text(), 0o755)
            shutil.copyfile(MEMORY_CORE, root / ".kent/scripts/workflow_project_memory.py")
            (root / ".kent/scripts/workflow_project_memory.py").chmod(0o644)
        self.put(self.kit, ".kent/workflow-profile.toml",
                 '[commands]\njanitor = ".kent/scripts/workflow-task-janitor"\n'
                 'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n')
        self.put(self.kit, ".kent/scripts/workflow-task-janitor",
                 "#!/usr/bin/env python3\n# disposable Kit Janitor fixture\n", 0o755)
        self.put(self.kit, ".kent/scripts/workflow_runtime_contracts.py",
                 '"""Disposable Kit Janitor sibling fixture."""\n')
        for root in (self.project, self.kit):
            self.commit(root)
            head = self.git(root, "rev-parse", "HEAD").strip()
            self.commits[root] = head
            self.git(root, "update-ref", "refs/remotes/origin/main", head)
        self.receipt["source_commit"] = self.commits[self.project]
        self.receipt["kit_commit"] = self.commits[self.kit]
        self.receipt["project_memory"]["legacy_task_ids"] = list(legacy_task_ids)
        self.refresh_receipt_files()
        self.pin()

        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.kent_home = self.home / ".kent"
        self.kent_home.mkdir(mode=0o700)
        self.bin = self.kent_home / "bin"
        self.bin.mkdir(mode=0o700)
        self.binding_dir = self.kent_home / "project-memory-bindings"
        self.binding_dir.mkdir(mode=0o700)
        self.adapter_link = self.bin / "kent-project-memory"
        self.adapter_target = self.kit / ".kent/scripts/workflow-project-memory"
        self.adapter_link.symlink_to(self.adapter_target)
        self.ci_helper = self.bin / "kent-prepare-github-ci"
        self.ci_helper.symlink_to(self.kit / "scripts/prepare-github-ci")
        self.binding_path = self.binding_dir / "project-test.json"
        profile_raw = (self.project / ".kent/workflow-profile.toml").read_bytes()
        self.installation_binding = {
            "schema": "r2-installation-binding-v2",
            "project_id": "project-test",
            "primary_root": str(self.project),
            "primary_workspace_id": "workspace-test",
            "profile_sha256": hashlib.sha256(profile_raw).hexdigest(),
            "receipt_path": str(self.receipt_path),
            "receipt_sha256": self.digest,
            "kit_root": str(self.kit),
            "source_ref": "refs/remotes/origin/main",
            "kit_ref": "refs/remotes/origin/main",
            "ci_helper": str(self.ci_helper),
            "helper_bindings": [{"link": str(self.adapter_link),
                                 "target": str(self.adapter_target)}],
        }
        self.write_installation_binding()
        self.memory_root = self.base / "memory"
        self.memory_project = self.memory_root / "fixture"
        initialized = self.memory_cli({"schema": "r2-request-v1",
                                       "operation": "initialize-project"})
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.native_bin = self.bin / "kent"
        self.native_task = self.make_native_task()
        self.write_fake_kent(self.native_task)
        return self

    def repin_native_sources(self):
        """Republish disposable fixture edits and refresh only its private binding."""
        for root in (self.project, self.kit):
            if self.git(root, "status", "--porcelain"):
                self.commit(root)
            head = self.git(root, "rev-parse", "HEAD").strip()
            self.commits[root] = head
            self.git(root, "update-ref", "refs/remotes/origin/main", head)
        self.receipt["source_commit"] = self.commits[self.project]
        self.receipt["kit_commit"] = self.commits[self.kit]
        self.refresh_receipt_files()
        self.pin()
        profile_raw = (self.project / ".kent/workflow-profile.toml").read_bytes()
        self.installation_binding["profile_sha256"] = hashlib.sha256(profile_raw).hexdigest()
        self.installation_binding["receipt_sha256"] = self.digest
        self.write_installation_binding()

    def add_receipt_sources_until(self, target):
        current = len(self.receipt["files"])
        if target < current:
            raise ValueError("target receipt inventory is below current fixture size")
        counts = {
            str(self.project): sum(item["root"] == str(self.project)
                                   for item in self.receipt["files"]),
            str(self.kit): sum(item["root"] == str(self.kit)
                               for item in self.receipt["files"]),
        }
        for index in range(current, target):
            root_name = min(counts, key=counts.get)
            if root_name == str(self.project):
                root = self.project
                prefix = ".kent/scripts/admission-fixtures"
            else:
                root = self.kit
                prefix = "templates/admission-fixtures"
            self.put(root, f"{prefix}/source-{index:04}.txt", "qualified fixture source\n")
            counts[root_name] += 1
        self.repin_native_sources()
        if len(self.receipt["files"]) != target:
            raise AssertionError("fixture did not produce the exact receipt inventory")

    def write_installation_binding(self):
        self.binding_path.write_text(json.dumps(self.installation_binding))
        self.binding_path.chmod(0o600)

    def memory_cli(self, request):
        return subprocess.run(
            [sys.executable, str(MEMORY_CLI), "--root", str(self.memory_root),
             "--project", "fixture", "--project-uid", "project-uid"],
            input=json.dumps(request), capture_output=True, text=True, timeout=30,
            env={**os.environ, "HOME": str(self.home) if hasattr(self, "home") else str(self.base)})

    def initialize_memory_task(self, key, uid, external_refs):
        result = self.memory_cli({
            "schema": "r2-request-v1", "operation": "initialize-task",
            "task_key": key, "task_uid": uid, "external_refs": external_refs})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["result"]

    def make_native_task(self, *, task_id="task-native", short_id="OSM-130",
                         workflow_id="workflow-other"):
        return {
            "summary": {"id": task_id, "project_id": "project-test",
                        "short_id": short_id, "workflow_id": workflow_id},
            "source_workspace": {"root_path": str(self.project), "is_primary": True,
                                 "workspace_id": "workspace-test"},
            "project": {"default_workspace_id": "workspace-test"},
            "worktree_path": None, "live_sessions": [],
            "current_nodes": [{"node_id": "node-native", "session_id": "session-native"}],
        }

    def write_fake_kent(self, task):
        task_json = json.dumps(task, separators=(",", ":"))
        worktree_status_json = json.dumps(self.native_worktree_status())
        task_id = task["summary"]["id"]
        self.native_bin.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"task = json.loads({task_json!r})\n"
            f"worktree_status = json.loads({worktree_status_json!r})\n"
            "args = sys.argv[1:]\n"
            f"project = {str(self.project)!r}\n"
            "if len(args) == 2 and args[0] == 'project' and args[1] in (project,):\n"
            "    print('project-test')\n"
            "elif args == ['project', 'list']:\n"
            "    print('project-test\\tFixture\\t' + project)\n"
            "elif args == ['worktree', 'status', '--session', 'session-native', '--json']:\n"
            "    print(json.dumps(worktree_status))\n"
            "elif (len(args) == 6 and args[:2] == ['task', 'show'] and "
            "args[2] in (task['summary']['id'], task['summary']['short_id']) "
            "and args[3] == '--project' and "
            "args[4] in ('project-test', project) and args[5] == '--json'):\n"
            "    print(json.dumps(task))\n"
            "elif args == ['task', 'sessions', task['summary']['short_id'], "
            "'--project', project, '--json']:\n"
            "    print(json.dumps({'task_id': task['summary']['id'], 'items': []}))\n"
            "elif args == ['workflow', 'graph', 'inspect', "
            "task['summary']['workflow_id'], '--json']:\n"
            "    graph = {'workflow_id': task['summary']['workflow_id'], 'graph': {'nodes': [\n"
            "        {'id': 'plan_contract_accept', 'kind': 'script', "
            "'script_path': '.kent/scripts/workflow-plan-contract'},\n"
            "        {'id': 'branch_identity', 'kind': 'script', "
            "'script_path': '.kent/scripts/workflow-branch-identity'},\n"
            "    ]}}\n"
            "    print(json.dumps(graph))\n"
            "elif len(args) == 9 and args[:2] == ['task', 'list']:\n"
            "    if (args[2:4] != ['--project', 'project-test'] or args[4] != '--limit' "
            "or args[6] != '--offset' or args[8] != '--json'):\n"
            "        sys.exit(2)\n"
            "    limit, offset = int(args[5]), int(args[7])\n"
            "    if not 1 <= limit <= 100 or offset < 0:\n"
            "        sys.exit(2)\n"
            "    inventory = [{'task_id': task['summary']['id']}]\n"
            "    page = inventory[offset:offset + limit]\n"
            "    result = {'scope': {'project_id': 'project-test'}, 'tasks': page}\n"
            "    if offset + len(page) < len(inventory):\n"
            "        result['next_offset'] = offset + len(page)\n"
            "    print(json.dumps(result))\n"
            "else:\n"
            "    sys.exit(2)\n")
        self.native_bin.chmod(0o700)

    def adapter_call(self, interface, operation, *, task_id="task-native",
                     session_id="session-native", standalone_binding=None,
                     native_context=None, extra=None, environment=None):
        request = {"schema": f"r2-{interface}-request-v1", "operation": operation,
                   "workspace_path": str(self.project)}
        if standalone_binding is not None:
            request["standalone_binding"] = standalone_binding
        if native_context is not None:
            request["native_context"] = native_context
        request.update(extra or {})
        env = {"HOME": str(self.home),
               "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}
        if task_id is not None:
            env["KENT_TASK_ID"] = task_id
        if session_id is not None:
            env["KENT_SESSION_ID"] = session_id
        env.update(environment or {})
        return subprocess.run(
            [sys.executable, str(self.project / ".kent/scripts/workflow-project-memory"),
             interface],
            input=json.dumps(request), capture_output=True, text=True, timeout=60, env=env)

    def cli(self, *extra):
        return [sys.executable, str(self.kit / "scripts/inspect-delivery-source"),
                   "--receipt", str(self.receipt_path), "--receipt-sha256", self.digest,
                   "--project-id", "project-test", "--workflow-id", "workflow-test",
                   "--project-root", str(self.project), "--kit-root", str(self.kit),
                   "--primary-root", str(self.primary), "--workspace", str(self.project),
                   "--source-ref", "refs/remotes/origin/main",
                   "--kit-ref", "refs/remotes/origin/main", "--ci-helper", str(self.helper), *extra]

    def test_published_closure_and_cli_are_read_only(self):
        before = {root: self.git(root, "status", "--porcelain") for root in (self.project, self.kit)}
        result = self.inspect()
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["receipt_sha256"], self.digest)
        process = subprocess.run(self.cli(), capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "admitted")
        for root in (self.project, self.kit):
            self.assertEqual(self.git(root, "status", "--porcelain"), before[root])
            self.assertFalse(list(root.rglob("__pycache__")))

    def test_profile_commands_cli_allow_only_exact_empty_or_qualified_paths(self):
        profile = self.project / ".kent/workflow-profile.toml"
        original = profile.read_text()
        cases = (
            ('""', 0, None),
            ('".kent/scripts/workflow-checkpoint"', 0, None),
            ("false", 2, "invalid_profile_command"),
            ("0", 2, "invalid_profile_command"),
            ("[]", 2, "invalid_profile_command"),
            ('" "', 2, "profile_helper_outside_source_closure"),
            ('"../escape"', 2, "unsafe_relative_path"),
            ('"/absolute"', 2, "unsafe_relative_path"),
            ('".kent/scripts/not-published"', 2, "profile_helper_outside_source_closure"),
        )
        for literal, returncode, reason in cases:
            with self.subTest(command=literal):
                profile.write_text(original.replace(
                    'checkpoint = ".kent/scripts/workflow-checkpoint"', f"smoke = {literal}"))
                self.commit(self.project)
                self.receipt["source_commit"] = self.git(
                    self.project, "rev-parse", "HEAD").strip()
                self.git(self.project, "update-ref", "refs/remotes/origin/main",
                         self.receipt["source_commit"])
                entry = next(item for item in self.receipt["files"]
                             if item["root"] == str(self.project)
                             and item["relative_path"] == ".kent/workflow-profile.toml")
                raw = profile.read_bytes()
                entry.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
                self.pin()
                result = subprocess.run(self.cli(), capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
                inspection = json.loads(result.stdout)
                self.assertEqual(inspection["status"], "admitted" if returncode == 0 else "blocked")
                if reason is not None:
                    self.assertEqual(inspection["reason"], reason)

    def add_local_instruction(self):
        """Publish only an ignore rule; the instruction stays outside Git."""
        self.put(self.project, ".gitignore",
                 (self.project / ".gitignore").read_text() + ".kent/skills/private/\n")
        self.commit(self.project)
        self.receipt["source_commit"] = self.git(self.project, "rev-parse", "HEAD").strip()
        self.git(self.project, "update-ref", "refs/remotes/origin/main",
                 self.receipt["source_commit"])
        entry = next(item for item in self.receipt["files"]
                     if item["root"] == str(self.project) and item["relative_path"] == ".gitignore")
        raw = (self.project / ".gitignore").read_bytes()
        entry.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
        name = ".kent/skills/private/instructions.md"
        self.put(self.project, name, "PRIVATE_LOCAL_INSTRUCTION_SENTINEL\n", 0o600)
        raw = (self.project / name).read_bytes()
        self.receipt["schema"] = "delivery-runtime-publication-v2"
        self.receipt["local_inputs"] = [{
            "kind": "local-instruction", "root": str(self.project), "relative_path": name,
            "sha256": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw), "mode": 0o600,
        }]
        self.pin()
        return self.project / name

    def assert_local_cli_blocked(self):
        result = subprocess.run(self.cli(), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "blocked")
        self.assertNotIn("PRIVATE_LOCAL_INSTRUCTION_SENTINEL", result.stdout + result.stderr)

    def test_v2_local_instruction_real_cli_and_task_isolation(self):
        path = self.add_local_instruction()
        result = subprocess.run(self.cli(), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        admitted = json.loads(result.stdout)
        self.assertEqual(admitted["file_count"], len(self.receipt["files"]) + 1)
        self.assertEqual(admitted["source_bytes"],
                         sum(item["byte_size"] for item in self.receipt["files"])
                         + path.stat().st_size)
        self.assertEqual(self.git(self.project, "ls-files", "--", str(path)), "")
        task = self.base / "task"
        self.git(self.project, "worktree", "add", "-qb", "task", str(task))
        self.assertFalse((task / path.relative_to(self.project)).exists())
        command = self.cli()
        command[command.index("--workspace") + 1] = str(task)
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.put(task, str(path.relative_to(self.project)), "checkout must not inherit\n", 0o600)
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.receipt["runtime_roots"].append(str(task))
        self.receipt["local_inputs"][0]["root"] = str(task)
        self.pin()
        self.assert_local_cli_blocked()

    def test_v2_local_instruction_closed_metadata_cli(self):
        self.add_local_instruction()
        original = dict(self.receipt["local_inputs"][0])
        changes = [
            {"kind": "helper"}, {"kind": "local-instruction", "extra": True},
            {"root": str(self.base)}, {"root": str(self.kit)},
            {"relative_path": ".kent/skills/private/../instructions.md"},
            {"relative_path": ".kent/skills/private/*.md"},
            {"relative_path": ".kent/scripts/private.md"},
            {"relative_path": ".kent/skills/private/instructions.py"},
            {"mode": 0o700}, {"mode": 0o620}, {"mode": True},
            {"byte_size": True}, {"byte_size": original["byte_size"] + 1},
            {"sha256": "0" * 64},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.receipt["local_inputs"] = [original | change]
                self.pin()
                self.assert_local_cli_blocked()
        self.receipt["local_inputs"] = [original, original]
        self.pin()
        self.assert_local_cli_blocked()
        self.receipt["schema"] = "delivery-runtime-publication-v1"
        self.pin()
        self.assert_local_cli_blocked()

    def test_v2_local_instruction_missing_changed_extra_and_unsafe_cli(self):
        path = self.add_local_instruction()
        raw = path.read_bytes()
        path.unlink()
        self.assert_local_cli_blocked()
        path.write_bytes(raw + b"changed\n")
        path.chmod(0o600)
        self.assert_local_cli_blocked()
        path.write_bytes(raw)
        path.chmod(0o700)
        self.assert_local_cli_blocked()
        path.chmod(0o600)
        extra = path.with_name("extra.md")
        extra.write_text("unbound private instruction\n")
        self.assert_local_cli_blocked()
        extra.unlink()
        path.unlink()
        path.symlink_to(self.project / "AGENTS.md")
        self.assert_local_cli_blocked()
        path.unlink()
        path.write_bytes(raw)
        path.chmod(0o600)
        os.link(path, extra)
        self.assert_local_cli_blocked()

    def test_v2_local_instruction_indexed_published_and_overlap_cli(self):
        path = self.add_local_instruction()
        self.git(self.project, "add", "-f", str(path))
        self.assert_local_cli_blocked()
        self.commit(self.project)
        self.receipt["source_commit"] = self.git(self.project, "rev-parse", "HEAD").strip()
        self.git(self.project, "update-ref", "refs/remotes/origin/main",
                 self.receipt["source_commit"])
        self.pin()
        self.assert_local_cli_blocked()
        item = dict(self.receipt["local_inputs"][0])
        item.pop("kind")
        self.receipt["files"].append(item)
        self.pin()
        self.assert_local_cli_blocked()

    def test_v2_local_instruction_never_qualifies_helper_cli(self):
        path = self.add_local_instruction()
        link = self.helper.with_name("private-helper")
        link.symlink_to(path)
        result = subprocess.run(self.cli("--installed-helper", str(link), str(path)),
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("helper_target_not_qualified_executable", result.stdout)

    def test_v2_local_instruction_never_qualifies_profile_command_cli(self):
        path = self.add_local_instruction()
        profile = self.project / ".kent/workflow-profile.toml"
        profile.write_text(profile.read_text().replace(
            ".kent/scripts/workflow-checkpoint", path.relative_to(self.project).as_posix()))
        self.commit(self.project)
        self.receipt["source_commit"] = self.git(self.project, "rev-parse", "HEAD").strip()
        self.git(self.project, "update-ref", "refs/remotes/origin/main",
                 self.receipt["source_commit"])
        entry = next(item for item in self.receipt["files"]
                     if item["root"] == str(self.project)
                     and item["relative_path"] == ".kent/workflow-profile.toml")
        raw = profile.read_bytes()
        entry.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
        self.pin()
        result = subprocess.run(self.cli(), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("profile_helper_outside_source_closure", result.stdout)

    def test_v2_empty_inventory_and_required_closed_collection_cli(self):
        self.receipt.update(schema="delivery-runtime-publication-v2", local_inputs=[])
        self.pin()
        result = subprocess.run(self.cli(), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["file_count"], len(self.receipt["files"]))
        for invalid in (None, {}, "private.md"):
            self.receipt["local_inputs"] = invalid
            self.pin()
            self.assert_local_cli_blocked()
        del self.receipt["local_inputs"]
        self.pin()
        self.assert_local_cli_blocked()

    def test_v2_local_instruction_nonowner_and_shared_limits(self):
        path = self.add_local_instruction()
        actual_fstat = self.module.os.fstat
        def foreign_owner(fd):
            info = actual_fstat(fd)
            if (info.st_ino, info.st_dev) == (path.stat().st_ino, path.stat().st_dev):
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info
        with mock.patch.object(self.module.os, "fstat", side_effect=foreign_owner):
            with self.assertRaisesRegex(ValueError, "unsafe source file"):
                self.inspect()
        entry = dict(self.receipt["local_inputs"][0])
        self.receipt["local_inputs"] = [entry] * 512
        self.pin()
        self.assert_local_cli_blocked()
        # A lowered fixture cap isolates shared byte accounting without allocating
        # 32 MiB or changing the production limit.
        self.receipt["local_inputs"] = [entry]
        self.pin()
        published_bytes = sum(item["byte_size"] for item in self.receipt["files"])
        with mock.patch.object(self.module, "MAX_TOTAL", published_bytes):
            with self.assertRaisesRegex(ValueError, "source inspection exceeds limit"):
                self.inspect()
        with mock.patch.object(self.module, "MAX_SOURCE_RECEIPT_FILES", len(self.receipt["files"])):
            with self.assertRaisesRegex(ValueError, "invalid local instruction inventory"):
                self.inspect()

    def test_v2_adapter_rejects_inconsistent_real_cli_count(self):
        self.native_adapter_fixture()
        self.add_local_instruction()
        self.repin_native_sources()
        admitted = self.adapter_call("workflow", "initialize-task")
        self.assertEqual(admitted.returncode, 0, admitted.stdout + admitted.stderr)
        inspector = self.kit / "scripts/inspect-delivery-source"
        source = inspector.read_text()
        changed = source.replace('"file_count": len(files) + len(local_inputs)',
                                 '"file_count": len(files)')
        self.assertNotEqual(source, changed)
        inspector.write_text(changed)
        self.repin_native_sources()
        result = self.adapter_call("workflow", "initialize-task")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid-memory-request", result.stderr)

    def test_v2_local_instruction_real_cli_detects_end_change(self):
        path = self.add_local_instruction()
        # The disposable Git shim mutates only at the final index readback;
        # the pinned inspector itself executes unchanged through its real CLI.
        actual_git = shutil.which("git")
        shim = self.base / "git-shim"
        shim.mkdir()
        fired = self.base / "mutation-fired"
        counter = self.base / "index-read-count"
        self.put(shim, "git",
                 f"#!{sys.executable}\n"
                 "import pathlib, subprocess, sys\n"
                 f"result = subprocess.run([{actual_git!r}, *sys.argv[1:]])\n"
                 f"fired = pathlib.Path({str(fired)!r})\n"
                 f"counter = pathlib.Path({str(counter)!r})\n"
                 f"if {str(self.project)!r} in sys.argv and '--stage' in sys.argv:\n"
                 "    count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
                 "    counter.write_text(str(count))\n"
                 "    if count == 2:\n"
                 "        fired.touch()\n"
                 f"        pathlib.Path({str(path)!r}).write_bytes(b'changed during inspection\\n')\n"
                 "sys.exit(result.returncode)\n", 0o755)
        result = subprocess.run(
            self.cli(), capture_output=True, text=True, timeout=120,
            env={**os.environ, "PATH": str(shim) + os.pathsep + os.environ.get("PATH", "")})
        self.assertTrue(fired.exists(), result.stdout)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("local_instruction_bytes_or_mode_differ", result.stdout)

    def test_v2_local_instruction_end_rechecks(self):
        path = self.add_local_instruction()
        original = self.module.verify_copy
        for mutation in ("bytes", "mode", "index", "extra", "identity"):
            with self.subTest(mutation=mutation):
                raw = path.read_bytes()
                extra = path.with_name("extra.md")
                fired = False
                def mutate(*args, **kwargs):
                    nonlocal fired
                    result = original(*args, **kwargs)
                    if not fired:
                        fired = True
                        if mutation == "bytes":
                            path.write_bytes(raw + b"changed\n")
                        elif mutation == "mode":
                            path.chmod(0o700)
                        elif mutation == "index":
                            self.git(self.project, "add", "-f", str(path))
                        elif mutation == "extra":
                            extra.write_text("unbound\n")
                        else:
                            replacement = path.with_name("replacement.md")
                            replacement.write_bytes(raw)
                            replacement.chmod(0o600)
                            replacement.replace(path)
                    return result
                with mock.patch.object(self.module, "verify_copy", side_effect=mutate):
                    with self.assertRaises(ValueError):
                        self.inspect()
                if mutation == "index":
                    self.git(self.project, "rm", "--cached", "-f", str(path))
                extra.unlink(missing_ok=True)
                path.write_bytes(raw)
                path.chmod(0o600)

    def test_cli_blocker_is_structured_and_does_not_expose_private_input(self):
        self.pin(b'{"PRIVATE_SENTINEL_DO_NOT_PRINT":')
        process = subprocess.run(self.cli(), capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["status"], "blocked")
        self.assertNotIn("PRIVATE_SENTINEL_DO_NOT_PRINT", process.stdout + process.stderr)

    def test_cli_rejects_unqualified_inspector_location(self):
        command = self.cli()
        command[1] = str(SCRIPT)
        process = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["reason"],
                         "inspector_must_execute_from_qualified_external_Kit_root")

    def test_product_descendant_with_identical_infrastructure_passes(self):
        self.put(self.project, "product.txt", "descendant\n")
        self.commit(self.project)
        self.inspect()

    def test_kit_self_development_shared_root_and_product_only_published_descendant(self):
        for relative in ("AGENTS.md", ".kent/workflow-profile.toml",
                         ".kent/project-contract.md", ".kent/scripts/workflow-checkpoint"):
            source = self.project / relative
            self.put(self.kit, relative, source.read_text(), stat.S_IMODE(source.stat().st_mode))
        self.commit(self.kit)
        self.receipt["source_commit"] = self.git(self.kit, "rev-parse", "HEAD").strip()
        self.put(self.kit, "product.txt", "product-only descendant\n")
        self.commit(self.kit)
        self.receipt["kit_commit"] = self.git(self.kit, "rev-parse", "HEAD").strip()
        self.git(self.kit, "update-ref", "refs/remotes/origin/main", self.receipt["kit_commit"])
        self.receipt["runtime_roots"] = [str(self.kit)]
        self.receipt["files"] = []
        for name in self.git(self.kit, "ls-files").splitlines():
            if name == "product.txt":
                continue
            path = self.kit / name
            raw = path.read_bytes()
            self.receipt["files"].append({
                "root": str(self.kit), "relative_path": name,
                "sha256": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw),
                "mode": stat.S_IMODE(path.stat().st_mode)})
        self.pin()
        self.inspect(project_root=self.kit, primary_root=self.kit, workspace_path=self.kit)

    def test_dirty_product_task_worktree_and_mutable_ignored_evidence_pass(self):
        task = self.base / "task"
        self.git(self.project, "worktree", "add", "-qb", "task", str(task))
        self.put(task, "product.txt", "unfinished\n")
        self.put(task, "new-product.txt", "new\n")
        self.put(task, ".kent/runtime/task/ledger", "mutable\n")
        self.put(task, "build/output", "mutable\n")
        self.put(self.project, ".kent/retained-evidence/task/proof", "mutable\n")
        self.inspect(workspace_path=task)

    def test_missing_transitive_module_fails_even_after_receipt_repin(self):
        for relative in ("workflowkit/delivery.py", "workflowkit/model.py", "workflowkit/__init__.py"):
            with self.subTest(relative=relative):
                original = list(self.receipt["files"])
                self.receipt["files"] = [item for item in original if item["relative_path"] != relative]
                self.pin()
                with self.assertRaises(ValueError):
                    self.inspect()
                self.receipt["files"] = original
        self.pin()

    def assert_source_admission_preserves_fixture_state(self):
        def snapshot():
            result = {}
            for root in (self.project, self.kit):
                for path in (root, *root.rglob("*")):
                    info = path.lstat()
                    result[str(path)] = (
                        info.st_dev, info.st_ino, info.st_mode, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns,
                        path.read_bytes() if path.is_file() else None,
                    )
            return result
        before = snapshot()
        result = self.inspect()
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["python_cache_tag"], sys.implementation.cache_tag)
        process = subprocess.run(self.cli(), capture_output=True, text=True, timeout=120)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "admitted")
        self.assertEqual(snapshot(), before)

    def test_source_admission_cli_preserves_normal_import_and_py_compile_caches(self):
        source = self.kit / "workflowkit/delivery.py"
        source.write_text('"""Cache fixture."""\n'
                          'VALUES = {"tuple": (1, 2), "unicode": "résumé"}\n'
                          'FROZEN = {"alpha", "beta", "gamma"}\n'
                          'def outer(value):\n'
                          '    def inner(other):\n'
                          '        assert other is not None\n'
                          '        return {name: item + value for name, item in other.items()}\n'
                          '    try:\n'
                          '        return inner({"key": 3})\n'
                          '    except (ValueError, TypeError):\n'
                          '        return None\n')
        self.commit(self.kit)
        self.receipt["kit_commit"] = self.git(self.kit, "rev-parse", "HEAD").strip()
        self.git(self.kit, "update-ref", "refs/remotes/origin/main", self.receipt["kit_commit"])
        item = next(item for item in self.receipt["files"] if item["relative_path"] == "workflowkit/delivery.py")
        item["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        item["byte_size"] = source.stat().st_size
        self.pin()
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        process = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import workflowkit.delivery",
             str(self.kit)], env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(Path(importlib.util.cache_from_source(str(source))).is_file())
        self.assert_source_admission_preserves_fixture_state()
        for invalidation in (py_compile.PycInvalidationMode.TIMESTAMP,
                             py_compile.PycInvalidationMode.CHECKED_HASH,
                             py_compile.PycInvalidationMode.UNCHECKED_HASH):
            for optimize in (0, 1, 2):
                py_compile.compile(str(source), doraise=True, optimize=optimize,
                                   invalidation_mode=invalidation)
            with self.subTest(invalidation=invalidation):
                self.assert_source_admission_preserves_fixture_state()

    def test_source_admission_trusts_owner_cache_even_when_import_executes_poison(self):
        source = self.kit / "workflowkit/delivery.py"
        cache = Path(py_compile.compile(str(source), doraise=True))
        # A valid timestamp/size header is not a binding of the executable body.
        replacement = compile("raise RuntimeError('unqualified-cache-executed')",
                              str(source), "exec", dont_inherit=True)
        cache.write_bytes(cache.read_bytes()[:16] + marshal.dumps(replacement))
        process = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import workflowkit.delivery", str(self.kit)],
            capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("unqualified-cache-executed", process.stderr)
        # This excluded guarantee is deliberate: unchanged qualified source
        # passes even though ordinary import can execute owner-controlled cache.
        self.assert_source_admission_preserves_fixture_state()

    def test_staged_only_infrastructure_change_fails(self):
        task = self.base / "task"
        self.git(self.project, "worktree", "add", "-qb", "task", str(task))
        self.put(task, ".kent/project-contract.md", "staged\n")
        self.git(task, "add", ".kent/project-contract.md")
        shutil.copy2(self.project / ".kent/project-contract.md", task / ".kent/project-contract.md")
        with self.assertRaises(ValueError):
            self.inspect(workspace_path=task)

    def test_wrong_receipt_digest_identity_policy_and_duplicate_keys_fail(self):
        with self.assertRaises(ValueError):
            self.inspect(receipt_sha256="0" * 64)
        for key, value in (("project_id", "foreign"), ("workflow_id", "foreign"),
                           ("policy", "unknown"), ("source_commit", "HEAD")):
            with self.subTest(key=key):
                original = self.receipt[key]
                self.receipt[key] = value
                self.pin()
                with self.assertRaises(ValueError):
                    self.inspect()
                self.receipt[key] = original
        self.pin(b'{"schema":"one","schema":"two"}')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_missing_or_short_or_unpublished_refs_fail(self):
        for ref in ("main", "HEAD", "refs/heads/master", "refs/remotes/missing/main"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.inspect(source_ref=ref)
        self.git(self.project, "update-ref", "-d", "refs/remotes/origin/main")
        with self.assertRaises(ValueError):
            self.inspect()

    def test_dirty_primary_and_foreign_task_repository_fail(self):
        self.put(self.project, "product.txt", "dirty primary\n")
        with self.assertRaises(ValueError):
            self.inspect()
        self.commit(self.project)
        self.inspect()
        with self.assertRaises(ValueError):
            self.inspect(workspace_path=self.kit)

    def test_foreign_repository_copy_with_same_commit_is_not_native_worktree(self):
        clone = self.base / "clone"
        self.git(self.base, "clone", "-q", "--no-hardlinks", str(self.project), str(clone))
        with self.assertRaises(ValueError):
            self.inspect(workspace_path=clone)

    def test_git_environment_routing_does_not_select_another_repository(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.kit / ".git"),
                                         "GIT_WORK_TREE": str(self.kit),
                                         "GIT_CONFIG_COUNT": "1",
                                         "GIT_CONFIG_KEY_0": "core.worktree",
                                         "GIT_CONFIG_VALUE_0": str(self.kit)}):
            self.inspect()

    def test_published_local_tag_can_bind_offline_source(self):
        for root in (self.project, self.kit):
            self.git(root, "tag", "approved-source")
        self.inspect(source_ref="refs/tags/approved-source", kit_ref="refs/tags/approved-source")

    def test_git_readback_is_bounded(self):
        with self.assertRaises(ValueError):
            self.module.git(self.project, "rev-parse", "HEAD", maximum=1)

    def test_receipt_symlink_ancestor_and_unsafe_mode_are_rejected(self):
        alias = self.base / "evidence-alias"
        alias.symlink_to(self.evidence, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.inspect(receipt_path=alias / self.receipt_path.name)
        self.receipt_path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.inspect()

    def test_duplicate_unknown_mutable_and_forged_source_entries_fail(self):
        original = json.loads(json.dumps(self.receipt))
        mutations = [
            lambda value: value["files"].append(dict(value["files"][0])),
            lambda value: value["files"][0].update(extra="unknown"),
            lambda value: value["files"][0].update(sha256="0" * 64),
            lambda value: value["files"][0].update(byte_size=True),
            lambda value: value["files"][0].update(mode=0o4755),
            lambda value: value["files"][0].update(relative_path=".kent/runtime/task/ledger"),
            lambda value: value["runtime_roots"].append(str(self.base)),
            lambda value: value["project_memory"].update(legacy_task_ids=["same", "same"]),
            lambda value: value["project_memory"].update(project_uid="foreign-config"),
            lambda value: value.update(approved=True),
        ]
        for mutation in mutations:
            self.receipt = json.loads(json.dumps(original))
            mutation(self.receipt)
            self.pin()
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, OSError)):
                self.inspect()

    def test_oversized_receipt_and_file_inventory_fail(self):
        self.pin(b" " * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.inspect()
        self.receipt["files"] *= 257
        self.pin()
        with self.assertRaises(ValueError):
            self.inspect()

    def test_receipt_file_and_aggregate_byte_limits_remain_unchanged(self):
        max_file = 8 * 1024 * 1024
        max_total = 32 * 1024 * 1024
        self.assertEqual(self.module.MAX_FILE, max_file)
        self.assertEqual(self.module.MAX_TOTAL, max_total)
        import runpy
        adapter = runpy.run_path(str(ADAPTER_TEMPLATE))
        self.assertEqual(adapter["MAX_NATIVE_TASKS"], 256)
        self.assertEqual(adapter["MAX_RECEIPT_BYTES"], 1024 * 1024)
        self.assertEqual(adapter["MAX_SOURCE_FILES"], 256)
        self.assertEqual(adapter["MAX_SOURCE_RECEIPT_FILES"], 512)

        self.receipt["files"][0]["byte_size"] = max_file + 1
        self.pin()
        with self.assertRaisesRegex(ValueError, "invalid source metadata"):
            self.inspect()

        payload = b"x" * max_file
        payload_digest = hashlib.sha256(payload).hexdigest()
        source_modes = {}
        for item in self.receipt["files"]:
            source_modes[(item["root"], item["relative_path"])] = item["mode"]
            item["byte_size"] = max_file
            item["sha256"] = payload_digest
        self.pin()
        actual_git = self.module.git
        actual_read_file = self.module.read_file

        def bounded_git(root, *arguments, maximum=32 * 1024 * 1024):
            if arguments[:2] == ("cat-file", "blob"):
                return payload
            return actual_git(root, *arguments, maximum=maximum)

        def bounded_read(path, maximum=8 * 1024 * 1024, *, private=False):
            path = Path(path)
            for (root, relative), mode in source_modes.items():
                if path == Path(root) / relative:
                    return payload, mode
            return actual_read_file(path, maximum, private=private)

        with mock.patch.dict(self.module.__dict__, {
                "git": bounded_git, "read_file": bounded_read}):
            with self.assertRaisesRegex(ValueError, "source inspection exceeds limit"):
                self.inspect()

    def test_512_source_receipt_entries_pass_and_513_fail_without_native_effect(self):
        self.native_adapter_fixture()
        self.add_receipt_sources_until(512)
        counts = {
            root: sum(item["root"] == root for item in self.receipt["files"])
            for root in (str(self.project), str(self.kit))
        }
        self.assertGreater(counts[str(self.project)], 100)
        self.assertGreater(counts[str(self.kit)], 100)
        self.assertLess(self.receipt_path.stat().st_size, 1024 * 1024)

        inspection = subprocess.run(self.cli(), capture_output=True, text=True, timeout=120)
        self.assertEqual(inspection.returncode, 0, inspection.stderr)
        self.assertEqual(json.loads(inspection.stdout)["file_count"], 512)

        self.native_task = self.make_native_task(task_id="task-512", short_id="OSM-512")
        self.write_fake_kent(self.native_task)
        admitted = self.adapter_call("workflow", "initialize-task", task_id="task-512")
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        self.assertTrue((self.memory_project / "tasks/OSM-512/task.json").is_file())

        self.add_receipt_sources_until(513)
        inspection = subprocess.run(self.cli(), capture_output=True, text=True, timeout=120)
        self.assertEqual(inspection.returncode, 2)
        self.assertEqual(json.loads(inspection.stdout)["status"], "blocked")

        self.native_task = self.make_native_task(task_id="task-513", short_id="OSM-513")
        self.write_fake_kent(self.native_task)
        blocked = self.adapter_call("workflow", "initialize-task", task_id="task-513")
        self.assertEqual(blocked.returncode, 2)
        self.assertFalse((self.memory_project / "tasks/OSM-513").exists())

    def test_additional_installed_helper_binding_is_checked(self):
        helper = self.base / "bin/kent-checkpoint"
        target = self.project / ".kent/scripts/workflow-checkpoint"
        helper.symlink_to(target)
        self.inspect(helper_bindings={str(helper): str(target)})
        process = subprocess.run(
            self.cli("--installed-helper", str(helper), str(target)), capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        helper.unlink()
        helper.symlink_to(self.kit / "scripts/prepare-github-ci")
        with self.assertRaises(ValueError):
            self.inspect(helper_bindings={str(helper): str(target)})

    def test_unsafe_installed_helper_directory_fails(self):
        self.helper.parent.chmod(0o777)
        with self.assertRaises(ValueError):
            self.inspect()

    def test_source_change_after_initial_read_is_not_admitted(self):
        original = self.module.verify_copy
        changed = False

        def mutate(*arguments, **keywords):
            nonlocal changed
            original(*arguments, **keywords)
            if not changed:
                changed = True
                self.put(self.kit, "workflowkit/delivery.py", "late drift\n")

        with mock.patch.object(self.module, "verify_copy", side_effect=mutate):
            with self.assertRaises(ValueError):
                self.inspect()

    def test_changed_tracked_or_new_untracked_task_infrastructure_fails(self):
        task = self.base / "task"
        self.git(self.project, "worktree", "add", "-qb", "task", str(task))
        self.put(task, ".kent/scripts/workflow-checkpoint", "changed\n", 0o755)
        with self.assertRaises(ValueError):
            self.inspect(workspace_path=task)
        shutil.copy2(self.project / ".kent/scripts/workflow-checkpoint",
                     task / ".kent/scripts/workflow-checkpoint")
        self.put(task, ".kent/scripts/unpublished-helper", "new\n")
        with self.assertRaises(ValueError):
            self.inspect(workspace_path=task)

    def test_published_descendant_infrastructure_drift_fails(self):
        self.put(self.project, ".kent/project-contract.md", "changed\n")
        self.commit(self.project)
        with self.assertRaises(ValueError):
            self.inspect()

    def test_helper_substitution_and_module_byte_drift_fail(self):
        self.helper.unlink()
        self.helper.symlink_to(self.project / ".kent/scripts/workflow-checkpoint")
        with self.assertRaises(ValueError):
            self.inspect()
        self.helper.unlink()
        self.helper.symlink_to(self.kit / "scripts/prepare-github-ci")
        self.put(self.kit, "workflowkit/delivery.py", "tampered\n")
        with self.assertRaises(ValueError):
            self.inspect()

    def test_symlink_hardlink_unsafe_modes_and_path_escape_fail(self):
        item = next(item for item in self.receipt["files"]
                    if item["relative_path"] == ".kent/project-contract.md")
        path = self.project / item["relative_path"]
        original = path.read_bytes()
        outside = self.base / "outside"
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.inspect()
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(ValueError):
            self.inspect()
        path.unlink()
        path.write_bytes(original)
        path.chmod(0o666)
        with self.assertRaises(ValueError):
            self.inspect()
        path.chmod(0o644)
        item["relative_path"] = "../outside"
        self.pin()
        with self.assertRaises(ValueError):
            self.inspect()

    def test_native_r2_source_admission_accepts_current_and_resumed_tasks(self):
        self.native_adapter_fixture()
        self.initialize_memory_task(
            "OSM-130", "task-uid",
            [{"namespace": "kent", "project_id": "project-test", "task_id": "task-native"}])

        normal = self.adapter_call("workflow", "resolve-task")
        self.assertEqual(normal.returncode, 0, normal.stderr)
        self.assertEqual(json.loads(normal.stdout)["result"]["mode"], "r2")

        resumed = self.adapter_call("workflow", "resolve-task", task_id=None)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["result"]["mode"], "r2")
        located = self.adapter_call(
            "workflow", "resolve-task", task_id=None,
            extra={"task_locator": "task-native"})
        self.assertEqual(located.returncode, 0, located.stderr)
        self.assertEqual(json.loads(located.stdout)["result"]["mode"], "r2")

    def test_native_ingress_preserves_verified_immutable_intake_source(self):
        self.native_adapter_fixture()
        created = self.memory_cli({
            "schema": "r2-request-v1",
            "operation": "initialize-intake",
            "invocation_id": "0baed477-3514-4ef5-b072-323303701a8c",
            "intent": {"issue": "TEST-1"}})
        self.assertEqual(created.returncode, 0, created.stderr)
        intake = json.loads(created.stdout)["result"]
        source = {"project_uid": intake["project_uid"], "task_uid": intake["task_uid"],
                  "sha256": intake["task_binding_sha256"]}
        source_path = self.memory_project / "tasks" / intake["task_key"] / "task.json"
        before = source_path.read_bytes()
        target = self.memory_project / "tasks/OSM-130/task.json"
        rejected = self.adapter_call(
            "workflow", "initialize-task",
            extra={"source_refs": [{**source, "sha256": "0" * 64}]})
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(target.exists())
        for _ in range(2):
            accepted = self.adapter_call(
                "workflow", "initialize-task", extra={"source_refs": [source]})
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(target.read_text())["source_refs"], [source])
        self.assertEqual(source_path.read_bytes(), before)
        resumed = self.adapter_call("workflow", "resolve-task")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_native_attachment_wire_roundtrip_and_bad_digest_no_write(self):
        self.native_adapter_fixture()
        self.initialize_memory_task(
            "OSM-130", "task-uid",
            [{"namespace": "kent", "project_id": "project-test", "task_id": "task-native"}])
        content = b"\x00\xffimage\nbytes"
        digest = hashlib.sha256(content).hexdigest()
        env = {"HOME": str(self.home),
               "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
               "KENT_SESSION_ID": "session-native"}

        def call(operation, body=None, **fields):
            request = {"schema": "r2-workflow-request-v1", "operation": operation,
                       "workspace_path": str(self.project), "task_locator": "task-native",
                       **fields}
            wire = json.dumps(request).encode() + (b"\n" + body if body is not None else b"")
            return subprocess.run(
                [sys.executable, str(self.project / ".kent/scripts/workflow-project-memory"),
                 "workflow"], input=wire, capture_output=True, timeout=60, env=env)

        bad = call("artifact-write", content, sha256="0" * 64, byte_size=len(content))
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, b"")
        self.assertFalse((self.memory_project / "tasks/OSM-130/artifacts").exists())
        written = call("artifact-write", content, sha256=digest, byte_size=len(content))
        self.assertEqual(written.returncode, 0, written.stderr)
        header = json.loads(written.stdout)
        self.assertEqual(header["schema"], "r2-artifact-response-v1")
        self.assertEqual(Path(header["object_path"]).read_bytes(), content)
        read = call("artifact-read", sha256=digest)
        self.assertEqual(read.returncode, 0, read.stderr)
        metadata, raw = read.stdout.split(b"\n", 1)
        self.assertEqual(json.loads(metadata)["byte_size"], len(content))
        self.assertEqual(raw, content)

    def test_native_first_ingress_qualifies_before_task_binding_write(self):
        self.native_adapter_fixture()
        self.native_task = self.make_native_task(task_id="task-new", short_id="OSM-131")
        self.write_fake_kent(self.native_task)
        initialized = self.adapter_call("workflow", "initialize-task", task_id="task-new")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertTrue((self.memory_project / "tasks/OSM-131/task.json").is_file())

        self.installation_binding["source_ref"] = "HEAD"
        self.write_installation_binding()
        self.native_task = self.make_native_task(task_id="task-blocked", short_id="OSM-132")
        self.write_fake_kent(self.native_task)
        blocked = self.adapter_call("workflow", "initialize-task", task_id="task-blocked")
        self.assertEqual(blocked.returncode, 2)
        self.assertFalse((self.memory_project / "tasks/OSM-132").exists())

    def test_fixed_legacy_task_routes_before_candidate_source_closure(self):
        self.native_adapter_fixture(legacy_task_ids=("task-legacy",))
        self.native_task = self.make_native_task(task_id="task-legacy", short_id="OSM-120")
        self.write_fake_kent(self.native_task)
        self.put(self.project, ".kent/project-contract.md", "old task infrastructure\n")
        (self.kit / "scripts/inspect-delivery-source").unlink()
        legacy = self.adapter_call("workflow", "resolve-task", task_id="task-legacy")
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(json.loads(legacy.stdout)["result"]["mode"], "legacy")
        self.assertFalse((self.memory_project / "tasks/OSM-120").exists())

    def test_resolve_janitor_returns_only_qualified_kit_receipt_descriptors(self):
        self.native_adapter_fixture()
        task = self.make_native_task(workflow_id="workflow-other")
        task["current_scripts"] = [{
            "current_node": {"node_id": "node-native", "transition_branch_key": None},
        }]
        self.native_task = task
        self.write_fake_kent(task)
        profile = self.project / ".kent/workflow-profile.toml"
        profile.write_text(profile.read_text().replace(
            "[project_memory]",
            'janitor = ".kent/scripts/workflow-sealed-task-janitor"\n'
            "[project_memory]"))
        self.put(self.project, ".kent/scripts/workflow-sealed-task-janitor",
                 "#!/usr/bin/env python3\n# primary bridge, never select as Kit Janitor\n", 0o755)
        self.repin_native_sources()

        result = self.adapter_call(
            "workflow", "resolve-janitor", task_id=None, session_id=None,
            native_context={"task_id": "task-native", "node_id": "node-native"})
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["schema"], "r2-workflow-response-v1")
        binding = envelope["result"]
        self.assertEqual(set(binding), {
            "schema", "mode", "janitor", "runtime_siblings",
        })
        self.assertEqual(binding["schema"], "r2-janitor-source-binding-v1")
        self.assertEqual(binding["mode"], "r2")
        self.assertEqual(binding["janitor"]["path"],
                         str(self.kit / ".kent/scripts/workflow-task-janitor"))
        self.assertEqual(len(binding["runtime_siblings"]), 1)
        self.assertEqual(binding["runtime_siblings"][0]["path"],
                         str(self.kit / ".kent/scripts/workflow_runtime_contracts.py"))
        for descriptor in [binding["janitor"], *binding["runtime_siblings"]]:
            self.assertEqual(set(descriptor), {"path", "sha256", "byte_size", "mode"})
            path = Path(descriptor["path"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             descriptor["sha256"])
            self.assertEqual(path.stat().st_size, descriptor["byte_size"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), descriptor["mode"])
            self.assertEqual(descriptor["path"].startswith(str(self.kit) + os.sep), True)
        self.assertFalse((self.memory_project / "tasks/OSM-130/task.json").exists())

    def test_resolve_janitor_requires_exact_current_script_and_closed_request(self):
        self.native_adapter_fixture()
        task = self.make_native_task()
        task["current_scripts"] = [{
            "current_node": {"node_id": "node-native", "transition_branch_key": None},
        }]
        self.native_task = task
        self.write_fake_kent(task)
        context = {"task_id": "task-native", "node_id": "node-native"}

        valid = self.adapter_call("workflow", "resolve-janitor", task_id=None,
                                  session_id=None, native_context=context)
        self.assertEqual(valid.returncode, 0, valid.stderr)

        forged_node = self.adapter_call(
            "workflow", "resolve-janitor", task_id=None, session_id=None,
            native_context={"task_id": "task-native", "node_id": "foreign-node"})
        self.assertEqual(forged_node.returncode, 2)
        injected_path = self.adapter_call(
            "workflow", "resolve-janitor", task_id=None, session_id=None,
            native_context=context, extra={
                "janitor_path": str(self.project / ".kent/scripts/workflow-task-janitor")})
        self.assertEqual(injected_path.returncode, 2)
        self.assertFalse((self.memory_project / "tasks/OSM-130/task.json").exists())

    def test_resolve_janitor_legacy_uses_unchanged_early_route(self):
        self.native_adapter_fixture(legacy_task_ids=("task-legacy",))
        task = self.make_native_task(task_id="task-legacy", short_id="OSM-120")
        task["current_scripts"] = [{
            "current_node": {"node_id": "node-native", "transition_branch_key": None},
        }]
        self.native_task = task
        self.write_fake_kent(task)
        self.put(self.project, ".kent/project-contract.md", "legacy-only source\n")
        (self.kit / "scripts/inspect-delivery-source").unlink()
        result = self.adapter_call(
            "workflow", "resolve-janitor", task_id=None, session_id=None,
            native_context={"task_id": "task-legacy", "node_id": "node-native"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"],
                         {"mode": "legacy", "binding_ref": None})
        self.assertFalse((self.memory_project / "tasks/OSM-120/task.json").exists())

    def test_resolve_janitor_missing_or_mismatched_receipt_entry_blocks(self):
        self.native_adapter_fixture()
        task = self.make_native_task()
        task["current_scripts"] = [{
            "current_node": {"node_id": "node-native", "transition_branch_key": None},
        }]
        self.native_task = task
        self.write_fake_kent(task)
        original = json.loads(json.dumps(self.receipt))
        context = {"task_id": "task-native", "node_id": "node-native"}
        identities = [
            (str(self.kit), ".kent/scripts/workflow-task-janitor"),
            (str(self.kit), ".kent/scripts/workflow_runtime_contracts.py"),
        ]
        for root, relative in identities:
            with self.subTest(relative=relative):
                self.receipt = json.loads(json.dumps(original))
                self.receipt["files"] = [
                    item for item in self.receipt["files"]
                    if (item["root"], item["relative_path"]) != (root, relative)]
                self.pin()
                self.installation_binding["receipt_sha256"] = self.digest
                self.write_installation_binding()
                missing = self.adapter_call(
                    "workflow", "resolve-janitor", task_id=None, session_id=None,
                    native_context=context)
                self.assertEqual(missing.returncode, 2)
                self.assertEqual(missing.stdout, "")

                self.receipt = json.loads(json.dumps(original))
                entry = next(item for item in self.receipt["files"]
                             if (item["root"], item["relative_path"]) == (root, relative))
                entry["sha256"] = "0" * 64
                self.pin()
                self.installation_binding["receipt_sha256"] = self.digest
                self.write_installation_binding()
                mismatch = self.adapter_call(
                    "workflow", "resolve-janitor", task_id=None, session_id=None,
                    native_context=context)
                self.assertEqual(mismatch.returncode, 2)
                self.assertEqual(mismatch.stdout, "")

        self.assertFalse((self.memory_project / "tasks/OSM-130/task.json").exists())

    def test_standalone_mcp_uses_native_project_binding_without_task_ownership(self):
        self.native_adapter_fixture()
        self.initialize_memory_task("intake-123", "intake-uid", [])
        task_path = self.memory_project / "tasks/intake-123/task.json"
        standalone = {
            "project_path": str(self.memory_project), "project_uid": "project-uid",
            "task_key": "intake-123", "task_uid": "intake-uid",
            "task_binding_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
        }
        result = self.adapter_call("mcp", "resolve-mcp", task_id=None, session_id=None,
                                   standalone_binding=standalone)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["mode"], "r2")

    def test_source_admission_mismatch_blocks_before_candidate_core_import(self):
        self.native_adapter_fixture()
        marker = self.base / "candidate-core-imported"
        candidate_core = self.project / ".kent/scripts/workflow_project_memory.py"
        candidate_core.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        blocked = self.adapter_call("workflow", "initialize-task", task_id="task-new")
        self.assertEqual(blocked.returncode, 2)
        self.assertFalse(marker.exists())
        self.assertFalse((self.memory_project / "tasks/OSM-130").exists())
        self.assertNotIn("candidate-core-imported", blocked.stdout + blocked.stderr)

    def test_unpinned_installed_inspector_is_not_executed(self):
        self.native_adapter_fixture()
        marker = self.base / "candidate-inspector-imported"
        self.put(self.kit, "scripts/inspect-delivery-source",
                 f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", 0o755)
        blocked = self.adapter_call("workflow", "initialize-task", task_id="task-new")
        self.assertEqual(blocked.returncode, 2)
        self.assertFalse(marker.exists())
        self.assertFalse((self.memory_project / "tasks/OSM-130").exists())

    def test_standalone_mcp_without_native_project_binding_blocks_without_effect(self):
        self.native_adapter_fixture()
        self.initialize_memory_task("intake-123", "intake-uid", [])
        task_path = self.memory_project / "tasks/intake-123/task.json"
        standalone = {
            "project_path": str(self.memory_project), "project_uid": "project-uid",
            "task_key": "intake-123", "task_uid": "intake-uid",
            "task_binding_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
        }
        path = self.base / "bin/no-kent"
        path.mkdir()
        (path / "git").symlink_to(shutil.which("git"))
        result = self.adapter_call(
            "mcp", "resolve-mcp", task_id=None, session_id=None,
            standalone_binding=standalone, environment={"PATH": str(path)})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(str(self.memory_project), result.stderr)

    def test_published_r2_acceptance_reaches_real_branch_selection_without_cache(self):
        install_r2_lifecycle_sources(self, coder_selection="complexity")
        accepted = accept_r2_plan(self)
        self.assertEqual(
            accepted["accepted_snapshot"]["authority_refs"],
            [FIXTURE_PLAN_REFERENCE],
        )
        self.assertEqual(
            accepted["accepted_snapshot"]["plan_kind"], "markdown",
        )
        self.assertEqual(
            accepted["accepted_snapshot"]["coder_selection"]["complexity"],
            "complex",
        )
        self.assertIn(FIXTURE_PLAN_REFERENCE, accepted["handoff"]["review_context"])

        accepted_head = accepted["accepted_path"].read_bytes()
        accepted["cache_path"].unlink()
        self.assertFalse(accepted["cache_path"].exists())
        branch = branch_r2_plan(self, accepted)

        self.assertEqual(branch["transition"], "branch_identity_ready", branch)
        self.assertEqual(branch["target_assignee"], "implementation-complex")
        self.assertEqual(branch["target_thinking"], "medium")
        self.assertEqual(branch["accepted_plan_sha256"],
                         accepted["accepted_snapshot"]["normalized_sha256"])
        self.assertFalse(accepted["cache_path"].exists())
        self.assertEqual(accepted["accepted_path"].read_bytes(), accepted_head)
        self.assertEqual(
            json.loads(accepted["accepted_path"].read_bytes())["authority_refs"],
            [FIXTURE_PLAN_REFERENCE],
        )
        self.assertFalse((self.project / ".todo").exists())


if __name__ == "__main__":
    unittest.main()
