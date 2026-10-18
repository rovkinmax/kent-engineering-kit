from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import runpy
import uuid
from unittest import mock

import workflowkit.operations as operations
REAL_ADMIT_SOURCE_ADMISSION = operations.admit_source_admission
from workflowkit.operations import (
    EffectBlocked,
    JournalError,
    OperationJournal,
    PlanValidationError,
    canonical_bytes,
    canonical_sha256,
    load_plan,
    reconcile_canonical_workflows,
)
from tests.test_workflow_retirement import (
    PROJECT_ID as RETIREMENT_PROJECT_ID,
    make_d9_fixture as make_retirement_fixture,
    read_log,
    read_state,
    workflow_id,
    write_state,
)
from tests.test_delivery_source_admission import DeliverySourceAdmissionTests

PROJECT_ID = "project-" + RETIREMENT_PROJECT_ID


class NativeGraphTransportTests(unittest.TestCase):
    def test_large_packet_tamper_never_releases_child(self):
        from tests.test_workflow_retirement import load_test_plan, PYTHON
        transfer = operations._transfer_guardian_stdin
        payload = b"x" * 427648
        for delivered in (payload[:-1], b"y" + payload[1:]):
            with self.subTest(length=len(delivered)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / "released"
                with OperationJournal(root / "state", "effect-test", load_test_plan(root)) as journal:
                    journal.persist({"phase": "in_progress", "effects": {}})
                    with mock.patch.object(operations, "_transfer_guardian_stdin",
                        side_effect=lambda process, data, deadline, timeout:
                            transfer(process, delivered, deadline, timeout)):
                        with self.assertRaises(EffectBlocked):
                            operations.run_effect(
                                journal, effect_key="graph", command=[PYTHON, "-c",
                                    "import sys; from pathlib import Path; Path(sys.argv[1]).touch()", str(marker)],
                                cwd=root, stdin_bytes=payload, stdin_limit=1024 * 1024,
                                preimage_sha256="a" * 64, postimage_sha256="b" * 64,
                                current_sha256=lambda: "a" * 64)
                    self.assertFalse(marker.exists())
                    with mock.patch.object(operations.subprocess, "Popen", side_effect=AssertionError("replay")):
                        self.assertEqual(operations.recover_effect(
                            journal, effect_key="graph", command=[PYTHON, "-c",
                                "import sys; from pathlib import Path; Path(sys.argv[1]).touch()", str(marker)],
                            cwd=root, stdin_bytes=payload, stdin_limit=1024 * 1024,
                            preimage_sha256="a" * 64, postimage_sha256="b" * 64,
                            current_sha256=lambda: "a" * 64), "preimage")

    def test_only_native_v2_graph_selects_larger_bound(self):
        item = {"compatibility_snapshot": {"native_components": {"schema": operations.NATIVE_COMPONENTS_V2_SCHEMA}}}
        self.assertEqual(operations._canonical_v2_stdin_limit(
            {"native_evidence": {}}, item, {"name": "graph"}), 1024 * 1024)
        for parsed, stage in (({}, "graph"), ({"native_evidence": {}}, "metadata")):
            self.assertEqual(operations._canonical_v2_stdin_limit(parsed, item, {"name": stage}), operations.MAX_OUTPUT)
        item["compatibility_snapshot"]["native_components"]["schema"] = "kent-native-components-v1"
        self.assertEqual(operations._canonical_v2_stdin_limit(
            {"native_evidence": {}}, item, {"name": "graph"}), operations.MAX_OUTPUT)

    def test_large_graph_pipe_and_hard_bounds(self):
        from tests.test_workflow_retirement import load_test_plan, PYTHON
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "packet"
            for size in (427648, 1024 * 1024):
                payload = (bytes(range(256)) * (size // 256 + 1))[:size]
                with OperationJournal(root / str(size), "effect-test", load_test_plan(root)) as journal:
                    journal.persist({"phase": "in_progress", "effects": {}})
                    result = operations.run_effect(
                        journal, effect_key="graph", command=[PYTHON, "-c",
                            "import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read()); sys.stdout.buffer.write(b'o'*400000); sys.stderr.buffer.write(b'e'*400000)",
                            str(marker)], cwd=root, stdin_bytes=payload, stdin_limit=1024 * 1024,
                        preimage_sha256="a" * 64, postimage_sha256="b" * 64,
                        current_sha256=lambda: "b" * 64 if marker.exists() and marker.read_bytes() == payload else "a" * 64,
                    )
                    self.assertEqual(result.settlement, "postimage")
                    self.assertEqual(marker.read_bytes(), payload)
                    self.assertEqual(result.stdout_sha256, hashlib.sha256(b"o" * operations.MAX_OUTPUT).hexdigest())
                    self.assertEqual(result.stderr_sha256, hashlib.sha256(b"e" * operations.MAX_OUTPUT).hexdigest())
            for limit in (operations.MAX_OUTPUT, 1024 * 1024):
                with self.assertRaises(PlanValidationError):
                    operations._effect_inputs([PYTHON, "-c", "pass"], root, None,
                        b"x" * (limit + 1), None, None, stdin_limit=limit)


def create_verified_source_fixture(
    testcase: unittest.TestCase,
    project_id: str,
    workflow_id_value: str,
    *,
    local_instruction: bool = False,
) -> tuple[DeliverySourceAdmissionTests, operations.VerifiedSourceAdmission]:
    """Create an offline native/install/source fixture and verify it end to end."""
    fixture = DeliverySourceAdmissionTests(
        "test_published_closure_and_cli_are_read_only"
    )
    fixture.setUp()
    testcase.addCleanup(fixture.doCleanups)
    fixture.native_adapter_fixture()
    if local_instruction:
        fixture.add_local_instruction()

    fixture.receipt["project_id"] = project_id
    fixture.receipt["workflow_id"] = workflow_id_value
    fixture.pin()
    fixture.installation_binding["project_id"] = project_id
    fixture.installation_binding["receipt_sha256"] = fixture.digest
    fixture.binding_path = fixture.binding_dir / f"{project_id}.json"
    task = fixture.make_native_task(workflow_id=workflow_id_value)
    task["summary"]["project_id"] = project_id
    fixture.native_task = task
    fixture.write_fake_kent(task)
    native_kent_source = fixture.native_bin.read_text().replace("project-test", project_id)
    native_kent_source = native_kent_source.replace(
        "args[4] != '--limit' or args[6] != '--offset'",
        "args[4] != '--offset' or args[6] != '--limit'",
    ).replace(
        "limit, offset = int(args[5]), int(args[7])",
        "offset, limit = int(args[5]), int(args[7])",
    )
    fixture.native_bin.write_text(native_kent_source)
    fixture.write_installation_binding()
    admission = {
        "schema": "delivery-source-admission-v1",
        "receipt_ref": {
            "path": str(fixture.receipt_path),
            "sha256": fixture.digest,
        },
        "installation_binding_sha256": hashlib.sha256(
            fixture.binding_path.read_bytes()
        ).hexdigest(),
        "primary_workspace_id": "workspace-test",
        "primary_root": str(fixture.project),
        "kit_root": str(fixture.kit),
        "source_ref": "refs/remotes/origin/main",
        "kit_ref": "refs/remotes/origin/main",
        "ci_helper": str(fixture.ci_helper),
        "helper_bindings": [{
            "link": str(fixture.adapter_link),
            "target": str(fixture.adapter_target),
        }],
    }
    with mock.patch.object(operations.Path, "home", return_value=fixture.home), \
            mock.patch.dict("os.environ", {"HOME": str(fixture.home)}):
        admitted = REAL_ADMIT_SOURCE_ADMISSION(
            admission,
            project_id=project_id,
            workflow_id=workflow_id_value,
            project_root=fixture.project,
            kent_path=fixture.native_bin,
        )
    fixture.source_admission = admitted.value
    fixture.verified_source_admission = admitted
    return fixture, admitted


def synthetic_source_admission(root: Path) -> dict:
    receipt_path = root / "source-publication.json"
    receipt_path.write_bytes(b"source-publication")
    return {
        "schema": "delivery-source-admission-v1",
        "receipt_ref": {
            "path": str(receipt_path),
            "sha256": hashlib.sha256(b"source-publication").hexdigest(),
        },
        "installation_binding_sha256": hashlib.sha256(b"installation-binding").hexdigest(),
        "primary_workspace_id": "workspace-test",
        "primary_root": str(root),
        "kit_root": str(root / "published-kit"),
        "source_ref": "refs/remotes/origin/main",
        "kit_ref": "refs/remotes/origin/main",
        "ci_helper": str(root / "bin/kent-prepare-github-ci"),
        "helper_bindings": [],
    }


def synthetic_verified_source_admission(
    admission: dict,
    *,
    project_id: str,
    workflow_id: str,
    project_root: str | Path,
    kent_path: str | Path,
) -> operations.VerifiedSourceAdmission:
    del kent_path
    parsed = operations._v2_source_admission(admission, "source_admission")
    if parsed is None:
        raise AssertionError("synthetic source admission cannot be null")
    from workflowkit.delivery import source_admission_gate

    root = str(Path(project_root))
    inspection = {
        "schema": "delivery-source-inspection-v1",
        "status": "admitted",
        "project_id": project_id,
        "workflow_id": workflow_id,
        "workspace_path": root,
    }
    value_raw = canonical_bytes(parsed)
    return operations.VerifiedSourceAdmission(
        project_id=project_id,
        workflow_id=workflow_id,
        project_root=root,
        primary_workspace_id=parsed["primary_workspace_id"],
        primary_root=parsed["primary_root"],
        kit_root=parsed["kit_root"],
        source_admission_sha256=hashlib.sha256(value_raw).hexdigest(),
        receipt_sha256=parsed["receipt_ref"]["sha256"],
        gate=source_admission_gate(
            parsed,
            project_id=project_id,
            workflow_id=workflow_id,
            project_root=root,
        ),
        value_raw=value_raw,
        inspection_raw=canonical_bytes(inspection),
        receipt_files_raw=canonical_bytes([]),
        _token=operations._VERIFIED_SOURCE_ADMISSION_TOKEN,
    )


def make_d9_fixture(root: Path) -> dict:
    fixture = make_retirement_fixture(root)
    state = read_state(fixture)
    state["project_id"] = PROJECT_ID
    write_state(fixture, state)
    return fixture


def canonical_plan(
    fixture: dict,
    *,
    intent: str = "graph-only",
    metadata: dict | None = None,
    graph: dict | None = None,
    d9: dict | None = None,
    name: str = "canonical-plan.json",
) -> tuple[operations.LoadedPlan, dict]:
    wid = workflow_id(0)
    target_graph = graph or {
        "version": 2,
        "nodes": [
            {"id": "terminal", "kind": "terminal"},
            {"id": "review", "kind": "agent"},
        ],
        "edges": [],
        "node_groups": [],
        "transition_groups": [],
    }
    item = {
        "workflow_id": wid,
        "project_id": PROJECT_ID,
        "intent": intent,
        "expected_version": 1,
        "terminal_tasks": [{"id": "TASK-1", "status": "done"}],
        "terminal_anchors": [{"id": "terminal", "kind": "terminal"}],
        "links": [
            {
                "project_id": PROJECT_ID,
                "workflow_id": wid,
                "is_default": True,
            }
        ],
        "default": wid,
    }
    if intent in {"graph-only", "graph-and-metadata"}:
        item["graph"] = target_graph
    if intent in {"metadata-only", "graph-and-metadata"}:
        item["metadata"] = metadata or {
            "name": "Canonical Workflow",
            "description": "Canonical description",
            "execution_target": "ask-on-first-execution",
        }
    value = {
        "schema": "canonical-workflow-reconcile-plan-v1",
        "state_dir": str(fixture["root"] / "canonical-state"),
        "project_root": str(fixture["root"]),
        "kent": {
            "path": str(fixture["kent"]),
            "sha256": hashlib.sha256(fixture["kent"].read_bytes()).hexdigest(),
        },
        "d9": d9 or {"none": True},
        "workflows": [item],
    }
    path = fixture["root"] / name
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    plan = load_plan(
        path,
        schema="canonical-workflow-reconcile-plan-v1",
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    return plan, value


def graph_apply_rows(fixture: dict) -> list[list[str]]:
    return [row for row in read_log(fixture) if row[:3] == ["workflow", "graph", "apply"]]


def canonical_v2_plan(
    fixture: dict,
    *,
    intent: str = "graph-only",
    pending_approval: bool = False,
    metadata: dict | None = None,
    script_path_change: bool = False,
    source_admission_enabled: bool = True,
    name: str = "canonical-plan-v2.json",
) -> tuple[operations.LoadedPlan, dict]:
    state = read_state(fixture)
    wid = workflow_id(0)
    workflow = state["workflows"][wid]
    workflow["graph"]["nodes"] = [
        {"id": "node-backlog", "key": "backlog", "kind": "agent"},
        {"id": "node-terminal", "key": "terminal", "kind": "terminal"},
        {"id": "node-implement", "key": "implement", "kind": "agent"},
        {"id": "node-plan", "key": "plan", "kind": "agent"},
        {"id": "node-fix", "key": "fix", "kind": "agent"},
        {
            "id": "node-plan-revalidation",
            "key": "plan_revalidation",
            "kind": "agent",
        },
    ]
    if script_path_change:
        workflow["graph"]["nodes"].append({
            "id": "node-script",
            "key": "janitor",
            "kind": "script",
            "script_path": ".kent/scripts/old-janitor",
        })
    if not any(edge.get("id") == "edge-backlog-plan" for edge in workflow["graph"]["edges"]):
        workflow["graph"]["transition_groups"].append({
            "id": "group-backlog-plan",
            "source_node_id": "node-backlog",
            "transition_id": "backlog_plan",
            "description": "Start planning.",
        })
        workflow["graph"]["edges"].append({
            "id": "edge-backlog-plan",
            "key": "backlog_plan",
            "transition_group_id": "group-backlog-plan",
            "target_node_id": "node-plan",
            "assignee_selection": "configured",
            "thinking_selection": "configured",
            "requires_approval": False,
            "context_mode": "continue_session",
            "context_source": {"kind": "selected_node", "node_key": "backlog"},
            "prompt_template": "Begin plan revalidation.",
        })
    if not any(edge.get("key") == "fix_replan" for edge in workflow["graph"]["edges"]):
        workflow["graph"]["transition_groups"].append({
            "id": "group-fix-replan", "source_node_id": "node-fix",
            "transition_id": "fix_replan",
        })
        workflow["graph"]["edges"].append({
            "id": "edge-fix-replan", "key": "fix_replan",
            "transition_group_id": "group-fix-replan",
            "target_node_id": "node-plan-revalidation",
            "parameters": [], "prompt_template": "Revalidate after a fix.",
        })
    detail = workflow["details"]["TASK-1"]
    detail["live_sessions"] = []
    detail["current_nodes"] = [{"node_id": "node-terminal"}]
    detail["current_scripts"] = []
    detail["pending_approvals"] = (
        [{"id": "approval-1", "proposal": {"transition": "plan_revalidation"}}]
        if pending_approval
        else []
    )
    write_state(fixture, state)

    source_graph = {"version": workflow["version"], **workflow["graph"]}
    source_metadata = {
        "name": workflow["metadata"]["name"],
        "description": workflow["metadata"]["description"],
        "execution_target": workflow["metadata"]["execution_target"],
    }
    target_graph = json.loads(json.dumps(source_graph))
    target_graph["version"] += 1
    if not any(edge.get("key") == "implement_replan" for edge in source_graph["edges"]):
        target_graph["transition_groups"].append({
            "id": "group-implement-replan",
            "source_node_id": "node-implement",
            "transition_id": "implement_replan",
            "description": "Return to plan revalidation.",
        })
        target_graph["edges"].append({
            "id": "edge-implement-replan",
            "key": "implement_replan",
            "transition_group_id": "group-implement-replan",
            "target_node_id": "node-plan-revalidation",
            "assignee_selection": "configured",
            "thinking_selection": "configured",
            "requires_approval": False,
            "context_mode": "continue_session",
            "context_source": {"kind": "selected_node", "node_key": "plan"},
            "prompt_template": "Revalidate the approved plan.",
            "parameters": [],
        })
    source_admission = (
        synthetic_source_admission(Path(fixture["root"]))
        if source_admission_enabled
        else None
    )
    from workflowkit.delivery import SOURCE_ADMISSION_BEGIN, SOURCE_ADMISSION_END, source_admission_gate

    source_ingress = operations._v2_ingress_edge(source_graph, "source graph")
    target_ingress = operations._v2_ingress_edge(target_graph, "target graph")
    source_prompt = source_ingress["prompt_template"]
    if source_admission is None:
        pass
    elif SOURCE_ADMISSION_BEGIN in source_prompt:
        frame_end = source_prompt.find(SOURCE_ADMISSION_END)
        suffix_start = frame_end + len(SOURCE_ADMISSION_END)
        if frame_end < 0 or not source_prompt[suffix_start:].startswith("\n\n"):
            raise AssertionError("fixture source gate frame is malformed")
        suffix = source_prompt[suffix_start + 2:]
    else:
        suffix = source_prompt
    if source_admission is not None:
        target_ingress["prompt_template"] = (
            source_admission_gate(
                source_admission,
                project_id=PROJECT_ID,
                workflow_id=wid,
                project_root=str(fixture["root"]),
            )
            + "\n\n"
            + suffix
        )
    target_metadata = metadata or {
        **source_metadata,
        "description": "Updated with compatible plan revalidation.",
    }
    if intent == "graph-only":
        target_metadata = source_metadata
    elif intent == "metadata-only":
        target_graph = source_graph
    elif intent != "graph-and-metadata":
        raise AssertionError(f"unsupported v2 test intent: {intent}")

    session_evidence = Path(fixture["root"]) / "sessions" / "SESSION-1" / "events.jsonl"
    session_ref = {
        "path": str(session_evidence),
        "sha256": hashlib.sha256(session_evidence.read_bytes()).hexdigest(),
    }
    detail = read_state(fixture)["workflows"][wid]["details"]["TASK-1"]
    task_status = workflow["tasks"][0]["status"]
    task_detail_status = detail["status"]
    current_nodes = [
        row.get("node_id", row.get("id")) if isinstance(row, dict) else row
        for row in detail.get("current_nodes", [])
    ]
    task_snapshot = {
        "id": "TASK-1",
        "status": task_status.get("kind", task_status.get("status")),
        "native_state": task_status.get("native_state"),
        "terminal": task_detail_status.get("native_state") == "terminal",
        "current_nodes": current_nodes,
        "pending_decisions": [
            {
                "id": row["id"],
                "payload_sha256": canonical_sha256(row),
            }
            for row in detail["pending_approvals"]
        ],
        "history_classification": "reviewed-compatible",
        "history_refs": [session_ref],
    }
    session_snapshot = {
        "id": "SESSION-1",
        "task_id": "TASK-1",
        "status": "idle",
        "retained": True,
        "live_owner": None,
        "contract_fingerprint": canonical_sha256(
            {"session_id": "SESSION-1", "source_refs": [session_ref]}
        ),
        "source_refs": [session_ref],
    }
    compatibility_snapshot = {
        "schema": "canonical-workflow-compatibility-snapshot-v1",
        "workflow_id": wid,
        "project_id": PROJECT_ID,
        "expected_version": workflow["version"],
        "execution_target": source_metadata["execution_target"],
        "execution_root": str(fixture["root"]),
        "tasks": [task_snapshot],
        "sessions": [session_snapshot],
        "live_executions": [],
    }

    runtime_root = Path(fixture["root"]) / "published-runtime"
    runtime_root.mkdir(exist_ok=True)
    runtime_file = runtime_root / "janitor"
    runtime_file.write_text("#!/bin/sh\nexit 0\n")
    runtime_file.chmod(0o755)
    if not (runtime_root / ".git").exists():
        subprocess.run(["/usr/bin/git", "init", "-q", str(runtime_root)], check=True)
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(runtime_root),
                "config",
                "user.name",
                "Fixture",
            ],
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(runtime_root),
                "config",
                "user.email",
                "fixture@example.invalid",
            ],
            check=True,
        )
    subprocess.run(
        ["/usr/bin/git", "-C", str(runtime_root), "add", "janitor"],
        check=True,
    )
    if subprocess.run(
        ["/usr/bin/git", "-C", str(runtime_root), "status", "--porcelain"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout:
        subprocess.run(
            ["/usr/bin/git", "-C", str(runtime_root), "commit", "-q", "-m", "fixture"],
            check=True,
        )
    runtime_ref = {
        "path": str(runtime_file),
        "sha256": hashlib.sha256(runtime_file.read_bytes()).hexdigest(),
        "root": str(runtime_root),
        "relative_path": "janitor",
        "mode": stat.S_IMODE(runtime_file.stat().st_mode),
        "byte_size": runtime_file.stat().st_size,
        "published_commit": subprocess.run(
            ["/usr/bin/git", "-C", str(runtime_root), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip(),
    }
    runtime_refs = [runtime_ref]
    if script_path_change and intent in {"graph-only", "graph-and-metadata"}:
        next(node for node in target_graph["nodes"] if node["id"] == "node-script")[
            "script_path"
        ] = str(runtime_file)
    source_graph_sha256 = canonical_sha256(source_graph)
    source_metadata_sha256 = canonical_sha256(source_metadata)
    full_target_graph = (
        target_graph if intent in {"graph-only", "graph-and-metadata"} else source_graph
    )
    full_target_metadata = (
        target_metadata
        if intent in {"metadata-only", "graph-and-metadata"}
        else source_metadata
    )
    target_graph_sha256 = canonical_sha256(full_target_graph)
    target_metadata_sha256 = canonical_sha256(full_target_metadata)
    changed_paths = []
    if target_graph_sha256 != source_graph_sha256:
        if any(
            edge.get("key") == "implement_replan"
            and not any(
                before.get("id") == edge.get("id")
                for before in source_graph["edges"]
            )
            for edge in full_target_graph["edges"]
        ):
            changed_paths.append("graph.added_edge:implement_replan")
        if (
            operations._v2_ingress_edge(source_graph, "source graph")["prompt_template"]
            != operations._v2_ingress_edge(full_target_graph, "target graph")["prompt_template"]
        ):
            changed_paths.append("graph.edges:edge-backlog-plan.prompt_template")
        if script_path_change:
            changed_paths.append("graph.nodes:node-script.script_path")
    if target_metadata_sha256 != source_metadata_sha256:
        changed_paths.append("metadata.description")
    changed_paths.extend(["task:TASK-1", "session:SESSION-1"])
    review = {
        "schema": "canonical-workflow-compatibility-review-v1",
        "workflow_id": wid,
        "project_id": PROJECT_ID,
        "source_graph_sha256": source_graph_sha256,
        "source_metadata_sha256": source_metadata_sha256,
        "target_graph_sha256": target_graph_sha256,
        "target_metadata_sha256": target_metadata_sha256,
        "compatibility_snapshot_sha256": canonical_sha256(compatibility_snapshot),
        "runtime_refs_sha256": canonical_sha256(runtime_refs),
        "source_admission_sha256": (
            canonical_sha256(source_admission)
            if source_admission is not None
            else None
        ),
        "outcome": "PASS",
        "reviewer_session_id": "423e4567-e89b-12d3-a456-426614174111",
        "dispositions": [
            {
                "path": path,
                "disposition": "compatible",
                "evidence": [session_ref],
            }
            for path in changed_paths
        ] + [{
            "path": "task:TASK-1.pending_decisions",
            "disposition": "non-impact",
            "evidence": [session_ref],
        }],
    }
    review_path = Path(fixture["root"]) / "compatibility-review.json"
    review_raw = canonical_bytes(review)
    review_path.write_bytes(review_raw)
    item = {
        "workflow_id": wid,
        "project_id": PROJECT_ID,
        "intent": intent,
        "expected_version": workflow["version"],
        "source_graph_sha256": source_graph_sha256,
        "source_metadata_sha256": source_metadata_sha256,
        "links": [
            {"project_id": PROJECT_ID, "workflow_id": wid, "is_default": True}
        ],
        "default": wid,
        "anchors": [{"id": "node-terminal", "kind": "terminal"}],
        "compatibility_snapshot": compatibility_snapshot,
        "runtime_refs": runtime_refs,
        "compatibility_review_ref": {
            "path": str(review_path),
            "sha256": hashlib.sha256(review_raw).hexdigest(),
        },
        "source_admission": source_admission,
    }
    if intent in {"graph-only", "graph-and-metadata"}:
        item["target_graph"] = target_graph
    if intent in {"metadata-only", "graph-and-metadata"}:
        item["target_metadata"] = target_metadata
    value = {
        "schema": "canonical-workflow-reconcile-plan-v2",
        "state_dir": str(
            Path(fixture["root"]) / f"canonical-state-{name}"
        ),
        "project_root": str(fixture["root"]),
        "kent": {
            "path": str(fixture["kent"]),
            "sha256": hashlib.sha256(fixture["kent"].read_bytes()).hexdigest(),
        },
        "workflows": [item],
    }
    path = Path(fixture["root"]) / name
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    plan = load_plan(
        path,
        schema="canonical-workflow-reconcile-plan-v2",
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    return plan, value


def write_canonical_v2_plan(
    fixture: dict,
    value: dict,
    *,
    name: str = "canonical-plan-v2.json",
) -> operations.LoadedPlan:
    path = Path(fixture["root"]) / name
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return load_plan(
        path,
        schema="canonical-workflow-reconcile-plan-v2",
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )


def make_native_persistence(
    fixture: dict,
    *,
    production_shape: bool = False,
    with_approvals: bool = False,
    unknown_backlog: bool = False,
    with_questions: bool = False,
) -> tuple[Path, Path, Path, sqlite3.Connection, list[dict], list[dict], dict]:
    """Create a disposable, WAL-backed persistence DB matching native reader queries."""
    root = Path(fixture["root"])
    home = root / "test-home"
    persistence = home / ".kent"
    database_dir = persistence / "db"
    sessions_root = persistence / "projects" / PROJECT_ID / "sessions"
    database_dir.mkdir(parents=True)
    sessions_root.mkdir(parents=True)

    state = read_state(fixture)
    workflow = state["workflows"][workflow_id(0)]
    question_input = {
        "question": "Choose a native test route",
        "suggestions": ["Continue", "Pause"],
        "recommended_option_index": 2,
    }
    if production_shape:
        tasks: list[dict] = []
        details: dict[str, dict] = {}
        task_sessions: dict[str, list[dict]] = {}
        next_session = 1
        detailed_counts = [10, 10, 10, 10, 10, 10, 7]
        terminal_counts = [37] * 15 + [36] * 2
        for index in range(30):
            task_id = f"TASK-{index + 1}"
            terminal = index >= 13
            status = {"kind": "done", "native_state": "terminal"} if terminal else (
                {
                    "kind": "waiting_approval",
                    "native_state": "waiting_approval",
                    "attention_types": ["approval"],
                }
                if with_approvals and index < 4
                else {"kind": "doing", "native_state": "waiting"}
                if unknown_backlog and index == 0
                else
                {
                    "kind": "waiting_ask",
                    "native_state": "waiting_ask",
                    "attention_types": ["question"],
                }
                if index == 0 or (with_questions and index in (4, 5))
                else {"kind": "doing", "native_state": "waiting"}
            )
            node_id = "node-terminal" if terminal else "node-backlog"
            count = (
                detailed_counts[index]
                if index < len(detailed_counts)
                else terminal_counts[index - 13]
                if terminal
                else 0
            )
            rows = []
            for _ in range(count):
                session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"native-session-{next_session}"))
                next_session += 1
                rows.append({"session_id": session_id, "status": "idle"})
            tasks.append({"task_id": task_id, "status": status})
            current_node = {"node_id": node_id}
            if with_approvals and index < 4:
                current_node["transition_branch_key"] = "backlog_plan"
            elif (index == 0 or (with_questions and index in (4, 5))) and rows and not unknown_backlog:
                current_node["session_id"] = rows[0]["session_id"]
            details[task_id] = {
                "id": task_id,
                "summary": {"id": task_id, "done": terminal, "status": status},
                "status": status,
                "current_nodes": [current_node],
                "terminal_anchors": [{"node_id": node_id}],
                "pending_approvals": [],
                "live_sessions": [],
                "retained_session_count": count,
                "current_scripts": [],
            }
            if unknown_backlog and index == 0:
                details[task_id].pop("pending_approvals")
            task_sessions[task_id] = rows
        workflow["tasks"] = tasks
        workflow["details"] = details
        workflow["sessions"] = task_sessions
        write_state(fixture, state)
    connection = sqlite3.connect(database_dir / "main.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            primary_workspace_id TEXT,
            default_project_workflow_link_id TEXT
        );
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            canonical_root_path TEXT NOT NULL
        );
        CREATE TABLE workflows (
            id BLOB PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            version INTEGER NOT NULL,
            execution_target_policy TEXT NOT NULL,
            execution_target_custom_ref TEXT
        );
        CREATE TABLE project_workflow_links (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            workflow_id BLOB NOT NULL
        );
        CREATE TABLE task_records (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            workflow_id BLOB NOT NULL,
            project_workflow_link_id TEXT NOT NULL,
            workflow_revision_seen INTEGER NOT NULL,
            source_workspace_id TEXT,
            managed_worktree_id TEXT,
            execution_target_mode TEXT,
            execution_target_requested_ref TEXT,
            execution_target_resolved_ref TEXT,
            execution_target_commit_oid TEXT,
            execution_target_provenance TEXT,
            updated_at_unix_ms INTEGER
        );
        CREATE TABLE worktrees (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            canonical_root_path TEXT NOT NULL,
            creation_base_commit_oid TEXT
        );
        CREATE TABLE task_current_nodes (
            task_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            transition_branch_key TEXT,
            current_input_values_json TEXT,
            prior_node_values_json TEXT,
            session_id TEXT,
            scheduling_state TEXT,
            interruption_reason TEXT,
            interruption_detail_json TEXT,
            interrupted_at_unix_ms INTEGER,
            entered_by_edge_id TEXT,
            effective_assignee TEXT,
            effective_thinking TEXT,
            assignee_origin TEXT
        );
        CREATE TABLE task_pending_approvals (
            id TEXT PRIMARY KEY,
            source_task_id TEXT NOT NULL,
            source_node_id TEXT NOT NULL,
            source_transition_branch_key TEXT,
            source_session_id TEXT,
            workflow_version INTEGER NOT NULL,
            transition_snapshot_json TEXT NOT NULL,
            materialized_values_json TEXT NOT NULL,
            created_at_unix_ms INTEGER NOT NULL
        );
        CREATE TABLE task_pending_approval_branches (
            approval_id TEXT NOT NULL,
            transition_branch_key TEXT NOT NULL,
            target_snapshot_json TEXT NOT NULL,
            effective_edge_configuration_json TEXT NOT NULL,
            context_source_resolution_json TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            workspace_id TEXT,
            worktree_id TEXT,
            artifact_relpath TEXT NOT NULL,
            continuation_json TEXT NOT NULL,
            locked_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            previous_session_id TEXT,
            parent_agent_session_id TEXT,
            task_id TEXT,
            completed_compaction_count INTEGER,
            last_sequence INTEGER NOT NULL
        );
        CREATE TABLE session_workflow_node_associations (
            session_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            transition_branch_key TEXT,
            associated_at_unix_ms INTEGER NOT NULL
        );
        """
    )
    wid = workflow_id(0)
    wid_bytes = bytes.fromhex(wid.replace("-", ""))
    connection.execute(
        "INSERT INTO projects VALUES (?, ?, ?)",
        (PROJECT_ID, "workspace-1", "link-1"),
    )
    connection.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?)",
        ("workspace-1", PROJECT_ID, str(root)),
    )
    connection.execute(
        "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?)",
        (
            wid_bytes,
            workflow["metadata"]["name"],
            workflow["metadata"]["description"],
            workflow["version"],
            workflow["metadata"]["execution_target"],
            None,
        ),
    )
    connection.execute(
        "INSERT INTO project_workflow_links VALUES (?, ?, ?)",
        ("link-1", PROJECT_ID, wid_bytes),
    )
    for task in workflow["tasks"]:
        task_id = task["task_id"]
        detail = workflow["details"][task_id]
        connection.execute(
            "INSERT INTO task_records (id,project_id,workflow_id,project_workflow_link_id,"
            "workflow_revision_seen) VALUES (?, ?, ?, ?, ?)",
            (task_id, PROJECT_ID, wid_bytes, "link-1", workflow["version"]),
        )
        current = detail["current_nodes"][0]
        connection.execute(
            """
            INSERT INTO task_current_nodes VALUES (
                ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
            )
            """,
            (
                task_id,
                current["node_id"],
                current.get("transition_branch_key"),
                current.get("session_id"),
            ),
        )
    if with_approvals:
        graph = workflow["graph"]
        transition_group = next(
            row for row in graph["transition_groups"]
            if row["transition_id"] == "backlog_plan"
        )
        edge = next(row for row in graph["edges"] if row["key"] == "backlog_plan")
        for index in range(4):
            task_id = f"TASK-{index + 1}"
            approval_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"native-approval-{index + 1}")
            )
            transition = {
                "workflow_id": workflow_id(0),
                "id": transition_group["id"],
                "source_node_id": "node-backlog",
                "transition_id": "backlog_plan",
                "commentary": f"Reviewed native approval {index + 1}",
            }
            connection.execute(
                "INSERT INTO task_pending_approvals VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    approval_id,
                    task_id,
                    "node-backlog",
                    "backlog_plan",
                    workflow["version"],
                    canonical_bytes(transition).decode("utf-8"),
                    canonical_bytes({"approval_index": index + 1}).decode("utf-8"),
                    index + 1,
                ),
            )
            target = {
                "node_id": "node-plan",
                "node_kind": "agent",
                "entered_by_edge_id": edge["id"],
            }
            effective_edge = {**edge, "workflow_id": workflow_id(0)}
            context = {
                "active_source": {"kind": "exact", "session_id": None},
                "target_session": {"kind": "reuse", "session_id": None},
            }
            connection.execute(
                "INSERT INTO task_pending_approval_branches VALUES (?, ?, ?, ?, ?)",
                (
                    approval_id,
                    "backlog_plan",
                    canonical_bytes(target).decode("utf-8"),
                    canonical_bytes(effective_edge).decode("utf-8"),
                    canonical_bytes(context).decode("utf-8"),
                ),
            )
    session_rows: list[dict] = []
    detailed_task_ids = {
        task["task_id"]
        for task in workflow["tasks"]
        if task["status"].get("native_state") != "terminal"
    }
    for task_id, rows in workflow["sessions"].items():
        for row in rows:
            session_id = row["session_id"]
            session_path = sessions_root / session_id
            session_path.mkdir(parents=True)
            if task_id in detailed_task_ids:
                event = (
                    {
                        "seq": 1,
                        "kind": "message",
                        "payload": {
                            "role": "assistant",
                            "tool_calls": [{
                                "name": "ask_question",
                                "call_id": "call-native-question-1",
                                "input": question_input,
                            }],
                        },
                    }
                    if (task_id == "TASK-1" and not with_approvals and not unknown_backlog)
                    or (with_questions and task_id in ("TASK-5", "TASK-6"))
                    else {
                        "seq": 1,
                        "kind": "message",
                        "payload": {"role": "assistant", "content": "retained"},
                    }
                )
                (session_path / "events.jsonl").write_bytes(
                    canonical_bytes({"contract": "kent.session.events", "version": 2})
                    + b"\n"
                    + canonical_bytes(event)
                    + b"\n"
                )
            connection.execute(
                """
                INSERT INTO sessions VALUES (
                    ?, ?, NULL, NULL, ?, '{}', '{"locked":true}', '{}',
                    NULL, NULL, ?, 0, 1
                )
                """,
                (
                    session_id,
                    PROJECT_ID,
                    f"projects/{PROJECT_ID}/sessions/{session_id}",
                    task_id,
                ),
            )
            current_node = workflow["details"][task_id]["current_nodes"][0]["node_id"]
            connection.execute(
                "INSERT INTO session_workflow_node_associations VALUES (?, ?, NULL, ?)",
                (session_id, current_node, len(session_rows) + 1),
            )
            session_rows.append({"id": session_id, "task_id": task_id})
    connection.commit()
    database = database_dir / "main.sqlite3"
    return (
        home,
        database,
        sessions_root,
        connection,
        session_rows,
        workflow["tasks"],
        question_input,
    )


@contextmanager
def native_observer_patches(
    context: dict,
    *,
    process_side_effect: list[dict] | None = None,
):
    original_run = operations._run
    question_input = context["question_input"]
    rendered = [question_input["question"], "Suggestions:"]
    for index, suggestion in enumerate(question_input["suggestions"], start=1):
        line = f"{index}. {suggestion}"
        if question_input["recommended_option_index"] == index:
            line += " (recommended)"
        rendered.append(line)
    question_output = ("\n".join(rendered) + "\n").encode("utf-8")

    def run(command, **kwargs):
        if len(command) > 1 and command[1] == "question":
            if (
                len(command) != 6 or command[2] != "--session"
                or command[3] not in context.get("question_session_ids", [context["question_session_id"]])
                or list(command[4:]) != ["--project", PROJECT_ID]
            ):
                return 1, b"", b"unexpected question query"
            return 0, question_output, b""
        return original_run(command, **kwargs)

    process_patch = (
        mock.patch.object(
            operations,
            "_native_process_binding",
            side_effect=process_side_effect,
        )
        if process_side_effect is not None
        else mock.patch.object(
            operations,
            "_native_process_binding",
            return_value=context["process_proof"],
        )
    )
    with mock.patch.dict(os.environ, {"HOME": str(context["home"])}), process_patch, mock.patch.object(
        operations,
        "_run",
        side_effect=run,
    ):
        yield


def native_canonical_v2_plan(
    fixture: dict,
    testcase: unittest.TestCase,
    *,
    production_shape: bool = False,
    with_approvals: bool = False,
    unknown_backlog: bool = False,
    intent: str = "graph-only",
    synchronize_effects: bool = False,
    with_questions: bool = False,
    with_bindings: bool = False,
) -> tuple[operations.LoadedPlan, dict, dict]:
    if synchronize_effects:
        # Install before canonical_v2_plan binds the executable digest. Only
        # effect-owned Workflow fields are mirrored; frozen decisions stay put.
        kent = Path(fixture["kent"])
        source = kent.read_text()
        marker = '    STATE_PATH.write_text(json.dumps(state, sort_keys=True))'
        database = Path(fixture["root"]) / "test-home/.kent/db/main.sqlite3"
        replacement = marker + f'''
    if args[:3] == ["workflow", "graph", "apply"] or args[:2] == ["workflow", "update"]:
        with sqlite3.connect({str(database)!r}) as connection:
            for wid, workflow in state["workflows"].items():
                metadata = workflow["metadata"]
                target = execution_target_policy(metadata["execution_target"])
                connection.execute(
                    "UPDATE workflows SET version=?,name=?,description=?,"
                    "execution_target_policy=?,execution_target_custom_ref=? WHERE id=?",
                    (workflow["version"], metadata["name"], metadata["description"],
                     target["mode"], target.get("custom_ref"), bytes.fromhex(wid.replace("-", ""))),
                )
'''
        if source.count(marker) != 1:
            raise AssertionError("fake Kent save hook is not unique")
        source = source.replace(marker, replacement, 1)
        metadata_marker = '    state["last_update_argv"] = args'
        if source.count(metadata_marker) != 1:
            raise AssertionError("fake Kent metadata hook is not unique")
        # Native workflow update saves metadata through SaveWorkflowGraph,
        # which increments the same Workflow revision as graph apply.
        source = source.replace(metadata_marker, '    workflow["version"] += 1\n' + metadata_marker, 1)
        graph_marker = '        workflow["graph"] = document["graph"]'
        if source.count(graph_marker) != 1:
            raise AssertionError("fake Kent graph-save hook is not unique")
        whitespace = (
            "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
            "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
        )
        source = source.replace(graph_marker, graph_marker + f'''
        for edge in workflow["graph"]["edges"]:
            if isinstance(edge.get("prompt_template"), str):
                edge["prompt_template"] = edge["prompt_template"].strip({whitespace!r})
''', 1)
        kent.write_text(source)
    plan, value = canonical_v2_plan(fixture, intent=intent)
    home, database, sessions_root, connection, sessions, _, question_input = make_native_persistence(
        fixture,
        production_shape=production_shape,
        with_approvals=with_approvals,
        unknown_backlog=unknown_backlog,
        with_questions=with_questions,
    )
    testcase.addCleanup(connection.close)
    descriptor = {
        "schema": operations.NATIVE_EVIDENCE_SCHEMA,
        "database_path": str(database),
        "database_schema_sha256": operations._native_schema_fingerprint(connection),
        "session_roots": [str(sessions_root)],
    }
    parsed = operations._validate_canonical_v2_plan(plan)
    parsed["native_evidence"] = descriptor
    process_proof = {
        "pid": 12345,
        "uid": os.geteuid(),
        "start": 67890,
        "argv": [str(fixture["kent"]), "serve", "--persistence-root", str(home / ".kent")],
        "executable": [1, 2],
        "database": [3, 4],
    }
    context = {
        "home": home,
        "database": database,
        "sessions_root": sessions_root,
        "connection": connection,
        "process_proof": process_proof,
        "question_input": question_input,
        "question_session_id": next(
            (row["id"] for row in sessions if row["task_id"] == "TASK-1"),
            "SESSION-1",
        ),
    }
    if with_questions:
        context["question_session_ids"] = [
            row["id"] for row in sessions if row["task_id"] in ("TASK-5", "TASK-6")
        ]
    if with_bindings:
        connection.executemany(
            "INSERT INTO workspaces VALUES (?, ?, ?)",
            [
                ("workspace-task", PROJECT_ID, str(Path(fixture["root"]) / "historical/task-source")),
                ("workspace-session", PROJECT_ID, str(Path(fixture["root"]) / "historical/session-source")),
            ],
        )
        connection.executemany(
            "INSERT INTO worktrees VALUES (?, ?, ?, ?)",
            [
                ("worktree-task", "workspace-task", str(Path(fixture["root"]) / "historical/task-tree"), "c" * 40),
                ("worktree-session", "workspace-session", str(Path(fixture["root"]) / "historical/session-tree"), "d" * 40),
            ],
        )
        connection.execute(
            "UPDATE task_records SET source_workspace_id='workspace-task',"
            "managed_worktree_id='worktree-task',execution_target_mode='custom_ref',"
            "execution_target_requested_ref='ref:published',execution_target_resolved_ref='refs/heads/published',"
            "execution_target_commit_oid=?,execution_target_provenance='explicit' WHERE id IN ('TASK-1','TASK-30')",
            ("e" * 40,),
        )
        connection.execute(
            "UPDATE sessions SET workspace_id='workspace-session',worktree_id='worktree-session' WHERE id=?",
            (sessions[0]["id"],),
        )
        connection.commit()
    with native_observer_patches(context):
        observed = operations._canonical_v2_read(parsed, parsed["workflows"][0])

    item = value["workflows"][0]
    snapshot = item["compatibility_snapshot"]
    history_refs = snapshot["tasks"][0]["history_refs"]
    snapshot["tasks"] = [
        {
            **row,
            "history_classification": "reviewed-compatible",
            "history_refs": history_refs,
        }
        for row in observed["compatibility_snapshot"]["tasks"]
    ]
    snapshot["sessions"] = [
        {
            **row,
            "source_refs": [{
                "path": str(Path(fixture["root"]) / "absent-local-cache" / row["id"]),
                "sha256": "0" * 64,
            }],
        }
        for row in observed["compatibility_snapshot"]["sessions"]
    ]
    snapshot["native_components"] = observed["compatibility_snapshot"]["native_components"]
    value["native_evidence"] = descriptor
    review_path = Path(item["compatibility_review_ref"]["path"])
    review = json.loads(review_path.read_text())
    review["compatibility_snapshot_sha256"] = canonical_sha256(snapshot)
    review["native_evidence_sha256"] = canonical_sha256(descriptor)
    dispositions = {row["path"] for row in review["dispositions"]}
    evidence = history_refs[0]
    for row in snapshot["tasks"]:
        path = f"task:{row['id']}"
        if path not in dispositions:
            review["dispositions"].append({
                "path": path,
                "disposition": "compatible",
                "evidence": [evidence],
            })
    for row in snapshot["sessions"]:
        path = f"session:{row['id']}"
        if path not in dispositions:
            review["dispositions"].append({
                "path": path,
                "disposition": "compatible",
                "evidence": [evidence],
            })
    review_raw = canonical_bytes(review)
    review_path.write_bytes(review_raw)
    item["compatibility_review_ref"]["sha256"] = hashlib.sha256(review_raw).hexdigest()
    native_plan = write_canonical_v2_plan(fixture, value)
    context["review_path"] = review_path
    return native_plan, context, observed


def install_v2_unresolved_graph_postimage(
    fixture: dict,
    plan: operations.LoadedPlan,
) -> dict:
    parsed = operations._validate_canonical_v2_plan(plan)
    item = parsed["workflows"][0]
    item["graph"] = item["target_graph"]
    if "target_metadata" in item:
        item["metadata"] = item["target_metadata"]
    with OperationJournal(
        Path(plan.value["state_dir"]),
        "canonical-workflow-reconcile",
        plan,
    ) as journal:
        prepared = journal.state["preimage"][0]
        graph_stage = next(
            stage
            for stage in operations._canonical_v2_progress(parsed, item, prepared)
            if stage["name"] == "graph"
        )
        identity = operations._effect_inputs(
            graph_stage["command"],
            Path(fixture["root"]),
            None,
            graph_stage["stdin"],
            canonical_sha256(operations._canonical_v2_owned(graph_stage["before"])),
            canonical_sha256(operations._canonical_v2_owned(graph_stage["after"])),
        )[4]
        journal.persist({
            **journal.state,
            "phase": "in_progress",
            "effects": {
                f"apply:{graph_stage['before']['workflow_id']}:graph": {
                    **identity,
                    "status": "unresolved",
                    "attempt": 1,
                    "child": None,
                },
            },
        })
    state = read_state(fixture)
    workflow = state["workflows"][graph_stage["before"]["workflow_id"]]
    workflow["version"] = graph_stage["after"]["version"]
    workflow["graph"] = {
        key: graph_stage["after"]["graph"][key]
        for key in ("nodes", "edges", "node_groups", "transition_groups")
    }
    write_state(fixture, state)
    return graph_stage


def install_effect(
    fixture: dict,
    plan: operations.LoadedPlan,
    *,
    member_status: str,
    effect_status: str,
    stage_name: str = "graph",
) -> dict:
    parsed = operations._validate_canonical_plan(plan)
    wid = workflow_id(0)
    with OperationJournal(
        fixture["root"] / "canonical-state",
        "canonical-workflow-reconcile",
        plan,
    ) as journal:
        prepared = journal.state["preimage"][0]
        stage = next(
            stage
            for stage in operations._canonical_progress(
                parsed,
                parsed["workflows"][0],
                prepared,
            )
            if stage["name"] == stage_name
        )
        identity = operations._effect_inputs(
            stage["command"],
            fixture["root"],
            None,
            stage["stdin"],
            canonical_sha256(stage["before"]),
            canonical_sha256(stage["after"]),
        )[4]
        journal.persist(
            {
                **journal.state,
                "phase": "in_progress",
                "members": [{"workflow_id": wid, "status": member_status}],
                "effects": {
                    f"apply:{wid}:{stage_name}": {
                        **identity,
                        "status": effect_status,
                        "attempt": 1,
                        "child": None,
                    }
                },
            }
        )
        return stage


class CanonicalWorkflowLifecycleTest(unittest.TestCase):
    def test_prepare_preflights_later_stage_before_persist_or_child(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self, intent="graph-and-metadata")
            real_progress = operations._canonical_v2_progress
            def invalid_later_stage(*args):
                stages = real_progress(*args)
                self.assertGreaterEqual(len(stages), 2)
                stages[-1] = {**stages[-1], "command": ["/not-an-executable"]}
                return stages
            with native_observer_patches(context), mock.patch.object(
                operations, "_canonical_v2_progress", side_effect=invalid_later_stage
            ), mock.patch.object(OperationJournal, "persist") as persist:
                with self.assertRaises(PlanValidationError):
                    reconcile_canonical_workflows(plan, mode="prepare")
                persist.assert_not_called()
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_prepare_rejects_later_stage_exec_vector_before_persist(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self, intent="graph-and-metadata")
            real_progress = operations._canonical_v2_progress
            def invalid_vector(*args):
                stages = real_progress(*args)
                self.assertGreaterEqual(len(stages), 2)
                stages[-1] = {**stages[-1], "command": [
                    *stages[-1]["command"], "x" * operations.EXEC_ENTRY_LIMIT]}
                return stages
            with native_observer_patches(context), mock.patch.object(
                operations, "_canonical_v2_progress", side_effect=invalid_vector
            ), mock.patch.object(OperationJournal, "persist") as persist:
                with self.assertRaisesRegex(PlanValidationError, "process-exec entry limit"):
                    reconcile_canonical_workflows(plan, mode="prepare")
                persist.assert_not_called()
            self.assertEqual(graph_apply_rows(fixture), [])

    def setUp(self) -> None:
        self._source_admission_patch = mock.patch.object(
            operations,
            "admit_source_admission",
            side_effect=synthetic_verified_source_admission,
        )
        self._source_admission_patch.start()
        self.addCleanup(self._source_admission_patch.stop)

    def test_verified_source_admission_binds_native_install_and_returns_immutable_inventory(self) -> None:
        wid = workflow_id(0)
        fixture, admitted = create_verified_source_fixture(self, PROJECT_ID, wid)
        self.assertEqual(admitted.project_id, PROJECT_ID)
        self.assertEqual(admitted.workflow_id, wid)
        self.assertEqual(admitted.primary_workspace_id, "workspace-test")
        self.assertEqual(admitted.receipt_sha256, fixture.digest)
        descriptors = admitted.receipt_files
        inspector = next(
            row for row in descriptors
            if row["root"] == str(fixture.kit)
            and row["relative_path"] == "scripts/inspect-delivery-source"
        )
        self.assertTrue(inspector["mode"] & 0o111)
        inspector["sha256"] = "0" * 64
        self.assertNotEqual(
            next(
                row for row in admitted.receipt_files
                if row["root"] == str(fixture.kit)
                and row["relative_path"] == "scripts/inspect-delivery-source"
            )["sha256"],
            "0" * 64,
        )

    def test_future_task_checkout_at_published_pin_passes_actual_source_inspector(self) -> None:
        fixture, admitted = create_verified_source_fixture(self, PROJECT_ID, workflow_id(0))
        checkout = fixture.base / "future-task-checkout"
        future_pin = fixture.receipt["source_commit"]
        fixture.git(fixture.project, "worktree", "add", "-q", "--detach", str(checkout), future_pin)
        self.assertEqual(fixture.git(checkout, "rev-parse", "HEAD").strip(), future_pin)
        result = subprocess.run(
            [
                sys.executable, str(fixture.kit / "scripts/inspect-delivery-source"),
                "--receipt", str(fixture.receipt_path), "--receipt-sha256", fixture.digest,
                "--project-id", PROJECT_ID, "--workflow-id", workflow_id(0),
                "--project-root", str(fixture.project), "--kit-root", str(fixture.kit),
                "--primary-root", str(fixture.project), "--workspace", str(checkout),
                "--source-ref", admitted.value["source_ref"], "--kit-ref", admitted.value["kit_ref"],
                "--ci-helper", str(fixture.ci_helper),
                "--installed-helper", str(fixture.adapter_link), str(fixture.adapter_target),
            ],
            cwd=checkout, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        inspection = json.loads(result.stdout)
        self.assertEqual(inspection["status"], "admitted")
        self.assertEqual(inspection["source_commit"], future_pin)
        self.assertEqual(inspection["workspace_path"], str(checkout))

    def test_v2_private_source_admission_uses_real_cli_and_combined_count(self):
        fixture, admitted = create_verified_source_fixture(
            self, PROJECT_ID, workflow_id(0), local_instruction=True)
        self.assertEqual(admitted.inspection["file_count"],
                         len(fixture.receipt["files"]) + 1)
        self.assertFalse(any(row["relative_path"].startswith(".kent/skills/private/")
                             for row in admitted.receipt_files))
        # A pinned but inconsistent inspector result must not become admission.
        source = fixture.kit / "scripts/inspect-delivery-source"
        raw = source.read_text()
        changed = raw.replace('"file_count": len(files) + len(local_inputs)',
                              '"file_count": len(files)')
        self.assertNotEqual(raw, changed)
        source.write_text(changed)
        fixture.repin_native_sources()
        fixture.receipt["project_id"] = PROJECT_ID
        fixture.receipt["workflow_id"] = workflow_id(0)
        fixture.pin()
        fixture.installation_binding["receipt_sha256"] = fixture.digest
        fixture.write_installation_binding()
        admission = dict(fixture.source_admission)
        admission["receipt_ref"] = {"path": str(fixture.receipt_path), "sha256": fixture.digest}
        admission["installation_binding_sha256"] = hashlib.sha256(
            fixture.binding_path.read_bytes()).hexdigest()
        with mock.patch.object(operations.Path, "home", return_value=fixture.home), \
                mock.patch.dict("os.environ", {"HOME": str(fixture.home)}):
            with self.assertRaisesRegex(PlanValidationError, "result differs"):
                REAL_ADMIT_SOURCE_ADMISSION(
                    admission, project_id=PROJECT_ID, workflow_id=workflow_id(0),
                    project_root=fixture.project, kent_path=fixture.native_bin)

    def test_operations_v2_rejects_closed_local_metadata_before_inspection(self):
        fixture, admitted = create_verified_source_fixture(
            self, PROJECT_ID, workflow_id(0), local_instruction=True)
        original = dict(fixture.receipt["local_inputs"][0])
        for change in ({"kind": "helper"}, {"extra": True}, {"mode": 0o700},
                       {"root": str(fixture.base)},
                       {"relative_path": ".kent/skills/private/../instruction.md"},
                       {"relative_path": ".kent/skills/private/*.md"},
                       {"byte_size": True}, {"sha256": "invalid"}):
            with self.subTest(change=change):
                fixture.receipt["local_inputs"] = [original | change]
                fixture.pin()
                fixture.installation_binding["receipt_sha256"] = fixture.digest
                fixture.write_installation_binding()
                admission = admitted.value
                admission["receipt_ref"]["sha256"] = fixture.digest
                admission["installation_binding_sha256"] = hashlib.sha256(
                    fixture.binding_path.read_bytes()).hexdigest()
                with mock.patch.object(operations.Path, "home", return_value=fixture.home), \
                        mock.patch.dict("os.environ", {"HOME": str(fixture.home)}):
                    with self.assertRaisesRegex(PlanValidationError, "source publication"):
                        REAL_ADMIT_SOURCE_ADMISSION(
                            admission, project_id=PROJECT_ID, workflow_id=workflow_id(0),
                            project_root=fixture.project, kent_path=fixture.native_bin)

    def test_source_admission_rejects_duplicate_native_task_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with mock.patch.object(
                operations,
                "_project_rows",
                return_value=[{"id": PROJECT_ID, "path": str(root)}],
            ), mock.patch.object(
                operations,
                "_run",
                return_value=(0, PROJECT_ID.encode(), b""),
            ), mock.patch.object(
                operations,
                "_kent_pages",
                return_value=[{"tasks": [{"task_id": "duplicate"}, {"task_id": "duplicate"}]}],
            ):
                with self.assertRaisesRegex(
                    PlanValidationError,
                    "native source-admission Task inventory contains duplicate identities",
                ):
                    operations._source_native_identity(
                        project_id=PROJECT_ID,
                        project_root=root,
                        primary_root=root,
                        kent_path=root / "kent",
                    )

    def test_source_native_identity_normalizes_project_paths_and_stops_at_primary_task(self) -> None:
        def task_detail(
            task_id: str,
            *,
            root: Path,
            workspace_id: str,
            is_primary: bool,
            default_workspace_id: str = "workspace-primary",
        ) -> dict:
            return {
                "summary": {"id": task_id, "project_id": PROJECT_ID},
                "source_workspace": {
                    "root_path": str(root),
                    "is_primary": is_primary,
                    "workspace_id": workspace_id,
                },
                "project": {"default_workspace_id": default_workspace_id},
            }

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            root = home / "primary"
            secondary_root = home / "secondary"
            root.mkdir(parents=True)
            details = {
                "task-00-secondary": task_detail(
                    "task-00-secondary",
                    root=secondary_root,
                    workspace_id="workspace-secondary",
                    is_primary=False,
                ),
                "task-01-primary": task_detail(
                    "task-01-primary",
                    root=root,
                    workspace_id="workspace-primary",
                    is_primary=True,
                ),
            }
            task_rows = [
                {"task_id": "task-55335-corrupt-later"},
                {"task_id": "task-01-primary"},
                {"task_id": "task-00-secondary"},
            ]
            for native_path in ("./", "~/primary", str(root)):
                with self.subTest(native_path=native_path):
                    detail_calls: list[str] = []

                    def read_detail(
                        _kent: Path,
                        args: list[str],
                        *,
                        cwd: Path,
                    ) -> dict:
                        self.assertEqual(cwd, root)
                        task_id = args[2]
                        detail_calls.append(task_id)
                        if task_id == "task-55335-corrupt-later":
                            raise AssertionError("read unrelated malformed Task detail")
                        return details[task_id]

                    with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                            mock.patch.object(
                                operations,
                                "_project_rows",
                                return_value=[{
                                    "id": PROJECT_ID,
                                    "name": "fixture",
                                    "path": native_path,
                                }],
                            ), mock.patch.object(
                                operations,
                                "_run",
                                return_value=(0, PROJECT_ID.encode(), b""),
                            ), mock.patch.object(
                                operations,
                                "_kent_pages",
                                return_value=[{"tasks": task_rows}],
                            ), mock.patch.object(
                                operations,
                                "_kent_json",
                                side_effect=read_detail,
                            ):
                        workspace_id = operations._source_native_identity(
                            project_id=PROJECT_ID,
                            project_root=root,
                            primary_root=root,
                            kent_path=root / "kent",
                        )

                    self.assertEqual(workspace_id, "workspace-primary")
                    self.assertEqual(detail_calls, ["task-00-secondary", "task-01-primary"])

    def test_source_native_identity_fails_closed_on_selected_task_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with mock.patch.object(
                operations,
                "_project_rows",
                return_value=[{"id": PROJECT_ID, "name": "fixture", "path": str(root)}],
            ), mock.patch.object(
                operations,
                "_run",
                return_value=(0, PROJECT_ID.encode(), b""),
            ), mock.patch.object(
                operations,
                "_kent_pages",
                return_value=[{"tasks": [
                    {"task_id": "task-00-selected"},
                    {"task_id": "task-01-later"},
                ]}],
            ), mock.patch.object(
                operations,
                "_kent_json",
                side_effect=operations.OperationError("selected native Task read failed"),
            ) as read_detail:
                with self.assertRaisesRegex(
                    operations.OperationError,
                    "selected native Task read failed",
                ):
                    operations._source_native_identity(
                        project_id=PROJECT_ID,
                        project_root=root,
                        primary_root=root,
                        kent_path=root / "kent",
                    )

            self.assertEqual(read_detail.call_count, 1)
            self.assertEqual(read_detail.call_args.args[1][2], "task-00-selected")

    def test_source_native_identity_rejects_inconsistent_primary_task_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve()
            root = temporary_root / "primary"
            other_root = temporary_root / "other"
            root.mkdir()
            other_root.mkdir()
            primary_detail = {
                "summary": {"id": "task-primary", "project_id": PROJECT_ID},
                "source_workspace": {
                    "root_path": str(root),
                    "is_primary": True,
                    "workspace_id": "workspace-primary",
                },
                "project": {"default_workspace_id": "workspace-primary"},
            }
            cases = (
                (
                    "source root",
                    {
                        **primary_detail,
                        "source_workspace": {
                            **primary_detail["source_workspace"],
                            "root_path": str(other_root),
                        },
                    },
                ),
                (
                    "default workspace",
                    {
                        **primary_detail,
                        "project": {"default_workspace_id": "workspace-other"},
                    },
                ),
                (
                    "project identity",
                    {
                        **primary_detail,
                        "summary": {"id": "task-primary", "project_id": "project-other"},
                    },
                ),
                (
                    "primary flag",
                    {
                        **primary_detail,
                        "source_workspace": {
                            **primary_detail["source_workspace"],
                            "is_primary": "true",
                        },
                    },
                ),
                (
                    "nonprimary source root",
                    {
                        **primary_detail,
                        "source_workspace": {
                            **primary_detail["source_workspace"],
                            "root_path": str(root),
                            "is_primary": False,
                        },
                    },
                ),
                (
                    "nonprimary default workspace",
                    {
                        **primary_detail,
                        "source_workspace": {
                            **primary_detail["source_workspace"],
                            "root_path": str(other_root),
                            "is_primary": False,
                            "workspace_id": "workspace-primary",
                        },
                    },
                ),
            )
            for label, detail in cases:
                with self.subTest(label=label), \
                        mock.patch.object(
                            operations,
                            "_project_rows",
                            return_value=[{
                                "id": PROJECT_ID,
                                "name": "fixture",
                                "path": str(root),
                            }],
                        ), mock.patch.object(
                            operations,
                            "_run",
                            return_value=(0, PROJECT_ID.encode(), b""),
                        ), mock.patch.object(
                            operations,
                            "_kent_pages",
                            return_value=[{"tasks": [{"task_id": "task-primary"}]}],
                        ), mock.patch.object(
                            operations,
                            "_kent_json",
                            return_value=detail,
                        ):
                    with self.assertRaises(PlanValidationError):
                        operations._source_native_identity(
                            project_id=PROJECT_ID,
                            project_root=root,
                            primary_root=root,
                            kent_path=root / "kent",
                        )

    def test_source_native_identity_fails_when_no_primary_task_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve()
            root = temporary_root / "primary"
            secondary_root = temporary_root / "secondary"
            root.mkdir()
            detail = {
                "summary": {"id": "task-secondary", "project_id": PROJECT_ID},
                "source_workspace": {
                    "root_path": str(secondary_root),
                    "is_primary": False,
                    "workspace_id": "workspace-secondary",
                },
                "project": {"default_workspace_id": "workspace-primary"},
            }
            with mock.patch.object(
                operations,
                "_project_rows",
                return_value=[{"id": PROJECT_ID, "name": "fixture", "path": str(root)}],
            ), mock.patch.object(
                operations,
                "_run",
                return_value=(0, PROJECT_ID.encode(), b""),
            ), mock.patch.object(
                operations,
                "_kent_pages",
                return_value=[{"tasks": [{"task_id": "task-secondary"}]}],
            ), mock.patch.object(
                operations,
                "_kent_json",
                return_value=detail,
            ):
                with self.assertRaisesRegex(
                    PlanValidationError,
                    "native primary workspace identity is unavailable or ambiguous",
                ):
                    operations._source_native_identity(
                        project_id=PROJECT_ID,
                        project_root=root,
                        primary_root=root,
                        kent_path=root / "kent",
                    )

    def test_native_event_selection_is_append_stable_strict_and_bounded(self) -> None:
        def event(sequence: int, kind: str, payload: dict) -> dict:
            return {"seq": sequence, "kind": kind, "payload": payload}

        header = canonical_bytes({"contract": "kent.session.events", "version": 2}) + b"\n"
        records = [
            event(1, "message", {
                "role": "developer",
                "message_type": "environment",
                "content": "initial",
            }),
            event(2, "generation_context", {
                "before_summary": [{"role": "developer", "message_type": "environment", "content": "current"}],
                "after_summary": [{"role": "developer", "content": "untyped instruction"}],
            }),
            event(3, "configuration_updated", {
                "item": {
                    "type": "configuration_update",
                    "configuration_effort": "medium",
                    "raw": {
                        "type": "configuration_update",
                        "reasoning": {"effort": "medium"},
                    },
                },
            }),
            event(4, "history_replaced", {
                "cache_metadata": {},
                "committed_entry_start": 0,
                "compacted_output": {},
                "compaction_number": 1,
                "engine": "remote",
                "mode": "workflow_post_completion",
            }),
            event(5, "message", {"role": "assistant", "content": "unrelated append"}),
        ]
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_bytes(header + b"".join(canonical_bytes(row) + b"\n" for row in records[:4]))
            first = operations._native_scan_events(
                path,
                expected_last_sequence=4,
                aggregate_bytes=[0],
                selected_bytes=[0],
            )
            with path.open("ab") as stream:
                stream.write(canonical_bytes(records[4]) + b"\n")
            appended = operations._native_scan_events(
                path,
                expected_last_sequence=5,
                aggregate_bytes=[0],
                selected_bytes=[0],
            )
            self.assertEqual(first[0], appended[0])
            self.assertNotEqual(first[1], appended[1])
            self.assertEqual(
                first[2]["payload"]["mode"],
                "workflow_post_completion",
            )
            self.assertIn("message:environment", first[3])
            self.assertIn("configuration_updated", first[3])
            self.assertIn("history_replaced", first[3])
            self.assertLessEqual(len(first[0]), operations.NATIVE_MAX_SELECTED_EVENTS_PER_SESSION)

            malformed = Path(temporary) / "malformed.jsonl"
            malformed.write_bytes(
                header
                + b'{"seq":1,"seq":2,"kind":"message","payload":{}}\n'
            )
            with self.assertRaisesRegex(EffectBlocked, "malformed JSON"):
                operations._native_scan_events(
                    malformed,
                    expected_last_sequence=1,
                    aggregate_bytes=[0],
                    selected_bytes=[0],
                )

            with mock.patch.object(operations, "NATIVE_MAX_EVENT_FILE_BYTES", 64):
                with self.assertRaisesRegex(EffectBlocked, "event file exceeds"):
                    operations._native_scan_events(
                        path,
                        expected_last_sequence=5,
                        aggregate_bytes=[0],
                        selected_bytes=[0],
                    )
            with mock.patch.object(operations, "NATIVE_MAX_EVENT_TOTAL_BYTES", 64):
                with self.assertRaisesRegex(EffectBlocked, "event inventory exceeds"):
                    operations._native_scan_events(
                        path,
                        expected_last_sequence=5,
                        aggregate_bytes=[0],
                        selected_bytes=[0],
                    )

            oversized = Path(temporary) / "oversized.jsonl"
            oversized.write_bytes(header + canonical_bytes(event(
                1, "message", {"role": "assistant", "content": "x" * 256},
            )) + b"\n")
            with mock.patch.object(operations, "NATIVE_MAX_EVENT_LINE_BYTES", 128):
                with self.assertRaisesRegex(EffectBlocked, "record exceeds"):
                    operations._native_scan_events(
                        oversized,
                        expected_last_sequence=1,
                        aggregate_bytes=[0],
                        selected_bytes=[0],
                    )

            linked = Path(temporary) / "linked.jsonl"
            linked.symlink_to(path)
            with self.assertRaisesRegex(EffectBlocked, "symlink"):
                operations._native_scan_events(
                    linked,
                    expected_last_sequence=5,
                    aggregate_bytes=[0],
                    selected_bytes=[0],
                )

    def test_native_history_replacement_accepts_bounded_payload_encodings(self) -> None:
        latest = {}
        current_event = {
            "seq": 4,
            "kind": "history_replaced",
            "payload": {
                "committed_entry_start": 0,
                "compaction_number": 1,
                "engine": "remote",
                "items": [],
                "mode": "workflow_post_completion",
            },
        }
        operations._native_event_selector(current_event, latest)
        self.assertEqual(latest["history_replaced"][0], 4)

        legacy_list_event = {
            "seq": 5,
            "kind": "history_replaced",
            "payload": {
                "cache_metadata": [],
                "committed_entry_start": 1,
                "compacted_output": {"summary": ""},
                "compaction_number": 2,
                "engine": "remote",
                "mode": "workflow_post_completion",
            },
        }
        operations._native_event_selector(legacy_list_event, latest)
        self.assertEqual(latest["history_replaced"][0], 5)

        compacted_only_event = {
            "seq": 6,
            "kind": "history_replaced",
            "payload": {
                "committed_entry_start": 2,
                "compacted_output": {"summary": ""},
                "compaction_number": 3,
                "engine": "remote",
                "mode": "workflow_post_completion",
            },
        }
        operations._native_event_selector(compacted_only_event, latest)
        self.assertEqual(latest["history_replaced"][0], 6)

        invalid_event = {
            **current_event,
            "payload": {**legacy_list_event["payload"], "cache_metadata": "invalid"},
        }
        with self.assertRaisesRegex(EffectBlocked, "history-replacement event shape"):
            operations._native_event_selector(invalid_event, {})

    def test_native_event_selection_omits_only_transient_developer_notices(self) -> None:
        latest = {}
        for sequence, message_type in (
            (1, "background_notice"),
            (2, "compaction_soon_reminder"),
        ):
            operations._native_event_selector({
                "seq": sequence,
                "kind": "message",
                "payload": {"role": "developer", "message_type": message_type},
            }, latest)
        self.assertEqual(latest, {})

        operations._native_event_selector({
            "seq": 3,
            "kind": "message",
            "payload": {"role": "developer", "message_type": "agent_steer"},
        }, latest)
        operations._native_event_selector({
            "seq": 4,
            "kind": "message",
            "payload": {"role": "developer", "message_type": "workflow_mode"},
        }, latest)
        self.assertEqual(
            set(latest),
            {"developer:3", "developer:4", "message:workflow_mode"},
        )

    def test_native_event_scanner_streams_a_production_sized_file_over_196mb(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            path = Path(temporary) / "large-events.jsonl"
            content = "x" * (7 * 1024 * 1024)
            with path.open("wb") as stream:
                stream.write(
                    canonical_bytes({"contract": "kent.session.events", "version": 2})
                    + b"\n"
                )
                for sequence in range(1, 29):
                    event = {
                        "seq": sequence,
                        "kind": "message",
                        "payload": {"role": "assistant", "content": content},
                    }
                    line = canonical_bytes(event) + b"\n"
                    self.assertLessEqual(
                        len(line),
                        operations.NATIVE_MAX_EVENT_LINE_BYTES,
                    )
                    stream.write(line)
            self.assertGreater(path.stat().st_size, 196_000_000)
            aggregate = [0]
            selected = [0]
            events, last_event, _, _ = operations._native_scan_events(
                path,
                expected_last_sequence=28,
                aggregate_bytes=aggregate,
                selected_bytes=selected,
            )
            self.assertEqual(events, [])
            self.assertEqual(last_event["seq"], 28)
            self.assertEqual(aggregate[0], path.stat().st_size)
            self.assertEqual(selected[0], 0)

    def test_internal_readback_output_limit_is_bounded_and_graph_cap_is_opt_in(self) -> None:
        output_size = operations.MAX_OUTPUT + 64
        command = [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write('x' * {output_size})",
        ]
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            cwd = Path(temporary)
            code, default_output, stderr = operations._run(command, cwd=cwd)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, b"")
            self.assertEqual(len(default_output), operations.MAX_OUTPUT)

            code, graph_output, stderr = operations._run(
                command,
                cwd=cwd,
                output_limit=operations.CANONICAL_GRAPH_MAX_OUTPUT,
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, b"")
            self.assertEqual(len(graph_output), output_size)

        with self.assertRaisesRegex(operations.OperationError, "output bound is invalid"):
            operations._run(
                command,
                cwd=Path.home(),
                output_limit=operations.CANONICAL_GRAPH_MAX_OUTPUT + 1,
            )

    def test_native_json_rejects_nonfinite_and_native_question_input_is_closed(self) -> None:
        with self.assertRaisesRegex(EffectBlocked, "malformed JSON"):
            operations._native_json(b'{"value":1e999}', "native test")
        with self.assertRaisesRegex(EffectBlocked, "exceeds 8 MiB"):
            operations._native_json(
                " " * (operations.NATIVE_MAX_DB_JSON_BYTES + 1),
                "oversized native JSON",
            )
        valid = {
            "question": "Choose a route",
            "suggestions": ["Continue", "Stop"],
            "recommended_option_index": 1,
        }
        self.assertEqual(operations._native_question_input(valid), valid)
        with self.assertRaisesRegex(EffectBlocked, "input is malformed"):
            operations._native_question_input({**valid, "extra": True})

    def test_native_pending_question_matches_rendered_cli_output_without_answering(self) -> None:
        question = {
            "question": "Choose a route",
            "suggestions": ["Continue", "Stop"],
            "recommended_option_index": 2,
        }
        expected = (
            "Choose a route\n"
            "Suggestions:\n"
            "1. Continue\n"
            "2. Stop (recommended)\n"
        ).encode()
        kent = Path("/opt/kent")
        root = Path("/workspace")
        with mock.patch.object(
            operations,
            "_run",
            return_value=(0, expected, b""),
        ) as run:
            operations._native_question_matches_cli(
                kent,
                root,
                PROJECT_ID,
                "44444444-4444-4444-8444-444444444444",
                question,
            )
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                str(kent),
                "question",
                "--session",
                "44444444-4444-4444-8444-444444444444",
                "--project",
                PROJECT_ID,
            ],
        )
        self.assertEqual(kwargs["cwd"], root)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

        with mock.patch.object(
            operations,
            "_run",
            return_value=(0, b"changed question\n", b""),
        ):
            with self.assertRaisesRegex(EffectBlocked, "output differs"):
                operations._native_question_matches_cli(
                    kent,
                    root,
                    PROJECT_ID,
                    "44444444-4444-4444-8444-444444444444",
                    question,
                )

    def test_native_approval_fingerprint_binds_all_five_raw_json_columns(self) -> None:
        workflow_id_value = "11111111-1111-4111-8111-111111111111"
        source_node = "22222222-2222-4222-8222-222222222222"
        target_node = "33333333-3333-4333-8333-333333333333"
        session_id = "44444444-4444-4444-8444-444444444444"
        group_id = "55555555-5555-4555-8555-555555555555"
        edge_id = "66666666-6666-4666-8666-666666666666"
        transition_id = "needs_user_action"
        graph_edge = {
            "id": edge_id,
            "key": transition_id,
            "transition_group_id": group_id,
            "target_node_id": target_node,
            "assignee_selection": "configured",
            "thinking_selection": "configured",
            "requires_approval": True,
            "context_mode": "reuse",
            "context_source": {"kind": "current_session"},
            "parameters": {},
            "prompt_template": "Resume the same node.",
        }
        workflow_graph = {
            "nodes": [
                {"id": source_node, "kind": "agent"},
                {"id": target_node, "kind": "agent"},
            ],
            "edges": [graph_edge],
            "transition_groups": [{
                "id": group_id,
                "source_node_id": source_node,
                "transition_id": transition_id,
            }],
        }
        approval = {
            "id": "77777777-7777-4777-8777-777777777777",
            "source_task_id": "task-88888888-8888-4888-8888-888888888888",
            "source_node_id": source_node,
            "source_transition_branch_key": None,
            "source_session_id": session_id,
            "workflow_version": 16,
            "transition_snapshot_json": json.dumps({
                "workflow_id": workflow_id_value,
                "id": group_id,
                "source_node_id": source_node,
                "transition_id": transition_id,
                "commentary": "Preserve exact snapshot.",
            }),
            "materialized_values_json": json.dumps({"work_kind": "bugfix"}),
            "created_at_unix_ms": 17,
        }
        branch = {
            "approval_id": approval["id"],
            "transition_branch_key": transition_id,
            "target_snapshot_json": json.dumps({
                "node_id": target_node,
                "node_kind": "agent",
                "entered_by_edge_id": edge_id,
                "scheduling_state": "ready",
                "current_input_values": {},
                "prior_values": {},
            }),
            "effective_edge_configuration_json": json.dumps({
                **graph_edge,
                "workflow_id": workflow_id_value,
            }),
            "context_source_resolution_json": json.dumps({
                "active_source": {"kind": "exact", "session_id": session_id},
                "target_session": {"kind": "reuse", "session_id": session_id},
            }),
        }
        nodes = [{
            "node_id": source_node,
            "transition_branch_key": None,
            "session_id": session_id,
        }]

        def digest(current_approval: dict, current_branch: dict) -> str:
            return canonical_sha256(operations._native_approval_payload(
                current_approval,
                [current_branch],
                nodes,
                workflow_id_value,
                workflow_graph,
            ))

        baseline = digest(approval, branch)
        for key in ("prompt", "prompt_template"):
            for change in ("added", "removed", "changed"):
                with self.subTest(prompt_key=key, change=change):
                    frozen_edge = json.loads(branch["effective_edge_configuration_json"])
                    if change == "added":
                        frozen_edge.pop(key, None)
                        graph_edge[key] = "New future prompt"
                    else:
                        frozen_edge[key] = "Original frozen prompt"
                        if change == "removed":
                            graph_edge.pop(key, None)
                        else:
                            graph_edge[key] = "Changed future prompt"
                    frozen_branch = {
                        **branch,
                        "effective_edge_configuration_json": json.dumps(frozen_edge),
                    }
                    frozen_digest = digest(approval, frozen_branch)
                    # The original raw prompt remains fingerprinted, regardless
                    # of its presence or value on the current graph.
                    frozen_edge[key] = "Tampered frozen prompt"
                    self.assertNotEqual(frozen_digest, digest(approval, {
                        **frozen_branch,
                        "effective_edge_configuration_json": json.dumps(frozen_edge),
                    }))
            graph_edge.pop(key, None)
        graph_edge["prompt_template"] = "Resume the same node."
        for version in (None, True, -1, "16", 16.0):
            with self.subTest(invalid_version=version), self.assertRaisesRegex(
                EffectBlocked, "workflow version is invalid",
            ):
                digest({**approval, "workflow_version": version}, branch)
        self.assertNotEqual(digest({**approval, "workflow_version": 17}, branch), baseline)
        nullable_edge_branch = dict(branch)
        nullable_edge = json.loads(branch["effective_edge_configuration_json"])
        nullable_edge["input_bindings"] = None
        nullable_edge["output_requirements"] = None
        nullable_edge_branch["effective_edge_configuration_json"] = json.dumps(nullable_edge)
        nullable_digest = digest(approval, nullable_edge_branch)
        self.assertNotEqual(nullable_digest, baseline)
        invalid_edge_branch = dict(nullable_edge_branch)
        invalid_edge = dict(nullable_edge)
        invalid_edge["input_bindings"] = {}
        invalid_edge_branch["effective_edge_configuration_json"] = json.dumps(invalid_edge)
        with self.assertRaisesRegex(EffectBlocked, "differs from the reviewed Workflow context"):
            digest(approval, invalid_edge_branch)
        invalid_edge_branch["effective_edge_configuration_json"] = json.dumps({
            **nullable_edge,
            "unknown_configuration": None,
        })
        with self.assertRaisesRegex(EffectBlocked, "differs from the reviewed Workflow context"):
            digest(approval, invalid_edge_branch)

        for column, source in (
            ("transition_snapshot_json", approval),
            ("materialized_values_json", approval),
            ("target_snapshot_json", branch),
            ("effective_edge_configuration_json", branch),
            ("context_source_resolution_json", branch),
        ):
            changed_approval = dict(approval)
            changed_branch = dict(branch)
            raw = source[column]
            if source is approval:
                changed_approval[column] = raw.replace("{", "{ ", 1)
            else:
                changed_branch[column] = raw.replace("{", "{ ", 1)
            self.assertNotEqual(
                digest(changed_approval, changed_branch),
                baseline,
                column,
            )

        semantic_approval = dict(approval)
        semantic_transition = json.loads(approval["transition_snapshot_json"])
        semantic_transition["commentary"] = "Changed reviewed transition text."
        semantic_approval["transition_snapshot_json"] = canonical_bytes(
            semantic_transition
        ).decode("utf-8")
        self.assertNotEqual(digest(semantic_approval, branch), baseline)

        semantic_approval = dict(approval)
        semantic_approval["materialized_values_json"] = canonical_bytes({
            "work_kind": "changed-setting",
        }).decode("utf-8")
        self.assertNotEqual(digest(semantic_approval, branch), baseline)

        semantic_branch = dict(branch)
        target_snapshot = json.loads(branch["target_snapshot_json"])
        target_snapshot["current_input_values"] = {"choice": "different-target"}
        semantic_branch["target_snapshot_json"] = canonical_bytes(
            target_snapshot
        ).decode("utf-8")
        self.assertNotEqual(digest(approval, semantic_branch), baseline)

        four_approvals = []
        for index in range(4):
            approval_row = {
                **approval,
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"native-approval-{index}")),
            }
            branch_row = {
                **branch,
                "approval_id": approval_row["id"],
            }
            four_approvals.append({
                "task_id": approval_row["source_task_id"],
                "approval_id": approval_row["id"],
                "record_sha256": canonical_sha256(
                    operations._native_approval_payload(
                        approval_row,
                        [branch_row],
                        nodes,
                        workflow_id_value,
                        workflow_graph,
                    )
                ),
            })
        self.assertEqual(len(four_approvals), 4)
        self.assertEqual(len({row["record_sha256"] for row in four_approvals}), 4)

    def test_native_process_adapters_bind_executable_and_database_descriptors(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            root = Path(temporary) / "persistence"
            database_dir = root / "db"
            database_dir.mkdir(parents=True)
            database = database_dir / "main.sqlite3"
            database.write_bytes(b"db")
            kent = Path(temporary) / "kent"
            kent.write_bytes(b"kent executable")
            kent.chmod(0o755)
            pid = 19342
            proc_root = Path(temporary) / "proc"
            process = proc_root / str(pid)
            (process / "fd").mkdir(parents=True)
            (process / "exe").symlink_to(kent)
            (process / "fd" / "7").symlink_to(database)
            (process / "cmdline").write_bytes(
                os.fsencode(str(kent)) + b"\x00serve\x00--persistence-root\x00"
                + os.fsencode(str(root)) + b"\x00"
            )
            stat_fields = ["S", *("0" for _ in range(18)), "9876"]
            (process / "stat").write_text(
                f"{pid} (kent) {' '.join(stat_fields)}",
                encoding="ascii",
            )
            linux = operations._native_process_evidence(
                pid,
                kent,
                root,
                database,
                system="linux",
                proc_root=proc_root,
            )
            self.assertEqual(linux["pid"], pid)
            self.assertEqual(linux["start"], 9876)

            executable_stat = kent.stat()
            database_stat = database.stat()
            ps = (
                f"{pid} {os.geteuid()} Thu Oct 15 15:56:00 2026 "
                f"{kent} serve --persistence-root {root}\n"
            ).encode()
            lsof = (
                f"p{pid}\nu{os.geteuid()}\n"
                f"ftxt\ntREG\nD0x{executable_stat.st_dev:x}\n"
                f"i{executable_stat.st_ino}\nn{kent}\n"
                "ftxt\ntREG\nD0x1\ni999\nn/usr/lib/dyld\n"
                f"f7\ntREG\nD0x{database_stat.st_dev:x}\n"
                f"i{database_stat.st_ino}\nn{database}\n"
                f"f14\ntREG\nD0x{database_stat.st_dev:x}\n"
                f"i{database_stat.st_ino}\nn{database}\n"
            ).encode()
            with mock.patch.object(
                operations,
                "_run",
                side_effect=[(0, ps, b""), (0, lsof, b"")],
            ):
                darwin = operations._native_process_evidence(
                    pid,
                    kent,
                    root,
                    database,
                    system="darwin",
                )
            self.assertEqual(darwin["pid"], pid)
            self.assertEqual(darwin["database"], [database_stat.st_dev, database_stat.st_ino])

            copied_database = database_dir / "same-bytes.sqlite3"
            copied_database.write_bytes(database.read_bytes())
            (process / "fd" / "7").unlink()
            (process / "fd" / "7").symlink_to(copied_database)
            with self.assertRaisesRegex(EffectBlocked, "identity or descriptors differ"):
                operations._native_process_evidence(
                    pid,
                    kent,
                    root,
                    database,
                    system="linux",
                    proc_root=proc_root,
                )
            (process / "fd" / "7").unlink()
            (process / "fd" / "7").symlink_to(database)

            wrong_kent = Path(temporary) / "different-kent"
            wrong_kent.write_bytes(b"same status, different executable")
            wrong_kent.chmod(0o755)
            (process / "exe").unlink()
            (process / "exe").symlink_to(wrong_kent)
            with self.assertRaisesRegex(EffectBlocked, "identity or descriptors differ"):
                operations._native_process_evidence(
                    pid,
                    kent,
                    root,
                    database,
                    system="linux",
                    proc_root=proc_root,
                )
            (process / "exe").unlink()
            (process / "exe").symlink_to(kent)

            changed_start_fields = ["S", *("0" for _ in range(18)), "9877"]
            (process / "stat").write_text(
                f"{pid} (kent) {' '.join(changed_start_fields)}",
                encoding="ascii",
            )
            reused_pid = operations._native_process_evidence(
                pid,
                kent,
                root,
                database,
                system="linux",
                proc_root=proc_root,
            )
            self.assertNotEqual(reused_pid["start"], linux["start"])
            with self.assertRaisesRegex(EffectBlocked, "identity or descriptors differ"):
                operations._native_process_evidence(
                    pid,
                    kent,
                    root,
                    database,
                    system="linux",
                    proc_root=Path(temporary) / "missing-proc",
                )

            bad_lsof = lsof.replace(
                f"i{database_stat.st_ino}\nn{database}".encode(),
                f"i{database_stat.st_ino + 1}\nn{database}".encode(),
            )
            with mock.patch.object(
                operations,
                "_run",
                side_effect=[(0, ps, b""), (0, bad_lsof, b"")],
            ):
                with self.assertRaisesRegex(
                    EffectBlocked,
                    "executable/database descriptors differ",
                ):
                    operations._native_process_evidence(
                        pid,
                        kent,
                        root,
                        database,
                        system="darwin",
                    )

    def test_native_foreign_root_status_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            parsed = operations._validate_canonical_v2_plan(plan)
            foreign_root = context["home"] / ".kent-foreign"
            status = {
                "health_status": "ok",
                "health_pid": 999,
                "command": [
                    str(fixture["kent"]),
                    "serve",
                    "--persistence-root",
                    str(foreign_root),
                ],
            }
            with mock.patch.dict(os.environ, {"HOME": str(context["home"])}), mock.patch.object(
                operations,
                "_kent_json",
                return_value=status,
            ), mock.patch.object(operations, "_native_process_evidence") as process_evidence:
                with self.assertRaisesRegex(EffectBlocked, "does not bind"):
                    operations._native_process_binding(
                        parsed,
                        parsed["native_evidence"],
                        PROJECT_ID,
                    )
                process_evidence.assert_not_called()

    def test_native_wal_plan_prepare_and_fresh_new_effect_gate(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, observed = native_canonical_v2_plan(fixture, self)
            database = context["database"]
            db_digest = hashlib.sha256(database.read_bytes()).hexdigest()
            wal_path = Path(str(database) + "-wal")
            wal_digest = hashlib.sha256(wal_path.read_bytes()).hexdigest()
            connection = context["connection"]
            total_changes = connection.total_changes
            self.assertEqual(
                observed["compatibility_snapshot"]["native_components"]["sessions"],
                [],
            )

            with native_observer_patches(context):
                prepared = reconcile_canonical_workflows(plan, mode="prepare")
                self.assertEqual(prepared["phase"], "prepared")

                released = []

                def refuse_effect(
                    journal,
                    *,
                    current_sha256,
                    preimage_sha256,
                    pre_release_check,
                    **kwargs,
                ):
                    del journal, kwargs
                    self.assertEqual(current_sha256(), preimage_sha256)
                    pre_release_check()
                    released.append(True)
                    return "preimage"

                with mock.patch.object(
                    operations,
                    "_canonical_v2_read",
                    wraps=operations._canonical_v2_read,
                ) as full_read, mock.patch.object(
                    operations,
                    "_settle_or_run",
                    side_effect=refuse_effect,
                ) as settle:
                    applied = reconcile_canonical_workflows(
                        plan,
                        mode="apply",
                        confirm=plan.sha256,
                    )
                self.assertEqual(applied["settled"], "preimage")
                self.assertGreaterEqual(full_read.call_count, 2)
                self.assertEqual(settle.call_count, 1)
                self.assertEqual(released, [True])

            self.assertEqual(graph_apply_rows(fixture), [])
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), db_digest)
            self.assertEqual(hashlib.sha256(wal_path.read_bytes()).hexdigest(), wal_digest)
            self.assertEqual(connection.total_changes, total_changes)

    def test_native_changed_database_blocks_dispatch_and_review_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
                context["connection"].execute(
                    "UPDATE workflows SET description=?",
                    ("changed outside the reviewed snapshot",),
                )
                context["connection"].commit()
                with mock.patch.object(operations, "_settle_or_run") as dispatch:
                    with self.assertRaisesRegex(EffectBlocked, "metadata differs"):
                        reconcile_canonical_workflows(
                            plan,
                            mode="apply",
                            confirm=plan.sha256,
                        )
                dispatch.assert_not_called()
            self.assertEqual(graph_apply_rows(fixture), [])

        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            value = json.loads(plan.raw)
            item = value["workflows"][0]
            review_path = Path(item["compatibility_review_ref"]["path"])
            review = json.loads(review_path.read_text())
            review["native_evidence_sha256"] = "0" * 64
            review_raw = canonical_bytes(review)
            review_path.write_bytes(review_raw)
            item["compatibility_review_ref"]["sha256"] = hashlib.sha256(
                review_raw
            ).hexdigest()
            invalid = write_canonical_v2_plan(fixture, value, name="bad-native-review.json")
            with native_observer_patches(context):
                with self.assertRaisesRegex(
                    PlanValidationError,
                    "review does not bind the exact v2 plan inputs",
                ):
                    reconcile_canonical_workflows(invalid, mode="prepare")
            self.assertEqual(graph_apply_rows(fixture), [])

        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            with native_observer_patches(context):
                with mock.patch.object(
                    operations.sqlite3,
                    "connect",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    with self.assertRaisesRegex(
                        EffectBlocked,
                        "SQLite observation failed",
                    ):
                        reconcile_canonical_workflows(plan, mode="prepare")
            self.assertEqual(graph_apply_rows(fixture), [])

        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
            context["connection"].close()
            context["database"].unlink()
            with native_observer_patches(context), mock.patch.object(
                operations,
                "_settle_or_run",
            ) as dispatch:
                with self.assertRaisesRegex(EffectBlocked, "absent or inaccessible"):
                    reconcile_canonical_workflows(
                        plan,
                        mode="apply",
                        confirm=plan.sha256,
                    )
                dispatch.assert_not_called()
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_settlement_uses_owned_graph_read_when_database_and_session_are_gone(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
            stage = install_v2_unresolved_graph_postimage(fixture, plan)
            context["connection"].close()
            context["database"].unlink()
            session_dir = context["sessions_root"] / "SESSION-1"
            session_dir.rmdir()
            with mock.patch.object(
                operations,
                "_canonical_v2_read",
                side_effect=AssertionError("mutable native evidence read before settlement"),
            ), mock.patch.object(
                operations,
                "_canonical_v2_plan_check",
                side_effect=AssertionError("plan evidence checked before settlement"),
            ):
                report = reconcile_canonical_workflows(
                    plan,
                    mode="apply",
                    confirm=plan.sha256,
                )
            self.assertEqual(report["settled"], "postimage")
            self.assertEqual(report["phase"], "in_progress")
            self.assertEqual(read_state(fixture)["workflows"][workflow_id(0)]["version"], 2)
            self.assertEqual(graph_apply_rows(fixture), [])
            self.assertEqual(stage["name"], "graph")

    def test_native_inventory_matches_694_total_and_67_detailed_sessions(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, observed = native_canonical_v2_plan(
                fixture,
                self,
                production_shape=True,
            )
            parsed = operations._validate_canonical_v2_plan(plan)
            with native_observer_patches(context):
                refreshed = operations._canonical_v2_read(
                    parsed,
                    parsed["workflows"][0],
                )
            self.assertEqual(len(refreshed["compatibility_snapshot"]["tasks"]), 30)
            self.assertEqual(len(refreshed["compatibility_snapshot"]["sessions"]), 67)
            self.assertEqual(
                len(refreshed["compatibility_snapshot"]["native_components"]["sessions"]),
                67,
            )
            self.assertEqual(
                context["connection"].execute("SELECT count(*) FROM sessions").fetchone()[0],
                694,
            )
            self.assertEqual(
                sum(not row["terminal"] for row in refreshed["compatibility_snapshot"]["tasks"]),
                13,
            )
            self.assertEqual(
                len(observed["compatibility_snapshot"]["native_components"]["sessions"]),
                67,
            )
            self.assertEqual(
                observed["compatibility_snapshot"]["native_components"]["questions"],
                [{
                    "task_id": "TASK-1",
                    "session_id": context["question_session_id"],
                    "call_id": "call-native-question-1",
                    "event_sequence": 1,
                    "input_sha256": canonical_sha256(context["question_input"]),
                }],
            )
            self.assertTrue(all(
                not Path(ref["path"]).exists()
                for session in plan.value["workflows"][0]["compatibility_snapshot"]["sessions"]
                for ref in session["source_refs"]
            ))

    def test_native_project_readers_resolve_absolute_tilde_and_query_relative_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]
            baseline = read_state(fixture)
            for reader in (operations._canonical_v2_read, operations._canonical_v2_owned_read):
                for native_root in (str(Path(temporary)), "~/..", "."):
                    with self.subTest(reader=reader.__name__, native_root=native_root):
                        state = json.loads(json.dumps(baseline))
                        state["project_root"] = native_root
                        write_state(fixture, state)
                        with native_observer_patches(context):
                            result = reader(parsed, item)
                        self.assertEqual(result["workflow_id"], item["workflow_id"])
                for foreign in (str(Path(temporary) / "foreign"), "~/foreign", "foreign"):
                    with self.subTest(reader=reader.__name__, foreign=foreign):
                        state = json.loads(json.dumps(baseline))
                        state["project_root"] = foreign
                        write_state(fixture, state)
                        with native_observer_patches(context), self.assertRaisesRegex(
                            EffectBlocked, "identity/root drifted",
                        ):
                            reader(parsed, item)

    def test_native_graph_save_matches_projected_prompt_bytes_exactly(self) -> None:
        from workflowkit.graph import plan_workflow_graph
        from workflowkit.model import EdgeSpec, NodeSpec, WorkflowSpec

        spec = WorkflowSpec(
            name="Native save fixture", description="Exact prompt bytes.", execution_target="head",
            nodes=(NodeSpec("start", "start", "Start"), NodeSpec("end", "terminal", "End")),
            edges=(EdgeSpec("finish", "start", "finish", "end", prompt=" \nKeep internal\n whitespace.\t\n"),),
        )
        projected = plan_workflow_graph(
            spec,
            {"workflow_id": workflow_id(0), "expected_version": 1,
             "graph": {key: [] for key in ("nodes", "edges", "node_groups", "transition_groups")}},
            metadata_changed=False,
        )
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self, synchronize_effects=True)
            value = plan.value
            item = value["workflows"][0]
            next(edge for edge in item["target_graph"]["edges"] if edge["key"] == "implement_replan")[
                "prompt_template"] = projected.document["graph"]["edges"][0]["prompt_template"]
            review = json.loads(context["review_path"].read_text())
            review["target_graph_sha256"] = canonical_sha256(item["target_graph"])
            raw = canonical_bytes(review)
            context["review_path"].write_bytes(raw)
            item["compatibility_review_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
            plan = write_canonical_v2_plan(fixture, value)
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
                self.assertEqual(
                    reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)["phase"], "complete")
                parsed = operations._validate_canonical_v2_plan(plan)
                actual = operations._canonical_v2_owned_read(parsed, parsed["workflows"][0])
            self.assertEqual(actual["graph"], item["target_graph"])
            self.assertEqual(len(graph_apply_rows(fixture)), 1)
            expected = operations._canonical_v2_owned(actual)
            for drift in ("prompt", "metadata", "version"):
                with self.subTest(drift=drift):
                    state = read_state(fixture)
                    baseline = json.loads(json.dumps(state))
                    workflow = state["workflows"][workflow_id(0)]
                    if drift == "prompt":
                        # Never strip the readback to hide an extra difference.
                        workflow["graph"]["edges"][-1]["prompt_template"] += "\n"
                    elif drift == "metadata":
                        workflow["metadata"]["description"] += " drift"
                    else:
                        workflow["version"] += 1
                    write_state(fixture, state)
                    with native_observer_patches(context), self.assertRaises(EffectBlocked):
                        operations._canonical_v2_assert_owned(parsed, parsed["workflows"][0], expected)
                    write_state(fixture, baseline)
            self.assertEqual(len(graph_apply_rows(fixture)), 1)

    def test_native_workflow_policy_change_preserves_task_bindings(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, observed = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_approvals=True,
                intent="graph-and-metadata", synchronize_effects=True,
                with_questions=True, with_bindings=True,
            )
            value = plan.value
            value["workflows"][0]["target_metadata"]["execution_target"] = "ref:" + "a" * 40
            review = json.loads(context["review_path"].read_text())
            review["target_metadata_sha256"] = canonical_sha256(value["workflows"][0]["target_metadata"])
            review["dispositions"].append({
                "path": "metadata.execution_target", "disposition": "compatible",
                "evidence": review["dispositions"][0]["evidence"],
            })
            raw = canonical_bytes(review)
            context["review_path"].write_bytes(raw)
            value["workflows"][0]["compatibility_review_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
            plan = write_canonical_v2_plan(fixture, value)
            frozen_tables = (
                "task_records", "sessions", "worktrees", "workspaces",
                "task_pending_approvals", "task_pending_approval_branches",
            )
            frozen = [
                context["connection"].execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in frozen_tables
            ]
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
                self.assertEqual(
                    reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)["phase"],
                    "complete",
                )
                parsed = operations._validate_canonical_v2_plan(plan)
                current = operations._canonical_v2_read(parsed, parsed["workflows"][0])
            before = observed["compatibility_snapshot"]["native_components"]
            after = current["compatibility_snapshot"]["native_components"]
            self.assertEqual(before, after)
            self.assertEqual(len(after["questions"]), 2)
            self.assertEqual(len(after["approvals"]), 4)
            for table, rows in zip(frozen_tables, frozen):
                self.assertEqual(
                    context["connection"].execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(), rows)
            self.assertEqual(len(graph_apply_rows(fixture)), 1)
            self.assertEqual(sum(row[:2] == ["workflow", "update"] for row in read_log(fixture)), 1)

    def test_native_execution_binding_drift_is_detected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self, synchronize_effects=True)
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
                context["connection"].execute(
                    "UPDATE task_records SET execution_target_commit_oid=?", ("b" * 40,))
                context["connection"].commit()
                with self.assertRaises(EffectBlocked):
                    reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_binding_digest_protects_every_execution_field_and_referenced_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, observed = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_bindings=True,
            )
            connection = context["connection"]
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]
            baseline = observed["compatibility_snapshot"]["native_components"]
            self.assertEqual(baseline["schema"], "kent-native-components-v2")
            changes = [
                ("task_records", "TASK-1", field, value)
                for field, value in (
                    ("source_workspace_id", "workspace-session"),
                    ("managed_worktree_id", "worktree-session"),
                    ("execution_target_mode", "default_branch"),
                    ("execution_target_requested_ref", "different-ref"),
                    ("execution_target_resolved_ref", "refs/heads/different"),
                    ("execution_target_commit_oid", "f" * 40),
                    ("execution_target_provenance", "workflow_default"),
                )
            ] + [
                ("task_records", "TASK-30", "execution_target_commit_oid", "f" * 40),
                ("task_records", "TASK-13", "execution_target_mode", "default_branch"),
                ("worktrees", "worktree-task", "canonical_root_path", str(Path(temporary) / "different-tree")),
                ("worktrees", "worktree-task", "creation_base_commit_oid", "f" * 40),
                ("worktrees", "worktree-task", "workspace_id", "workspace-session"),
                ("worktrees", "worktree-session", "canonical_root_path", str(Path(temporary) / "different-retained")),
                ("workspaces", "workspace-task", "canonical_root_path", str(Path(temporary) / "different-source")),
                ("workspaces", "workspace-session", "canonical_root_path", str(Path(temporary) / "different-retained-source")),
            ]
            with native_observer_patches(context):
                for table, identity, field, value in changes:
                    with self.subTest(table=table, identity=identity, field=field):
                        old = connection.execute(f"SELECT {field} FROM {table} WHERE id=?", (identity,)).fetchone()[0]
                        connection.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (value, identity))
                        connection.commit()
                        try:
                            current = operations._canonical_v2_read(parsed, item)
                            actual = current["compatibility_snapshot"]["native_components"]
                            self.assertNotEqual(
                                actual["task_execution_bindings_sha256"], baseline["task_execution_bindings_sha256"])
                            with self.assertRaisesRegex(EffectBlocked, "inventory drifted"):
                                operations._canonical_v2_gate(item, current, item["expected_version"])
                        finally:
                            connection.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (old, identity))
                            connection.commit()
                connection.execute("UPDATE task_records SET updated_at_unix_ms=999")
                connection.commit()
                actual = operations._canonical_v2_read(parsed, item)["compatibility_snapshot"]["native_components"]
                self.assertEqual(actual, baseline)
                for table, identity, field, value in (
                    ("task_records", "TASK-1", "source_workspace_id", "absent"),
                    ("task_records", "TASK-1", "managed_worktree_id", "absent"),
                    ("worktrees", "worktree-session", "workspace_id", "absent"),
                    ("workspaces", "workspace-task", "project_id", "foreign-project"),
                    ("workspaces", "workspace-session", "project_id", "foreign-project"),
                ):
                    with self.subTest(missing_or_foreign=(table, identity, field)):
                        old = connection.execute(f"SELECT {field} FROM {table} WHERE id=?", (identity,)).fetchone()[0]
                        connection.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (value, identity))
                        connection.commit()
                        try:
                            with self.assertRaises(EffectBlocked):
                                operations._canonical_v2_read(parsed, item)
                        finally:
                            connection.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (old, identity))
                            connection.commit()
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_component_versions_are_closed_and_preserve_original_shape(self) -> None:
        v1 = {
            "schema": "kent-native-components-v1", "approvals": [], "questions": [], "sessions": [],
            "terminal_inventory_sha256": "a" * 64,
        }
        v2 = {**v1, "schema": "kent-native-components-v2", "task_execution_bindings_sha256": "b" * 64}
        for value in (v1, v2):
            self.assertEqual(operations._native_components(value, "components"), value)
        for value in (
            {**v1, "task_execution_bindings_sha256": "b" * 64},
            {key: value for key, value in v2.items() if key != "task_execution_bindings_sha256"},
            {**v2, "task_execution_bindings_sha256": True},
            {**v2, "task_execution_bindings_sha256": "invalid"},
            {**v2, "extra": None},
        ):
            with self.subTest(value=value), self.assertRaises(PlanValidationError):
                operations._native_components(value, "components")

    def test_legacy_native_evidence_is_recovery_only_and_never_upgraded_from_live_state(self) -> None:
        for outcome in ("new-dispatch", "preimage", "postimage", "final", "complete"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, context, observed = native_canonical_v2_plan(
                    fixture, self, intent="graph-and-metadata" if outcome == "complete" else "graph-only")
                value = plan.value
                item = value["workflows"][0]
                components = item["compatibility_snapshot"]["native_components"]
                components["schema"] = "kent-native-components-v1"
                components.pop("task_execution_bindings_sha256")
                observed["compatibility_snapshot"]["native_components"] = dict(components)
                review = json.loads(context["review_path"].read_text())
                review["compatibility_snapshot_sha256"] = canonical_sha256(item["compatibility_snapshot"])
                raw = canonical_bytes(review)
                context["review_path"].write_bytes(raw)
                item["compatibility_review_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
                plan = write_canonical_v2_plan(fixture, value)
                state_dir = Path(value["state_dir"])
                with self.assertRaisesRegex(EffectBlocked, "require reviewed native-components-v2"):
                    reconcile_canonical_workflows(plan, mode="prepare")
                self.assertFalse(state_dir.exists(), "rejected v1 prepare must not create journal/lock directory")
                before_state = read_state(fixture)
                # A legacy journal is a historical fixture, never a new prepare
                # performed through the v2-only admission gate.
                with OperationJournal(state_dir, "canonical-workflow-reconcile", plan) as journal:
                    journal.persist({
                        "phase": "prepared", "preimage": [observed],
                        "inventory": {}, "members": [{"workflow_id": workflow_id(0), "status": "prepared"}],
                        "effects": {},
                    })
                with native_observer_patches(context):
                    if outcome != "new-dispatch":
                        install_v2_unresolved_graph_postimage(fixture, plan)
                    if outcome == "preimage":
                        write_state(fixture, before_state)
                    if outcome in ("final", "complete"):
                        if outcome == "complete":
                            state = read_state(fixture)
                            state["workflows"][workflow_id(0)]["metadata"] = item["target_metadata"]
                            write_state(fixture, state)
                        with OperationJournal(state_dir, "canonical-workflow-reconcile", plan) as journal:
                            journal.persist({
                                **journal.state, "phase": "complete" if outcome == "complete" else "in_progress",
                                "members": [{"workflow_id": workflow_id(0),
                                             "status": "verified" if outcome == "complete" else "graph_verified"}],
                            })
                    with mock.patch.object(
                        operations, "_canonical_v2_read", side_effect=AssertionError("legacy native reader used"),
                    ), mock.patch.object(
                        operations, "_canonical_v2_plan_check", side_effect=AssertionError("legacy evidence refreshed"),
                    ):
                        if outcome in ("new-dispatch", "final"):
                            with self.assertRaisesRegex(EffectBlocked, "require reviewed native-components-v2"):
                                reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                        else:
                            report = reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                            if outcome == "complete":
                                self.assertTrue(report["resumed"])
                                self.assertEqual(report["phase"], "complete")
                            else:
                                self.assertEqual(report["settled"], outcome)
                                with self.assertRaisesRegex(EffectBlocked, "require reviewed native-components-v2"):
                                    reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                self.assertEqual(graph_apply_rows(fixture), [])
                self.assertEqual(item["compatibility_snapshot"]["native_components"], components)

    def test_native_frozen_approvals_survive_real_graph_and_metadata_effects(self) -> None:
        for intent in ("graph-only", "graph-and-metadata"):
            with self.subTest(intent=intent), tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
                fixture = make_d9_fixture(Path(temporary))
                state = read_state(fixture)
                state["workflows"][workflow_id(0)]["version"] = 16
                write_state(fixture, state)
                plan, context, observed = native_canonical_v2_plan(
                    fixture, self, production_shape=True, with_approvals=True,
                    intent=intent, synchronize_effects=True,
                )
                connection = context["connection"]
                before = [
                    connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                    for table in ("task_pending_approvals", "task_pending_approval_branches")
                ]
                with native_observer_patches(context):
                    self.assertEqual(reconcile_canonical_workflows(plan, mode="prepare")["phase"],
                                     "prepared")
                    report = reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                    self.assertEqual(report["phase"], "complete")
                    parsed = operations._validate_canonical_v2_plan(plan)
                    current = operations._canonical_v2_read(parsed, parsed["workflows"][0])
                self.assertEqual(
                    current["compatibility_snapshot"]["native_components"]["approvals"],
                    observed["compatibility_snapshot"]["native_components"]["approvals"],
                )
                self.assertEqual(len(before[0]), 4)
                for table, rows in zip(
                    ("task_pending_approvals", "task_pending_approval_branches"), before,
                ):
                    self.assertEqual(
                        connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall(), rows)
                workflow = read_state(fixture)["workflows"][workflow_id(0)]
                expected_version = 18 if intent == "graph-and-metadata" else 17
                self.assertEqual(workflow["version"], expected_version)
                self.assertNotEqual(
                    json.loads(before[1][0][3])["prompt_template"],
                    next(edge for edge in workflow["graph"]["edges"]
                         if edge["key"] == "backlog_plan")["prompt_template"],
                )
                self.assertEqual(connection.execute("SELECT version FROM workflows").fetchone(), (expected_version,))
                self.assertEqual(len(graph_apply_rows(fixture)), 1)
                self.assertEqual(
                    sum(row[:2] == ["workflow", "update"] for row in read_log(fixture)),
                    int(intent == "graph-and-metadata"),
                )
                if intent == "graph-only":
                    # A new capture/review starts at graph17, while the native
                    # approvals still contain their untouched graph16 bytes.
                    value = plan.value
                    item = value["workflows"][0]
                    item["expected_version"] = 17
                    item["source_graph_sha256"] = canonical_sha256(item["target_graph"])
                    item["target_graph"]["version"] = 18
                    from workflowkit.delivery import SOURCE_ADMISSION_END, source_admission_gate
                    admission = item["source_admission"]
                    receipt = Path(admission["receipt_ref"]["path"])
                    receipt.write_bytes(b"fresh-source-publication")
                    admission["receipt_ref"]["sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
                    ingress = next(edge for edge in item["target_graph"]["edges"]
                                   if edge["key"] == "backlog_plan")
                    suffix = ingress["prompt_template"].split(SOURCE_ADMISSION_END + "\n\n", 1)[1]
                    ingress["prompt_template"] = source_admission_gate(
                        admission, project_id=PROJECT_ID, workflow_id=workflow_id(0),
                        project_root=str(fixture["root"]),
                    ) + "\n\n" + suffix
                    snapshot = item["compatibility_snapshot"]
                    snapshot["expected_version"] = 17
                    snapshot["native_components"] = current["compatibility_snapshot"]["native_components"]
                    review = json.loads(context["review_path"].read_text())
                    review["source_graph_sha256"] = item["source_graph_sha256"]
                    review["target_graph_sha256"] = canonical_sha256(item["target_graph"])
                    review["source_admission_sha256"] = canonical_sha256(admission)
                    review["compatibility_snapshot_sha256"] = canonical_sha256(snapshot)
                    review_raw = canonical_bytes(review)
                    context["review_path"].write_bytes(review_raw)
                    item["compatibility_review_ref"]["sha256"] = hashlib.sha256(review_raw).hexdigest()
                    value["state_dir"] += "-next"
                    fresh = write_canonical_v2_plan(fixture, value, name="fresh-native-plan.json")
                    with native_observer_patches(context):
                        self.assertEqual(reconcile_canonical_workflows(fresh, mode="prepare")["phase"],
                                         "prepared")
                        self.assertEqual(
                            reconcile_canonical_workflows(fresh, mode="apply", confirm=fresh.sha256)["phase"],
                            "complete",
                        )
                    self.assertEqual(len(graph_apply_rows(fixture)), 2)
                    self.assertEqual(
                        connection.execute("SELECT DISTINCT workflow_version FROM task_pending_approvals").fetchall(),
                        [(16,)],
                    )

    def test_native_frozen_approval_drift_blocks_before_dispatch(self) -> None:
        changes = [
            (table, f"UPDATE {table} SET {column}=' ' || {column}")
            for table, columns in (
                ("task_pending_approvals", ("transition_snapshot_json", "materialized_values_json")),
                ("task_pending_approval_branches", (
                    "target_snapshot_json", "effective_edge_configuration_json",
                    "context_source_resolution_json",
                )),
            )
            for column in columns
        ] + [
            ("version", "UPDATE task_pending_approvals SET workflow_version=15"),
            ("missing", "DELETE FROM task_pending_approvals WHERE source_task_id='TASK-1'"),
            ("extra", """INSERT INTO task_pending_approvals
                SELECT 'extra-approval',source_task_id,source_node_id,source_transition_branch_key,
                source_session_id,workflow_version,transition_snapshot_json,
                materialized_values_json,created_at_unix_ms FROM task_pending_approvals LIMIT 1"""),
            ("owner", "UPDATE task_pending_approvals SET source_session_id='foreign-session'"),
        ]
        for change, sql in changes:
            with self.subTest(change=change, sql=sql), tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, context, _ = native_canonical_v2_plan(
                    fixture, self, production_shape=True, with_approvals=True,
                    synchronize_effects=True,
                )
                with native_observer_patches(context):
                    reconcile_canonical_workflows(plan, mode="prepare")
                    context["connection"].execute(sql)
                    context["connection"].commit()
                    with mock.patch.object(operations, "_settle_or_run") as dispatch:
                        with self.assertRaises(EffectBlocked):
                            reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                        dispatch.assert_not_called()
                self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_drift_after_graph_blocks_metadata_and_completion(self) -> None:
        for change in ("approval", "unknown-graph", "binding", "null-backlog", "retained-root"):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, context, _ = native_canonical_v2_plan(
                    fixture, self, production_shape=True, with_approvals=True,
                    intent="graph-and-metadata", synchronize_effects=True,
                    with_bindings=True,
                )
                settle = operations._settle_or_run

                def settle_then_drift(*args, **kwargs):
                    result = settle(*args, **kwargs)
                    self.assertEqual(result, "postimage")
                    if change == "approval":
                        context["connection"].execute(
                            "UPDATE task_pending_approvals SET materialized_values_json="
                            "' ' || materialized_values_json")
                        context["connection"].commit()
                    elif change == "unknown-graph":
                        state = read_state(fixture)
                        state["workflows"][workflow_id(0)]["graph"]["edges"][0]["prompt_template"] = "unknown"
                        write_state(fixture, state)
                    elif change == "retained-root":
                        context["connection"].execute(
                            "UPDATE worktrees SET canonical_root_path=? WHERE id='worktree-session'",
                            (str(Path(temporary) / "changed-retained-root"),))
                        context["connection"].commit()
                    else:
                        task = "TASK-1" if change == "binding" else "TASK-13"
                        context["connection"].execute(
                            "UPDATE task_records SET execution_target_commit_oid=? WHERE id=?",
                            ("f" * 40, task))
                        context["connection"].commit()
                    return result

                with native_observer_patches(context):
                    reconcile_canonical_workflows(plan, mode="prepare")
                    with mock.patch.object(operations, "_settle_or_run", side_effect=settle_then_drift):
                        with self.assertRaises(EffectBlocked):
                            reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                self.assertEqual(len(graph_apply_rows(fixture)), 1)
                self.assertFalse(any(row[:2] == ["workflow", "update"] for row in read_log(fixture)))

    def test_native_frozen_snapshot_tampering_requires_matching_review(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_approvals=True,
                synchronize_effects=True,
            )
            value = plan.value
            value["workflows"][0]["compatibility_snapshot"]["native_components"]["approvals"][0][
                "record_sha256"] = "0" * 64
            changed = write_canonical_v2_plan(fixture, value)
            with native_observer_patches(context), mock.patch.object(operations, "_settle_or_run") as dispatch:
                parsed = operations._validate_canonical_v2_plan(changed)
                item = parsed["workflows"][0]
                item["graph"] = item["target_graph"]
                current = operations._canonical_v2_read(parsed, item)
                with self.assertRaisesRegex(PlanValidationError, "does not bind the exact v2 plan inputs"):
                    operations._canonical_v2_plan_check(
                        parsed, item, current, expected_workflow_id=None, expected_spec=None)
                with self.assertRaisesRegex(EffectBlocked, "inventory drifted"):
                    reconcile_canonical_workflows(changed, mode="prepare")
                dispatch.assert_not_called()
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_frozen_approvals_settle_without_evidence_but_cannot_progress(self) -> None:
        for intent in ("graph-only", "graph-and-metadata"):
            with self.subTest(intent=intent), tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, context, _ = native_canonical_v2_plan(
                    fixture, self, production_shape=True, with_approvals=True,
                    intent=intent, synchronize_effects=True,
                )
                with native_observer_patches(context):
                    reconcile_canonical_workflows(plan, mode="prepare")
                install_v2_unresolved_graph_postimage(fixture, plan)
                context["connection"].close()
                context["database"].unlink()
                with mock.patch.object(
                    operations, "_canonical_v2_read",
                    side_effect=AssertionError("mutable evidence read before owned-only settlement"),
                ), mock.patch.object(
                    operations, "_canonical_v2_plan_check",
                    side_effect=AssertionError("plan evidence read before owned-only settlement"),
                ):
                    report = reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                self.assertEqual(report["settled"], "postimage")
                self.assertEqual(report["phase"], "in_progress")
                with native_observer_patches(context), self.assertRaises(EffectBlocked):
                    reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                self.assertEqual(graph_apply_rows(fixture), [])
                self.assertFalse(any(row[:2] == ["workflow", "update"] for row in read_log(fixture)))

    def test_native_frozen_approvals_unknown_recovery_postimage_blocks(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_approvals=True,
                synchronize_effects=True,
            )
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
                install_v2_unresolved_graph_postimage(fixture, plan)
                state = read_state(fixture)
                state["workflows"][workflow_id(0)]["graph"]["edges"][0]["prompt_template"] = "unknown"
                write_state(fixture, state)
                with self.assertRaisesRegex(EffectBlocked, "unknown Workflow postimage"):
                    reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_frozen_approvals_known_postimage_settles_then_completes_without_replay(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_approvals=True,
                intent="graph-and-metadata", synchronize_effects=True,
            )
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
                install_v2_unresolved_graph_postimage(fixture, plan)
                context["connection"].execute("UPDATE workflows SET version=2")
                context["connection"].commit()
                report = reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                self.assertEqual(report["settled"], "postimage")
                self.assertEqual(report["phase"], "in_progress")
                self.assertFalse(any(row[:2] == ["workflow", "update"] for row in read_log(fixture)))
                self.assertEqual(
                    reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)["phase"],
                    "complete",
                )
            self.assertEqual(graph_apply_rows(fixture), [])
            self.assertEqual(sum(row[:2] == ["workflow", "update"] for row in read_log(fixture)), 1)
            self.assertEqual(
                context["connection"].execute("SELECT DISTINCT workflow_version FROM task_pending_approvals").fetchall(),
                [(1,)],
            )

    def test_native_four_database_approvals_and_branches_match_reviewed_components(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, observed = native_canonical_v2_plan(
                fixture,
                self,
                production_shape=True,
                with_approvals=True,
            )
            components = observed["compatibility_snapshot"]["native_components"]
            self.assertEqual(len(components["approvals"]), 4)
            self.assertEqual(
                context["connection"].execute(
                    "SELECT count(*) FROM task_pending_approvals"
                ).fetchone()[0],
                4,
            )
            self.assertEqual(
                context["connection"].execute(
                    "SELECT count(*) FROM task_pending_approval_branches"
                ).fetchone()[0],
                4,
            )
            with native_observer_patches(context):
                prepared = reconcile_canonical_workflows(plan, mode="prepare")
            self.assertEqual(prepared["phase"], "prepared")
            self.assertEqual(
                len(plan.value["workflows"][0]["compatibility_snapshot"]["native_components"]["approvals"]),
                4,
            )
            removed_approval = components["approvals"][0]["approval_id"]
            context["connection"].execute(
                "DELETE FROM task_pending_approvals WHERE id=?",
                (removed_approval,),
            )
            context["connection"].commit()
            with native_observer_patches(context), mock.patch.object(
                operations,
                "_settle_or_run",
            ) as dispatch:
                with self.assertRaisesRegex(EffectBlocked, "approval rows differ"):
                    reconcile_canonical_workflows(
                        plan,
                        mode="apply",
                        confirm=plan.sha256,
                    )
                dispatch.assert_not_called()
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_unknown_backlog_without_cli_approval_payload_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, observed = native_canonical_v2_plan(
                fixture,
                self,
                production_shape=True,
                unknown_backlog=True,
            )
            task = next(
                row for row in observed["compatibility_snapshot"]["tasks"]
                if row["id"] == "TASK-1"
            )
            self.assertFalse(task["terminal"])
            self.assertEqual(task["current_nodes"], ["node-backlog"])
            self.assertEqual(task["pending_decisions"], [])
            self.assertEqual(
                observed["compatibility_snapshot"]["native_components"]["approvals"],
                [],
            )
            state = read_state(fixture)
            self.assertNotIn(
                "pending_approvals",
                state["workflows"][workflow_id(0)]["details"]["TASK-1"],
            )
            with native_observer_patches(context):
                prepared = reconcile_canonical_workflows(plan, mode="prepare")
            self.assertEqual(prepared["phase"], "prepared")
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_empty_lock_requires_and_accepts_exact_postcompletion_context(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture,
                self,
                production_shape=True,
            )
            parsed = operations._validate_canonical_v2_plan(plan)
            session = next(
                row
                for row in parsed["workflows"][0]["compatibility_snapshot"]["sessions"]
                if row["task_id"] == "TASK-2"
            )
            event_path = context["sessions_root"] / session["id"] / "events.jsonl"
            header = canonical_bytes({"contract": "kent.session.events", "version": 2})
            context_events = [
                {
                    "seq": 1,
                    "kind": "message",
                    "payload": {
                        "role": "developer",
                        "message_type": "environment",
                        "content": "runtime context",
                    },
                },
                {
                    "seq": 2,
                    "kind": "message",
                    "payload": {
                        "role": "developer",
                        "message_type": "agent_steer",
                        "content": "preserved instruction",
                    },
                },
                {
                    "seq": 3,
                    "kind": "history_replaced",
                    "payload": {
                        "committed_entry_start": 0,
                        "compaction_number": 1,
                        "engine": "remote",
                        "compacted_output": {},
                        "mode": "workflow_post_completion",
                    },
                },
            ]
            event_path.write_bytes(
                header
                + b"\n"
                + b"".join(canonical_bytes(event) + b"\n" for event in context_events)
            )
            context["connection"].execute(
                "UPDATE sessions SET locked_json='{}', last_sequence=3 WHERE id=?",
                (session["id"],),
            )
            context["connection"].commit()
            with native_observer_patches(context):
                live = operations._canonical_v2_read(
                    parsed,
                    parsed["workflows"][0],
                )
            component = next(
                row for row in live["compatibility_snapshot"]["native_components"]["sessions"]
                if row["session_id"] == session["id"]
            )
            self.assertEqual(
                {row["sequence"] for row in component["events"]},
                {1, 2, 3},
            )

            missing_context = [
                {**context_events[1], "seq": 1},
                {**context_events[2], "seq": 2},
            ]
            event_path.write_bytes(
                header
                + b"\n"
                + b"".join(canonical_bytes(event) + b"\n" for event in missing_context)
            )
            context["connection"].execute(
                "UPDATE sessions SET last_sequence=2 WHERE id=?",
                (session["id"],),
            )
            context["connection"].commit()
            with native_observer_patches(context):
                with self.assertRaisesRegex(
                    EffectBlocked,
                    "empty native Session lock lacks exact post-completion and instruction history",
                ):
                    operations._canonical_v2_read(
                        parsed,
                        parsed["workflows"][0],
                    )
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_selected_event_omission_and_terminal_state_drift_block(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture,
                self,
                production_shape=True,
            )
            parsed = operations._validate_canonical_v2_plan(plan)
            first_session = parsed["workflows"][0]["compatibility_snapshot"]["sessions"][0]
            event_path = (
                context["sessions_root"] / first_session["id"] / "events.jsonl"
            )
            # The original file contains an unselected assistant message. Replace it
            # with an instruction-bearing event that must alter reviewed components.
            event_path.write_bytes(
                canonical_bytes({"contract": "kent.session.events", "version": 2})
                + b"\n"
                + canonical_bytes({
                    "seq": 1,
                    "kind": "message",
                    "payload": {
                        "role": "developer",
                        "message_type": "agent_steer",
                        "content": "selected event omitted from reviewed evidence",
                    },
                })
                + b"\n"
            )
            with native_observer_patches(context):
                live = operations._canonical_v2_read(parsed, parsed["workflows"][0])
            with self.assertRaisesRegex(EffectBlocked, "inventory drifted"):
                operations._canonical_v2_gate(
                    parsed["workflows"][0],
                    live,
                    parsed["workflows"][0]["expected_version"],
                )

            context["connection"].execute(
                "UPDATE task_current_nodes SET node_id='node-backlog' WHERE task_id='TASK-14'"
            )
            context["connection"].commit()
            with native_observer_patches(context):
                with self.assertRaisesRegex(EffectBlocked, "current-node ownership differs"):
                    operations._canonical_v2_read(parsed, parsed["workflows"][0])
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_schema_and_project_root_drift_fail_closed(self) -> None:
        for mutation, message in (
            ("schema", "schema fingerprint differs"),
            ("root", "workspace differs"),
            ("project", "project identity is absent"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                dir=Path.home()
            ) as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, context, _ = native_canonical_v2_plan(fixture, self)
                parsed = operations._validate_canonical_v2_plan(plan)
                if mutation == "schema":
                    context["connection"].execute(
                        "CREATE TABLE unexpected_schema_change (id TEXT)"
                    )
                elif mutation == "project":
                    context["connection"].execute(
                        "UPDATE projects SET id=? WHERE id=?",
                        ("project-223e4567-e89b-12d3-a456-426614174111", PROJECT_ID),
                    )
                else:
                    context["connection"].execute(
                        "UPDATE workspaces SET canonical_root_path=?",
                        (str(Path(temporary) / "foreign-root"),),
                    )
                context["connection"].commit()
                with native_observer_patches(context):
                    with self.assertRaisesRegex(EffectBlocked, message):
                        operations._canonical_v2_read(
                            parsed,
                            parsed["workflows"][0],
                        )
                self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_missing_script_inventory_blocks_even_without_execution_list(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            state = read_state(fixture)
            detail = state["workflows"][workflow_id(0)]["details"]["TASK-1"]
            detail.pop("current_scripts", None)
            detail.pop("live_executions", None)
            write_state(fixture, state)
            parsed = operations._validate_canonical_v2_plan(plan)
            with native_observer_patches(context):
                with self.assertRaisesRegex(EffectBlocked, "current Script"):
                    operations._canonical_v2_read(
                        parsed,
                        parsed["workflows"][0],
                    )
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_added_event_components_do_not_inherit_missing_cache_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture,
                self,
                production_shape=True,
            )
            parsed = operations._validate_canonical_v2_plan(plan)
            session = parsed["workflows"][0]["compatibility_snapshot"]["sessions"][0]
            cache_ref = session["source_refs"][0]
            self.assertFalse(Path(cache_ref["path"]).exists())
            with native_observer_patches(context):
                live = operations._canonical_v2_read(
                    parsed,
                    parsed["workflows"][0],
                )
            operations._canonical_v2_gate(
                parsed["workflows"][0],
                live,
                parsed["workflows"][0]["expected_version"],
            )
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_process_identity_drift_during_observation_blocks(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture,
                self,
                production_shape=True,
            )
            parsed = operations._validate_canonical_v2_plan(plan)
            changed_process = {
                **context["process_proof"],
                "start": context["process_proof"]["start"] + 1,
            }
            with native_observer_patches(
                context,
                process_side_effect=[
                    context["process_proof"],
                    changed_process,
                ],
            ):
                with self.assertRaisesRegex(
                    EffectBlocked,
                    "process identity drifted during observation",
                ):
                    operations._canonical_v2_read(
                        parsed,
                        parsed["workflows"][0],
                    )
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_question_input_drift_and_missing_selected_event_block(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture,
                self,
                production_shape=True,
            )
            parsed = operations._validate_canonical_v2_plan(plan)
            event_path = (
                context["sessions_root"]
                / context["question_session_id"]
                / "events.jsonl"
            )
            question_event = {
                "seq": 1,
                "kind": "message",
                "payload": {
                    "role": "assistant",
                    "tool_calls": [{
                        "name": "ask_question",
                        "call_id": "call-native-question-1",
                        "input": {
                            **context["question_input"],
                            "question": "A changed question",
                        },
                    }],
                },
            }
            event_path.write_bytes(
                canonical_bytes({"contract": "kent.session.events", "version": 2})
                + b"\n"
                + canonical_bytes(question_event)
                + b"\n"
            )
            with native_observer_patches(context):
                with self.assertRaisesRegex(EffectBlocked, "output differs"):
                    operations._canonical_v2_read(
                        parsed,
                        parsed["workflows"][0],
                    )

            event_path.unlink()
            with native_observer_patches(context):
                with self.assertRaisesRegex(EffectBlocked, "absent or inaccessible"):
                    operations._canonical_v2_read(
                        parsed,
                        parsed["workflows"][0],
                    )
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_copy_db_and_symlink_hardlink_sources_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self)
            parsed = operations._validate_canonical_v2_plan(plan)
            copied_database = context["home"] / ".kent" / "db" / "copied.sqlite3"
            copied = sqlite3.connect(copied_database)
            context["connection"].backup(copied)
            copied.close()
            original_ids = context["connection"].execute(
                "SELECT id FROM task_records ORDER BY id"
            ).fetchall()
            copied_conn = sqlite3.connect(copied_database)
            self.assertEqual(
                copied_conn.execute("SELECT id FROM task_records ORDER BY id").fetchall(),
                original_ids,
            )
            copied_conn.close()
            copied_descriptor = {
                **parsed["native_evidence"],
                "database_path": str(copied_database),
            }
            with mock.patch.dict(os.environ, {"HOME": str(context["home"])}):
                with self.assertRaisesRegex(EffectBlocked, "default local persistence root"):
                    operations._native_expected_paths(
                        copied_descriptor,
                        PROJECT_ID,
                    )

            hardlink = context["home"] / ".kent" / "db" / "linked.sqlite3"
            os.link(context["database"], hardlink)
            with self.assertRaisesRegex(EffectBlocked, "link count"):
                operations._native_path_stat(hardlink, "native copied database")
            symlink = context["home"] / ".kent" / "db" / "symlink.sqlite3"
            symlink.symlink_to(context["database"])
            with self.assertRaisesRegex(EffectBlocked, "symlink"):
                operations._native_path_stat(symlink, "native linked database")
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_v2_source_gate_is_exactly_bound_and_only_in_backlog_plan_ingress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_v2_plan(fixture)
            item = value["workflows"][0]
            source_graph = {
                "version": item["expected_version"],
                **read_state(fixture)["workflows"][workflow_id(0)]["graph"],
            }
            target_graph = item["target_graph"]
            operations._v2_source_gate_check(
                source_graph,
                target_graph,
                item["source_admission"],
                project_id=PROJECT_ID,
                workflow_id=workflow_id(0),
                project_root=Path(fixture["root"]),
            )
            gate = operations._v2_source_gate_check
            from workflowkit.delivery import source_admission_gate

            expected = source_admission_gate(
                item["source_admission"],
                project_id=PROJECT_ID,
                workflow_id=workflow_id(0),
                project_root=str(fixture["root"]),
            )
            ingress_id = operations._v2_ingress_edge(target_graph, "target graph")["id"]

            def changed(mutator):
                graph = json.loads(json.dumps(target_graph))
                mutator(graph)
                return graph

            cases = {
                "missing": changed(lambda graph: graph["edges"][0].update(
                    prompt_template="Begin plan revalidation."
                )),
                "altered": changed(lambda graph: graph["edges"][0].update(
                    prompt_template=graph["edges"][0]["prompt_template"].replace(
                        item["source_admission"]["receipt_ref"]["sha256"],
                        "0" * 64,
                        1,
                    )
                )),
                "duplicated": changed(lambda graph: graph["edges"][0].update(
                    prompt_template=graph["edges"][0]["prompt_template"] + "\n\n" + expected
                )),
                "misplaced": changed(lambda graph: (
                    graph["edges"][0].update(prompt_template="Begin plan revalidation."),
                    next(edge for edge in graph["edges"] if edge["id"] != ingress_id).update(
                        prompt_template=expected
                    ),
                )),
                "hidden-extra-marker": changed(lambda graph: graph["edges"][1].update(prompt=expected)),
                "node-extra-marker": changed(lambda graph: graph["nodes"][0].update(prompt=expected)),
                "group-extra-marker": changed(lambda graph: graph["transition_groups"][0].update(prompt=expected)),
            }
            for name, graph in cases.items():
                with self.subTest(name=name), self.assertRaises(PlanValidationError):
                    gate(
                        source_graph,
                        graph,
                        item["source_admission"],
                        project_id=PROJECT_ID,
                        workflow_id=workflow_id(0),
                        project_root=Path(fixture["root"]),
                    )
            for graph in (
                changed(lambda graph: graph["edges"][0].update(
                    prompt_template=expected + "\n\nNew reviewed ingress body.")),
                changed(lambda graph: graph["edges"][1].update(
                    prompt_template="New reviewed non-ingress body.")),
            ):
                gate(
                    source_graph, graph, item["source_admission"],
                    project_id=PROJECT_ID, workflow_id=workflow_id(0), project_root=Path(fixture["root"]),
                )

    def test_composed_builder_parameters_and_prompt_reviews_are_exact(self) -> None:
        from workflowkit.graph import plan_workflow_graph
        from workflowkit.delivery import source_admission_gate

        root = Path(__file__).resolve().parents[1]
        builder = runpy.run_path(str(root / ".kent/workflows/kit_development.py"))
        profile = builder["profile_at"](root, check_files=False)
        legacy = replace(
            profile, commands=builder["COMMAND_TARGETS"],
            policies={key: value for key, value in profile.policies.items() if key != "delivery_reliability"},
        )
        for complexity in (False, True):
            with self.subTest(complexity=complexity), tempfile.TemporaryDirectory() as temporary:
                legacy_profile = replace(
                    legacy, policies={**legacy.policies, "coder_selection": "complexity" if complexity else "disabled"},
                )
                current_profile = replace(
                    profile, policies={**profile.policies, "coder_selection": "complexity" if complexity else "disabled"},
                )
                baseline = builder["build_complexity_candidate_workflow" if complexity else "build_workflow"](
                    legacy_profile)
                spec = builder["build_current_workflow"](current_profile)
                empty = {"workflow_id": workflow_id(0), "expected_version": 16,
                         "graph": {key: [] for key in ("nodes", "edges", "node_groups", "transition_groups")}}
                source_graph = plan_workflow_graph(baseline, empty, metadata_changed=False).document["graph"]
                target = plan_workflow_graph(
                    spec, {**empty, "graph": source_graph}, metadata_changed=False).document["graph"]
                source_graph["version"] = 16
                target["version"] = 17
                fixture = make_d9_fixture(Path(temporary))
                plan, value = canonical_v2_plan(fixture)
                item = value["workflows"][0]
                runtime = item["runtime_refs"][0]
                for graph in (source_graph, target):
                    for node in graph["nodes"]:
                        if node["kind"] == "script":
                            node["script_path"] = runtime["path"]
                ingress = operations._v2_ingress_edge(target, "builder target")
                ingress["prompt_template"] = source_admission_gate(
                    item["source_admission"], project_id=PROJECT_ID, workflow_id=workflow_id(0),
                    project_root=str(fixture["root"]),
                ) + "\n\n" + ingress["prompt_template"]
                parameters = next(edge for edge in target["edges"] if edge["key"] == "implement_replan")[
                    "parameters"]
                self.assertEqual(len(parameters), 7 if complexity else 6)
                operations._v2_graph_compatibility(source_graph, target, item["runtime_refs"])
                operations._v2_source_gate_check(
                    source_graph, target, item["source_admission"], project_id=PROJECT_ID,
                    workflow_id=workflow_id(0), project_root=Path(fixture["root"]),
                )
                mutations = [
                    parameters[:-1], list(reversed(parameters)), [{"key": "arbitrary"}],
                    [{**parameters[0], "key": "renamed"}, *parameters[1:]],
                    [{**parameters[0], "type": "changed"}, *parameters[1:]],
                    [{**parameters[0], "default": None}, *parameters[1:]],
                ]
                for mutation in mutations:
                    bad = json.loads(json.dumps(target))
                    next(edge for edge in bad["edges"] if edge["key"] == "implement_replan")[
                        "parameters"] = mutation
                    with self.assertRaisesRegex(PlanValidationError, "additive route"):
                        operations._v2_graph_compatibility(source_graph, bad, item["runtime_refs"])
                typed_source = json.loads(json.dumps(source_graph))
                typed_target = json.loads(json.dumps(target))
                for graph in (typed_source, typed_target):
                    next(edge for edge in graph["edges"] if edge["key"] == "fix_replan")[
                        "parameters"][0]["default"] = True
                next(edge for edge in typed_target["edges"] if edge["key"] == "implement_replan")[
                    "parameters"][0]["default"] = 1
                with self.assertRaisesRegex(PlanValidationError, "additive route"):
                    operations._v2_graph_compatibility(typed_source, typed_target, item["runtime_refs"])
                for duplicate in (False, True):
                    bad_source = json.loads(json.dumps(source_graph))
                    fix = next(edge for edge in bad_source["edges"] if edge["key"] == "fix_replan")
                    if duplicate:
                        bad_source["edges"].append({**fix, "id": "duplicate-fix"})
                        bad_target = json.loads(json.dumps(target))
                        bad_target["edges"].append({**fix, "id": "duplicate-fix"})
                    else:
                        bad_source["edges"].remove(fix)
                        bad_target = json.loads(json.dumps(target))
                        bad_target["edges"] = [edge for edge in bad_target["edges"] if edge["id"] != fix["id"]]
                    with self.assertRaisesRegex(PlanValidationError, "additive route"):
                        operations._v2_graph_compatibility(bad_source, bad_target, item["runtime_refs"])

                source = {
                    **read_state(fixture)["workflows"][workflow_id(0)],
                    "workflow_id": workflow_id(0), "project_id": PROJECT_ID,
                    "version": 16, "graph": source_graph, "links": item["links"], "default": item["default"],
                }
                source["metadata"] = {key: source["metadata"][key]
                                      for key in ("name", "description", "execution_target")}
                item.update(expected_version=16, source_graph_sha256=canonical_sha256(source_graph),
                            source_metadata_sha256=canonical_sha256(source["metadata"]), target_graph=target,
                            anchors=[])
                review_path = Path(item["compatibility_review_ref"]["path"])
                review = json.loads(review_path.read_text())
                review.update(source_graph_sha256=item["source_graph_sha256"],
                              source_metadata_sha256=item["source_metadata_sha256"],
                              target_graph_sha256=canonical_sha256(target),
                              target_metadata_sha256=canonical_sha256(source["metadata"]))
                required = operations._v2_required_review_paths(
                    source_graph, target, source["metadata"], source["metadata"], item["compatibility_snapshot"])
                evidence = review["dispositions"][0]["evidence"]
                review["dispositions"] = [
                    {"path": path, "disposition": "compatible", "evidence": evidence} for path in required
                ]

                def write_review():
                    raw = canonical_bytes(review)
                    review_path.write_bytes(raw)
                    item["compatibility_review_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()

                write_review()
                operations._validate_canonical_v2_target(
                    {"project_root": Path(fixture["root"])}, item, source, None, None)
                prompt_paths = [path for path in required if path.endswith(".prompt_template")]
                self.assertGreater(len(prompt_paths), 1)
                for path in prompt_paths:
                    accepted = review["dispositions"]
                    review["dispositions"] = [row for row in accepted if row["path"] != path]
                    write_review()
                    with self.assertRaisesRegex(PlanValidationError, "changed semantic path"):
                        operations._validate_canonical_v2_target(
                            {"project_root": Path(fixture["root"])}, item, source, None, None)
                    review["dispositions"] = accepted

    def test_v2_script_paths_allow_only_exact_qualified_script_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_v2_plan(fixture, script_path_change=True)
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]
            source_graph = {
                "version": item["expected_version"],
                **read_state(fixture)["workflows"][workflow_id(0)]["graph"],
            }
            target_graph = value["workflows"][0]["target_graph"]
            operations._v2_graph_compatibility(
                source_graph,
                target_graph,
                item["runtime_refs"],
            )
            required = operations._v2_required_review_paths(
                source_graph,
                target_graph,
                {"name": "Workflow", "description": "", "execution_target": "default-branch"},
                {"name": "Workflow", "description": "", "execution_target": "default-branch"},
                item["compatibility_snapshot"],
            )
            self.assertIn("graph.nodes:node-script.script_path", required)

            unqualified = json.loads(json.dumps(target_graph))
            next(node for node in unqualified["nodes"] if node["id"] == "node-script")[
                "script_path"
            ] = str(Path(fixture["root"]) / "unqualified-helper")
            with self.assertRaises(PlanValidationError):
                operations._v2_graph_compatibility(
                    source_graph,
                    unqualified,
                    item["runtime_refs"],
                )
            changed_semantics = json.loads(json.dumps(target_graph))
            next(node for node in changed_semantics["nodes"] if node["id"] == "node-script")[
                "display_name"
            ] = "Not only a script path change"
            with self.assertRaises(PlanValidationError):
                operations._v2_graph_compatibility(
                    source_graph,
                    changed_semantics,
                    item["runtime_refs"],
                )

            reconcile_canonical_workflows(plan, mode="prepare")
            reconcile_canonical_workflows(
                plan,
                mode="apply",
                confirm=plan.sha256,
            )
            live = read_state(fixture)["workflows"][workflow_id(0)]["graph"]
            self.assertEqual(
                next(node for node in live["nodes"] if node["id"] == "node-script")[
                    "script_path"
                ],
                target_graph["nodes"][-1]["script_path"],
            )

    def test_v2_review_must_bind_source_admission_and_metadata_only_null_is_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            allowed, _ = canonical_v2_plan(
                fixture,
                intent="metadata-only",
                source_admission_enabled=False,
            )
            report = reconcile_canonical_workflows(allowed, mode="prepare")
            self.assertEqual(report["phase"], "prepared")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_v2_plan(
                fixture,
                intent="graph-and-metadata",
            )
            review_path = Path(
                value["workflows"][0]["compatibility_review_ref"]["path"]
            )
            review = json.loads(review_path.read_bytes())
            review["source_admission_sha256"] = "0" * 64
            review_raw = canonical_bytes(review)
            review_path.write_bytes(review_raw)
            value["workflows"][0]["compatibility_review_ref"]["sha256"] = hashlib.sha256(
                review_raw
            ).hexdigest()
            bad_review = write_canonical_v2_plan(fixture, value)
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(bad_review, mode="prepare")
            self.assertEqual(graph_apply_rows(fixture), [])

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(
                fixture,
                intent="metadata-only",
                source_admission_enabled=False,
                metadata={
                    "name": "Workflow",
                    "description": "Metadata update without source admission.",
                    "execution_target": "default-branch",
                },
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(plan, mode="prepare")
            self.assertEqual(graph_apply_rows(fixture), [])

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(
                fixture,
                intent="graph-only",
                source_admission_enabled=False,
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(plan, mode="prepare")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_v2_plan(fixture)
            value["workflows"][0]["source_admission"]["primary_root"] = str(
                Path(fixture["root"]) / "different-primary"
            )
            mismatched_root = write_canonical_v2_plan(fixture, value)
            with self.assertRaisesRegex(
                PlanValidationError,
                "source admission primary root differs",
            ):
                reconcile_canonical_workflows(mismatched_root, mode="prepare")

    def test_v2_review_requires_script_path_disposition_and_source_admission_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_v2_plan(
                fixture,
                script_path_change=True,
                name="missing-script-disposition.json",
            )
            review_path = Path(
                value["workflows"][0]["compatibility_review_ref"]["path"]
            )
            review = json.loads(review_path.read_bytes())
            review["dispositions"] = [
                row for row in review["dispositions"]
                if row["path"] != "graph.nodes:node-script.script_path"
            ]
            review_raw = canonical_bytes(review)
            review_path.write_bytes(review_raw)
            value["workflows"][0]["compatibility_review_ref"]["sha256"] = hashlib.sha256(
                review_raw
            ).hexdigest()
            bad_review = write_canonical_v2_plan(
                fixture,
                value,
                name="missing-script-disposition.json",
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(bad_review, mode="prepare")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            _, value = canonical_v2_plan(fixture, name="missing-admission.json")
            del value["workflows"][0]["source_admission"]
            missing_admission = write_canonical_v2_plan(
                fixture,
                value,
                name="missing-admission.json",
            )
            with self.assertRaisesRegex(PlanValidationError, "missing 'source_admission'"):
                reconcile_canonical_workflows(missing_admission, mode="prepare")

    def test_v2_cli_applies_reviewed_same_uuid_update_and_preserves_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(
                fixture,
                intent="graph-and-metadata",
                pending_approval=True,
            )
            script = str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "reconcile-canonical-workflows"
            )
            cli_main = runpy.run_path(script, run_name="reconcile_workflow_cli")["main"]
            for mode in ("prepare", "apply"):
                args = [
                    script,
                    mode,
                    "--plan",
                    str(Path(fixture["root"]) / "canonical-plan-v2.json"),
                    "--expect-plan-sha256",
                    plan.sha256,
                    "--kent",
                    str(fixture["kent"]),
                ]
                if mode == "apply":
                    args.extend(["--confirm", plan.sha256])
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(sys, "argv", args), redirect_stdout(stdout), redirect_stderr(stderr):
                    result = cli_main()
                self.assertEqual(result, 0, stderr.getvalue())
                report = json.loads(stdout.getvalue())
                if mode == "prepare":
                    with OperationJournal(
                        Path(plan.value["state_dir"]),
                        "canonical-workflow-reconcile",
                        plan,
                    ) as journal:
                        prepared = journal.state["inventory"]["source_admission_prepared"]
                        self.assertEqual(
                            prepared["source_admission_sha256"],
                            canonical_sha256(plan.value["workflows"][0]["source_admission"]),
                        )
            self.assertEqual(report["phase"], "complete")
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            self.assertTrue(
                any(edge["key"] == "implement_replan" for edge in workflow["graph"]["edges"])
            )
            self.assertEqual(workflow["version"], 2)
            self.assertTrue(workflow["default"])
            self.assertEqual(
                workflow["details"]["TASK-1"]["pending_approvals"],
                [{"id": "approval-1", "proposal": {"transition": "plan_revalidation"}}],
            )
            calls = read_log(fixture)
            self.assertFalse(
                any(
                    row[:2] in (["workflow", "create"], ["workflow", "link"], ["workflow", "default"])
                    for row in calls
                )
            )
            self.assertEqual(len(graph_apply_rows(fixture)), 1)
            with OperationJournal(
                Path(plan.value["state_dir"]),
                "canonical-workflow-reconcile",
                plan,
            ) as journal:
                admission_records = journal.state["inventory"]["source_admissions"]
                self.assertEqual(
                    set(admission_records),
                    {
                        f"apply:{workflow_id(0)}:graph",
                        f"apply:{workflow_id(0)}:metadata",
                    },
                )
                self.assertTrue(all(
                    row["source_admission_sha256"]
                    == canonical_sha256(plan.value["workflows"][0]["source_admission"])
                    for row in admission_records.values()
                ))

    def test_v2_new_task_or_changed_pending_decision_blocks_before_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture, pending_approval=True)
            reconcile_canonical_workflows(plan, mode="prepare")
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            status = {"kind": "doing", "native_state": "waiting"}
            workflow["tasks"].append({"task_id": "TASK-2", "status": status})
            workflow["details"]["TASK-2"] = {
                "summary": {"id": "TASK-2", "done": False},
                "status": status,
                "current_nodes": [{"node_id": "implement"}],
                "pending_approvals": [],
                "live_sessions": [],
                "retained_session_count": 0,
            }
            workflow["sessions"]["TASK-2"] = []
            write_state(fixture, state)
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(
                    plan,
                    mode="apply",
                    confirm=plan.sha256,
                )
            self.assertEqual(graph_apply_rows(fixture), [])

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture, pending_approval=True)
            reconcile_canonical_workflows(plan, mode="prepare")
            state = read_state(fixture)
            state["workflows"][workflow_id(0)]["details"]["TASK-1"][
                "pending_approvals"
            ][0]["proposal"]["transition"] = "different-transition"
            write_state(fixture, state)
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(
                    plan,
                    mode="apply",
                    confirm=plan.sha256,
                )
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_v2_waiting_approval_without_native_payload_blocks(self) -> None:
        for payload in ("missing", "empty"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, _ = canonical_v2_plan(fixture)
                parsed = operations._validate_canonical_v2_plan(plan)
                item = parsed["workflows"][0]
                state = read_state(fixture)
                workflow = state["workflows"][item["workflow_id"]]
                waiting = {
                    "kind": "waiting_approval",
                    "native_state": "waiting_approval",
                    "attention_types": ["approval"],
                }
                workflow["tasks"][0]["status"] = waiting
                detail = workflow["details"]["TASK-1"]
                detail["status"] = waiting
                detail["summary"]["status"] = waiting
                detail["summary"]["done"] = False
                if payload == "missing":
                    detail.pop("pending_approvals")
                    detail["summary"].pop("pending_approvals", None)
                else:
                    detail["pending_approvals"] = []
                write_state(fixture, state)

                with self.assertRaisesRegex(EffectBlocked, "pending approval inventory"):
                    operations._canonical_v2_read(parsed, item)

    def test_v2_waiting_approval_with_native_payload_is_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture)
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]
            approval = {
                "id": "approval-native",
                "proposal": {"transition": "plan_revalidation"},
            }
            state = read_state(fixture)
            workflow = state["workflows"][item["workflow_id"]]
            waiting = {
                "kind": "waiting_approval",
                "native_state": "waiting_approval",
                "attention_types": ["approval"],
            }
            workflow["tasks"][0]["status"] = waiting
            detail = workflow["details"]["TASK-1"]
            detail["status"] = waiting
            detail["summary"]["status"] = waiting
            detail["summary"]["done"] = False
            detail["pending_approvals"] = [approval]
            write_state(fixture, state)

            live = operations._canonical_v2_read(parsed, item)

            self.assertEqual(
                live["compatibility_snapshot"]["tasks"][0]["pending_decisions"],
                [{
                    "id": "approval-native",
                    "payload_sha256": canonical_sha256(approval),
                }],
            )

    def test_v2_active_task_without_approval_payload_remains_unknown_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture)
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]
            state = read_state(fixture)
            workflow = state["workflows"][item["workflow_id"]]
            active = {"kind": "doing", "native_state": "active"}
            workflow["tasks"][0]["status"] = active
            detail = workflow["details"]["TASK-1"]
            detail["status"] = active
            detail["summary"].update({"status": active, "done": False})
            detail["current_nodes"] = [{"node_id": "node-implement"}]
            detail.pop("pending_approvals")
            write_state(fixture, state)

            with self.assertRaisesRegex(EffectBlocked, "approval inventory is unavailable"):
                operations._canonical_v2_read(parsed, item)

    def test_v2_current_scripts_are_required_and_active_scripts_block(self) -> None:
        for script_state in ("missing", "malformed", "active"):
            with self.subTest(script_state=script_state), tempfile.TemporaryDirectory() as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, _ = canonical_v2_plan(fixture)
                parsed = operations._validate_canonical_v2_plan(plan)
                item = parsed["workflows"][0]
                state = read_state(fixture)
                detail = state["workflows"][item["workflow_id"]]["details"]["TASK-1"]
                if script_state == "missing":
                    detail.pop("current_scripts")
                elif script_state == "malformed":
                    detail["current_scripts"] = {"path": ".kent/scripts/example"}
                else:
                    detail["current_scripts"] = [{
                        "path": ".kent/scripts/example",
                        "current_node": {"node_id": "node-implement"},
                    }]
                write_state(fixture, state)

                with self.assertRaisesRegex(
                    EffectBlocked,
                    "current Script|active native Script",
                ):
                    operations._canonical_v2_read(parsed, item)

    def test_v2_empty_current_scripts_prove_idle_without_live_executions_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture)
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]
            state = read_state(fixture)
            detail = state["workflows"][item["workflow_id"]]["details"]["TASK-1"]
            detail.pop("live_executions", None)
            write_state(fixture, state)

            live = operations._canonical_v2_read(parsed, item)

            self.assertEqual(live["compatibility_snapshot"]["live_executions"], [])

    def test_v2_live_inventory_does_not_echo_plan_only_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture)
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]

            live = operations._canonical_v2_read(parsed, item)
            native_session = live["compatibility_snapshot"]["sessions"][0]
            native_task = live["compatibility_snapshot"]["tasks"][0]

            self.assertNotIn("contract_fingerprint", native_session)
            self.assertNotIn("source_refs", native_session)
            self.assertNotIn("history_classification", native_task)
            self.assertNotIn("history_refs", native_task)
            operations._canonical_v2_gate(item, live, item["expected_version"])

    def test_v2_unknown_history_and_approval_state_do_not_block_terminal_or_backlog(self) -> None:
        for state_name in ("terminal", "backlog"):
            with self.subTest(state=state_name), tempfile.TemporaryDirectory() as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, _ = canonical_v2_plan(fixture)
                parsed = operations._validate_canonical_v2_plan(plan)
                item = parsed["workflows"][0]
                item_task = item["compatibility_snapshot"]["tasks"][0]
                item_task["history_classification"] = "unknown"
                item_task["history_refs"] = []
                state = read_state(fixture)
                workflow = state["workflows"][item["workflow_id"]]
                detail = workflow["details"]["TASK-1"]
                detail.pop("pending_approvals")
                if state_name == "backlog":
                    active = {"kind": "doing", "native_state": "waiting"}
                    workflow["tasks"][0]["status"] = active
                    item_task.update({
                        "status": "doing",
                        "native_state": "waiting",
                        "terminal": False,
                        "current_nodes": ["node-backlog"],
                    })
                    detail.update({
                        "status": active,
                        "current_nodes": [{"node_id": "node-backlog"}],
                    })
                    detail["summary"].update({"status": active, "done": False})
                review_path = Path(item["compatibility_review_ref"]["path"])
                review = json.loads(review_path.read_text())
                review["compatibility_snapshot_sha256"] = canonical_sha256(
                    item["compatibility_snapshot"]
                )
                review_raw = canonical_bytes(review)
                review_path.write_bytes(review_raw)
                item["compatibility_review_ref"]["sha256"] = hashlib.sha256(
                    review_raw
                ).hexdigest()
                write_state(fixture, state)

                live = operations._canonical_v2_read(parsed, item)

                self.assertIsNone(live["compatibility_snapshot"]["tasks"][0]["pending_decisions"])
                operations._canonical_v2_gate(item, live, item["expected_version"])

    def test_v2_unknown_approval_requires_exact_nonimpact_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture)
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]
            state = read_state(fixture)
            state["workflows"][item["workflow_id"]]["details"]["TASK-1"].pop(
                "pending_approvals"
            )
            write_state(fixture, state)
            live = operations._canonical_v2_read(parsed, item)

            review_path = Path(item["compatibility_review_ref"]["path"])
            review = json.loads(review_path.read_text())
            next(
                row for row in review["dispositions"]
                if row["path"] == "task:TASK-1.pending_decisions"
            )["disposition"] = "compatible"
            review_raw = canonical_bytes(review)
            review_path.write_bytes(review_raw)
            item["compatibility_review_ref"]["sha256"] = hashlib.sha256(
                review_raw
            ).hexdigest()

            with self.assertRaisesRegex(EffectBlocked, "inventory drifted"):
                operations._canonical_v2_gate(item, live, item["expected_version"])

    def test_v2_rejects_workflow_linked_from_an_unreviewed_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            fake_kent = fixture["kent"]
            source = fake_kent.read_text()
            marker = '    print(state["project_id"] + "\\tTest Project\\t" + state["project_root"])\n'
            replacement = (
                marker
                + '    print("project-223e4567-e89b-12d3-a456-426614174111'
                + '\\tOther Project\\t/other-project")\n'
            )
            self.assertIn(marker, source)
            fake_kent.write_text(source.replace(marker, replacement, 1))
            plan, _ = canonical_v2_plan(fixture)
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(plan, mode="prepare")
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_v2_runtime_drift_and_unsupported_structural_change_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture)
            from workflowkit.model import NodeSpec, WorkflowSpec

            wrong_spec = WorkflowSpec(
                name="Wrong Workflow",
                description="Not the reviewed target.",
                execution_target="default-branch",
                nodes=(NodeSpec("terminal", "terminal", "Terminal"),),
                edges=(),
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(
                    plan,
                    mode="prepare",
                    expected_workflow_id=workflow_id(0),
                    expected_spec=wrong_spec,
                )
            self.assertEqual(graph_apply_rows(fixture), [])
            reconcile_canonical_workflows(plan, mode="prepare")
            runtime_file = Path(plan.value["workflows"][0]["runtime_refs"][0]["path"])
            runtime_file.write_text("#!/bin/sh\nexit 1\n")
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(
                    plan,
                    mode="apply",
                    confirm=plan.sha256,
                )
            self.assertEqual(graph_apply_rows(fixture), [])

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_v2_plan(fixture)
            value["workflows"][0]["target_graph"]["edges"][0][
                "target_node_id"
            ] = "node-terminal"
            raw = canonical_bytes(value)
            plan_path = Path(fixture["root"]) / "canonical-plan-v2.json"
            plan_path.write_bytes(raw)
            invalid = load_plan(
                plan_path,
                schema="canonical-workflow-reconcile-plan-v2",
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(invalid, mode="prepare")
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_v2_timeout_with_task_progress_settles_owned_graph_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            fake_kent = fixture["kent"]
            source = fake_kent.read_text()
            marker = (
                '    save()\n'
                '    print(json.dumps({"version": workflow["version"]}))\n'
            )
            replacement = (
                '    if state.get("timeout_progress"):\n'
                '        task_status = {"kind": "doing", "native_state": "waiting"}\n'
                '        workflow["tasks"][0]["status"] = task_status\n'
                '        detail = workflow["details"]["TASK-1"]\n'
                '        detail["summary"]["done"] = False\n'
                '        detail["status"] = task_status\n'
                '        detail["current_nodes"] = [{"node_id": "node-implement"}]\n'
                '        save()\n'
                '        import time\n'
                '        time.sleep(2)\n'
                '    else:\n'
                '        save()\n'
                '    print(json.dumps({"version": workflow["version"]}))\n'
            )
            self.assertIn(marker, source)
            fake_kent.write_text(source.replace(marker, replacement, 1))
            state = read_state(fixture)
            state["timeout_progress"] = True
            write_state(fixture, state)
            plan, _ = canonical_v2_plan(
                fixture,
                intent="graph-and-metadata",
            )
            reconcile_canonical_workflows(plan, mode="prepare")
            settle = operations._settle_or_run

            def short_timeout(*args, **kwargs):
                kwargs["timeout"] = 0.5
                return settle(*args, **kwargs)

            with mock.patch.object(
                operations,
                "_settle_or_run",
                side_effect=short_timeout,
            ):
                report = reconcile_canonical_workflows(
                    plan,
                    mode="apply",
                    confirm=plan.sha256,
                )
            self.assertEqual(report["phase"], "in_progress")
            self.assertEqual(report["settled"], "postimage")
            self.assertEqual(len(graph_apply_rows(fixture)), 1)
            self.assertFalse(
                any(row[:2] == ["workflow", "update"] for row in read_log(fixture))
            )
            with OperationJournal(
                Path(plan.value["state_dir"]),
                "canonical-workflow-reconcile",
                plan,
            ) as journal:
                effect = journal.state["effects"][
                    f"apply:{workflow_id(0)}:graph"
                ]
                self.assertEqual(effect["status"], "verified")
                self.assertEqual(
                    journal.state["members"][0]["status"],
                    "graph_verified",
                )

    def test_v2_resumes_unresolved_postimage_after_task_progress_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(fixture, intent="graph-and-metadata")
            reconcile_canonical_workflows(plan, mode="prepare")
            parsed = operations._validate_canonical_v2_plan(plan)
            item = parsed["workflows"][0]
            item["graph"] = item["target_graph"]
            item["metadata"] = item["target_metadata"]
            wid = workflow_id(0)
            state_path = Path(plan.value["state_dir"])
            with OperationJournal(
                state_path,
                "canonical-workflow-reconcile",
                plan,
            ) as journal:
                prepared = journal.state["preimage"][0]
                graph_stage = next(
                    stage
                    for stage in operations._canonical_progress(parsed, item, prepared)
                    if stage["name"] == "graph"
                )
                identity = operations._effect_inputs(
                    graph_stage["command"],
                    Path(fixture["root"]),
                    None,
                    graph_stage["stdin"],
                    canonical_sha256(operations._canonical_v2_owned(graph_stage["before"])),
                    canonical_sha256(operations._canonical_v2_owned(graph_stage["after"])),
                )[4]
                journal.persist({
                    **journal.state,
                    "phase": "in_progress",
                    "effects": {
                        f"apply:{wid}:graph": {
                            **identity,
                            "status": "unresolved",
                            "attempt": 1,
                            "child": None,
                        },
                    },
                })
            state = read_state(fixture)
            workflow = state["workflows"][wid]
            workflow["version"] = graph_stage["after"]["version"]
            workflow["graph"] = {
                key: graph_stage["after"]["graph"][key]
                for key in ("nodes", "edges", "node_groups", "transition_groups")
            }
            task_status = {"kind": "doing", "native_state": "waiting"}
            workflow["tasks"][0]["status"] = task_status
            detail = workflow["details"]["TASK-1"]
            detail["summary"]["done"] = False
            detail["status"] = task_status
            detail["current_nodes"] = [{"node_id": "node-implement"}]
            write_state(fixture, state)

            report = reconcile_canonical_workflows(
                plan,
                mode="apply",
                confirm=plan.sha256,
            )
            self.assertEqual(report["phase"], "in_progress")
            self.assertEqual(report["settled"], "postimage")
            self.assertEqual(graph_apply_rows(fixture), [])
            self.assertFalse(
                any(row[:2] == ["workflow", "update"] for row in read_log(fixture))
            )
            with OperationJournal(
                state_path,
                "canonical-workflow-reconcile",
                plan,
            ) as journal:
                self.assertEqual(
                    journal.state["effects"][f"apply:{wid}:graph"]["status"],
                    "verified",
                )
                self.assertEqual(journal.state["members"][0]["status"], "graph_verified")

    def test_v2_settles_attempted_graph_before_missing_evidence_blocks_next_effect(self) -> None:
        for missing in ("runtime", "review", "receipt"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                fixture = make_d9_fixture(Path(temporary))
                plan, _ = canonical_v2_plan(
                    fixture,
                    intent="graph-and-metadata",
                )
                reconcile_canonical_workflows(plan, mode="prepare")
                graph_stage = install_v2_unresolved_graph_postimage(fixture, plan)
                item = plan.value["workflows"][0]
                if missing == "runtime":
                    Path(item["runtime_refs"][0]["path"]).unlink()
                elif missing == "review":
                    Path(item["compatibility_review_ref"]["path"]).unlink()
                else:
                    Path(item["source_admission"]["receipt_ref"]["path"]).unlink()

                admission_calls = []

                def refuse_fresh_admission(*args, **kwargs):
                    admission_calls.append((args, kwargs))
                    raise PlanValidationError("simulated source receipt is unavailable")

                with mock.patch.object(
                    operations,
                    "admit_source_admission",
                    side_effect=refuse_fresh_admission,
                ) as admission:
                    with self.assertRaises((PlanValidationError, EffectBlocked)):
                        reconcile_canonical_workflows(
                            plan,
                            mode="apply",
                            confirm=plan.sha256,
                        )
                self.assertEqual(admission.call_count, 1 if missing == "receipt" else 0)
                self.assertEqual(len(admission_calls), admission.call_count)
                self.assertEqual(graph_apply_rows(fixture), [])
                with OperationJournal(
                    Path(plan.value["state_dir"]),
                    "canonical-workflow-reconcile",
                    plan,
                ) as journal:
                    effects = journal.state["effects"]
                    graph_key = f"apply:{workflow_id(0)}:graph"
                    metadata_key = f"apply:{workflow_id(0)}:metadata"
                    self.assertEqual(effects[graph_key]["status"], "verified")
                    self.assertNotIn(metadata_key, effects)
                    live_workflow = read_state(fixture)["workflows"][workflow_id(0)]
                    self.assertEqual(
                        live_workflow["version"],
                        graph_stage["after"]["version"],
                    )
                    self.assertEqual(
                        canonical_sha256(live_workflow["graph"]),
                        canonical_sha256({
                            key: graph_stage["after"]["graph"][key]
                            for key in ("nodes", "edges", "node_groups", "transition_groups")
                        }),
                    )

    def test_v2_reverse_uses_fresh_plan_and_legacy_rollback_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            original_description = read_state(fixture)["workflows"][
                workflow_id(0)
            ]["metadata"]["description"]
            forward, _ = canonical_v2_plan(
                fixture,
                intent="graph-and-metadata",
            )
            reconcile_canonical_workflows(forward, mode="prepare")
            reconcile_canonical_workflows(
                forward,
                mode="apply",
                confirm=forward.sha256,
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(
                    forward,
                    mode="rollback",
                    confirm=forward.sha256,
                )
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            status = {"kind": "doing", "native_state": "waiting"}
            workflow["tasks"][0]["status"] = status
            workflow["details"]["TASK-1"]["summary"]["done"] = False
            workflow["details"]["TASK-1"]["status"] = status
            workflow["details"]["TASK-1"]["current_nodes"] = [
                {"node_id": "node-implement"}
            ]
            write_state(fixture, state)
            reverse, _ = canonical_v2_plan(
                fixture,
                intent="metadata-only",
                metadata={
                    "name": workflow["metadata"]["name"],
                    "description": original_description,
                    "execution_target": workflow["metadata"]["execution_target"],
                },
                name="reverse-plan-v2.json",
            )
            reconcile_canonical_workflows(reverse, mode="prepare")
            report = reconcile_canonical_workflows(
                reverse,
                mode="apply",
                confirm=reverse.sha256,
            )
            self.assertEqual(report["phase"], "complete")
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            self.assertEqual(workflow["metadata"]["description"], original_description)
            self.assertTrue(
                any(edge["key"] == "implement_replan" for edge in workflow["graph"]["edges"])
            )
            self.assertEqual(len(graph_apply_rows(fixture)), 1)

    def test_native_project_identity_is_preserved_in_cli_readbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_plan(fixture)
            reconcile_canonical_workflows(plan, mode="prepare")
            calls = read_log(fixture)
            project_calls = [call for call in calls if "--project" in call]
            self.assertTrue(project_calls)
            for call in project_calls:
                self.assertEqual(call[call.index("--project") + 1], PROJECT_ID)

    def test_canonical_project_fields_reject_non_native_or_mismatched_ids(self) -> None:
        invalid = (
            RETIREMENT_PROJECT_ID,
            "workspace-" + RETIREMENT_PROJECT_ID,
            "project-" + RETIREMENT_PROJECT_ID.upper(),
            "project-not-a-uuid",
            "project-" + RETIREMENT_PROJECT_ID + " ",
        )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            _, baseline = canonical_plan(fixture)
            for field in ("project", "link"):
                for value in (*invalid, "project-123e4567-e89b-12d3-a456-426614174099"):
                    with self.subTest(field=field, value=value):
                        document = json.loads(json.dumps(baseline))
                        member = document["workflows"][0]
                        target = member if field == "project" else member["links"][0]
                        target["project_id"] = value
                        raw = canonical_bytes(document)
                        path = Path(temporary) / "invalid-project.json"
                        path.write_bytes(raw)
                        plan = load_plan(
                            path,
                            schema="canonical-workflow-reconcile-plan-v1",
                            expected_sha256=hashlib.sha256(raw).hexdigest(),
                        )
                        with self.assertRaises(PlanValidationError):
                            operations._validate_canonical_plan(plan)
            self.assertEqual(read_log(fixture), [])

    def test_workflow_summary_requires_closed_metadata_and_normalizes_live_targets(self) -> None:
        wid = workflow_id(0)
        policies = {
            "ask_on_first_execution": "ask-on-first-execution",
            "none": "none",
            "head": "head",
            "default_branch": "default-branch",
            "custom_ref": "ref:refs/tags/v1",
        }
        for mode, expected in policies.items():
            with self.subTest(mode=mode):
                policy = {"mode": mode}
                if mode == "custom_ref":
                    policy["custom_ref"] = "refs/tags/v1"
                summary = {
                    "id": wid,
                    "version": 1,
                    "name": "Workflow",
                    "description": "Description",
                    "execution_target_policy": policy,
                }
                self.assertEqual(
                    operations._workflow_summary(summary, wid),
                    {"present": True, "workflow_id": wid, "revision": 1},
                )
                self.assertEqual(
                    operations._canonical_metadata(summary)["execution_target"],
                    expected,
                )
        invalid = [
            {"revision": 1},
            {"version": True},
            {"version": -1},
            {"version": 1, "extra": True},
            {"version": 1, "execution_target_policy": {"mode": "unknown"}},
            {"version": 1, "execution_target_policy": {"mode": "custom_ref"}},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                source = {
                    "id": wid,
                    "version": 1,
                    "name": "Workflow",
                    "description": "Description",
                    "execution_target_policy": {"mode": "none"},
                }
                source.update(fields)
                with self.assertRaises(EffectBlocked):
                    operations._workflow_summary(source, wid)
                with self.assertRaises(EffectBlocked):
                    operations._canonical_metadata(source)
        flat = {
            "id": wid,
            "version": 1,
            "name": "Workflow",
            "description": "",
            "execution_target_policy": {"mode": "none"},
        }
        self.assertEqual(operations._canonical_metadata(flat)["description"], "")
        wrapped_revision = {key: value for key, value in flat.items() if key != "version"}
        wrapped_revision["revision"] = 1
        invalid_shapes = [
            None, [], {"workflow": flat}, {"workflow": wrapped_revision},
            *({key: value for key, value in flat.items() if key != missing} for missing in flat),
        ]
        for body in invalid_shapes:
            with self.subTest(shape=body):
                with self.assertRaises(EffectBlocked):
                    operations._workflow_summary(body, wid)
                with self.assertRaises(EffectBlocked):
                    operations._canonical_metadata(body)
        invalid_summaries = [
            {
                "id": workflow_id(1),
            },
            {
                "version": "1",
            },
            {
                "id": None,
            },
            {
                "id": "",
            },
            {
                "id": "\x00",
            },
            {
                "name": None,
            },
            {
                "description": 7,
            },
            {
                "execution_target_policy": None,
            },
            {
                "execution_target_policy": [],
            },
            {
                "execution_target_policy": {"mode": "none", "extra": True},
            },
            {
                "execution_target_policy": {"mode": "custom_ref", "custom_ref": ""},
            },
            {
                "execution_target_policy": {"mode": "custom_ref", "custom_ref": "refs/tags/v 1"},
            },
        ]
        for mutation in invalid_summaries:
            with self.subTest(summary_mutation=mutation):
                source = {
                    "id": wid,
                    "version": 1,
                    "name": "Workflow",
                    "description": "Description",
                    "execution_target_policy": {"mode": "none"},
                }
                source.update(mutation)
                with self.assertRaises(EffectBlocked):
                    operations._workflow_summary(source, wid)
                if "id" not in mutation or mutation["id"] != workflow_id(1):
                    with self.assertRaises(EffectBlocked):
                        operations._canonical_metadata(source)

    def test_plan_accepts_only_public_execution_target_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            targets = (
                "ask-on-first-execution",
                "none",
                "head",
                "default-branch",
                "ref:refs/tags/v1",
            )
            for index, target in enumerate(targets):
                with self.subTest(target=target):
                    plan, _ = canonical_plan(
                        fixture,
                        intent="metadata-only",
                        metadata={
                            "name": "Canonical",
                            "description": "Description",
                            "execution_target": target,
                        },
                        name=f"target-{index}.json",
                    )
                    parsed = operations._validate_canonical_plan(plan)
                    self.assertEqual(
                        parsed["workflows"][0]["metadata"]["execution_target"],
                        target,
                    )
            for index, target in enumerate((
                "ask_on_first_execution",
                "default_branch",
                "ref:",
                "ref:refs/tags/v 1",
                "local",
            )):
                with self.subTest(invalid_target=target):
                    plan, _ = canonical_plan(
                        fixture,
                        intent="metadata-only",
                        metadata={
                            "name": "Canonical",
                            "description": "Description",
                            "execution_target": target,
                        },
                        name=f"invalid-{index}.json",
                    )
                    with self.assertRaises(PlanValidationError):
                        operations._validate_canonical_plan(plan)

    def test_public_execution_target_selectors_cover_metadata_stage_contract(self) -> None:
        targets = (
            "ask-on-first-execution",
            "none",
            "head",
            "default-branch",
            "ref:refs/tags/v1",
        )
        for index, target in enumerate(targets):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                fixture = make_d9_fixture(Path(temporary))
                metadata = {
                    "name": f"Metadata {index}",
                    "description": "Selector contract",
                    "execution_target": target,
                }
                plan, _ = canonical_plan(
                    fixture,
                    intent="metadata-only",
                    metadata=metadata,
                    name="metadata-plan.json",
                )
                parsed = operations._validate_canonical_plan(plan)
                item = parsed["workflows"][0]
                self.assertEqual(item["metadata"]["execution_target"], target)
                reconcile_canonical_workflows(plan, mode="prepare")
                with OperationJournal(
                    fixture["root"] / "canonical-state",
                    "canonical-workflow-reconcile",
                    plan,
                ) as journal:
                    prepared = journal.state["preimage"][0]
                    stage = operations._canonical_progress(parsed, item, prepared)[0]
                    before_sha = canonical_sha256(stage["before"])
                    after_sha = canonical_sha256(stage["after"])
                    expected_identity = operations._effect_inputs(
                        stage["command"],
                        fixture["root"],
                        None,
                        stage["stdin"],
                        before_sha,
                        after_sha,
                    )[4]
                    self.assertEqual(
                        stage["command"],
                        [
                            str(fixture["kent"]),
                            "workflow",
                            "update",
                            workflow_id(0),
                            "--name",
                            metadata["name"],
                            "--description",
                            metadata["description"],
                            "--execution-target",
                            target,
                            "--json",
                        ],
                    )
                    self.assertEqual(
                        journal.state["inventory"]["targets"][workflow_id(0)],
                        {
                            "preimage_sha256": before_sha,
                            "target_sha256": after_sha,
                        },
                    )
                report = reconcile_canonical_workflows(plan, mode="apply", confirm=True)
                self.assertEqual(report["phase"], "complete")
                self.assertIn(
                    [
                        "workflow",
                        "update",
                        workflow_id(0),
                        "--name",
                        metadata["name"],
                        "--description",
                        metadata["description"],
                        "--execution-target",
                        target,
                        "--json",
                    ],
                    read_log(fixture),
                )
                with OperationJournal(
                    fixture["root"] / "canonical-state",
                    "canonical-workflow-reconcile",
                    plan,
                ) as journal:
                    effect = journal.state["effects"][f"apply:{workflow_id(0)}:metadata"]
                    for key, value in expected_identity.items():
                        self.assertEqual(effect[key], value)
                final = operations._canonical_read(parsed, item)
                self.assertEqual(final["metadata"], metadata)
                self.assertEqual(canonical_sha256(final), after_sha)

            with self.subTest(recovery_target=target), tempfile.TemporaryDirectory() as temporary:
                fixture = make_d9_fixture(Path(temporary))
                metadata = {
                    "name": f"Recovery metadata {index}",
                    "description": "Recovery selector contract",
                    "execution_target": target,
                }
                plan, _ = canonical_plan(
                    fixture,
                    intent="metadata-only",
                    metadata=metadata,
                    name="recovery-plan.json",
                )
                parsed = operations._validate_canonical_plan(plan)
                item = parsed["workflows"][0]
                reconcile_canonical_workflows(plan, mode="prepare")
                state = read_state(fixture)
                state["workflows"][workflow_id(0)]["metadata"] = metadata
                write_state(fixture, state)
                stage = install_effect(
                    fixture,
                    plan,
                    member_status="prepared",
                    effect_status="unresolved",
                    stage_name="metadata",
                )
                expected_identity = operations._effect_inputs(
                    stage["command"],
                    fixture["root"],
                    None,
                    stage["stdin"],
                    canonical_sha256(stage["before"]),
                    canonical_sha256(stage["after"]),
                )[4]
                report = reconcile_canonical_workflows(plan, mode="apply", confirm=True)
                self.assertEqual(report["phase"], "complete")
                self.assertEqual(
                    [row for row in read_log(fixture) if row[:2] == ["workflow", "update"]],
                    [],
                )
                with OperationJournal(
                    fixture["root"] / "canonical-state",
                    "canonical-workflow-reconcile",
                    plan,
                ) as journal:
                    effect = journal.state["effects"][f"apply:{workflow_id(0)}:metadata"]
                    for key, value in expected_identity.items():
                        self.assertEqual(effect[key], value)
                    self.assertEqual(effect["status"], "verified")
                    self.assertEqual(effect["attempt"], 1)
                final = operations._canonical_read(parsed, item)
                self.assertEqual(final["metadata"], metadata)
                self.assertEqual(
                    canonical_sha256(final),
                    canonical_sha256(stage["after"]),
                )

    def test_validation_requires_true_but_ignores_nonblocking_diagnostics(self) -> None:
        cases = (
            {"valid": True, "errors": [{"code": "script_path_relative_check_skipped"}]},
            {"valid": False},
            {},
            {"valid": "true"},
        )
        for validation in cases:
            with self.subTest(validation=validation), tempfile.TemporaryDirectory() as temporary:
                fixture = make_d9_fixture(Path(temporary))
                state = read_state(fixture)
                state["validation"] = validation
                write_state(fixture, state)
                plan, _ = canonical_plan(fixture)
                if validation.get("valid") is True:
                    report = reconcile_canonical_workflows(plan, mode="prepare")
                    self.assertEqual(report["phase"], "prepared")
                    with OperationJournal(
                        fixture["root"] / "canonical-state",
                        "canonical-workflow-reconcile",
                        plan,
                    ) as journal:
                        persisted = canonical_bytes(journal.state)
                    evidence = canonical_bytes(report)
                    self.assertNotIn(b"script_path_relative_check_skipped", persisted)
                    self.assertNotIn(b"script_path_relative_check_skipped", evidence)
                    self.assertNotIn(b"errors", persisted)
                    self.assertNotIn(b"errors", evidence)
                else:
                    with self.assertRaises(EffectBlocked):
                        reconcile_canonical_workflows(plan, mode="prepare")

    def test_raw_fields_and_nonterminal_plan_state_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            _, value = canonical_plan(fixture)
            for mutation in (
                lambda item: item.update(command=["/bin/echo"]),
                lambda item: item.update(preimage={}),
                lambda item: item["terminal_tasks"].append(
                    {"id": "TASK-2", "status": "doing"}
                ),
            ):
                candidate = json.loads(json.dumps(value))
                mutation(candidate["workflows"][0])
                if candidate["workflows"][0].get("terminal_tasks", [])[-1].get(
                    "status"
                ) == "doing":
                    candidate["workflows"][0]["terminal_tasks"][-1]["extra"] = True
                raw = canonical_bytes(candidate)
                path = fixture["root"] / "invalid.json"
                path.write_bytes(raw)
                plan = load_plan(
                    path,
                    schema="canonical-workflow-reconcile-plan-v1",
                    expected_sha256=hashlib.sha256(raw).hexdigest(),
                )
                with self.assertRaises(PlanValidationError):
                    reconcile_canonical_workflows(plan, mode="prepare")

    def test_missing_or_incomplete_d9_dependency_blocks_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            d9 = {
                "none": False,
                "path": str(fixture["root"] / "missing-d9.json"),
                "sha256": "a" * 64,
                "operation": "workflow-retirement-batch",
                "phase": "complete",
                "members": [workflow_id(0)],
            }
            plan, _ = canonical_plan(fixture, d9=d9)
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(plan, mode="prepare")
            self.assertFalse((fixture["root"] / "canonical-state").exists())

    def test_task_detail_activity_blocks_even_when_task_list_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            state = read_state(fixture)
            detail = state["workflows"][workflow_id(0)]["details"]["TASK-1"]
            detail["summary"]["done"] = False
            detail["status"] = {"kind": "doing", "native_state": "active"}
            detail["current_nodes"] = [{"node_id": "implement"}]
            write_state(fixture, state)
            plan, _ = canonical_plan(fixture)
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(plan, mode="prepare")

    def test_project_link_default_and_revision_drift_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_plan(fixture)
            state = read_state(fixture)
            state["workflows"][workflow_id(0)]["default"] = False
            write_state(fixture, state)
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(plan, mode="prepare")
            state["workflows"][workflow_id(0)]["default"] = True
            state["workflows"][workflow_id(0)]["version"] = 2
            write_state(fixture, state)
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(plan, mode="prepare")

    def test_exact_graph_apply_advances_one_version_with_complete_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_plan(fixture)
            prepared = reconcile_canonical_workflows(plan, mode="prepare")
            self.assertEqual(prepared["phase"], "prepared")
            report = reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            self.assertEqual(report["phase"], "complete")
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            self.assertEqual(workflow["version"], 2)
            self.assertEqual(workflow["graph"], {
                key: value["workflows"][0]["graph"][key]
                for key in ("nodes", "edges", "node_groups", "transition_groups")
            })
            self.assertEqual(
                state["last_graph_document"],
                {
                    "workflow_id": workflow_id(0),
                    "expected_version": 1,
                    "graph": workflow["graph"],
                },
            )
            self.assertEqual(
                graph_apply_rows(fixture),
                [["workflow", "graph", "apply", "-", "--confirm", "--json"]],
            )

    def test_zero_exit_graph_noop_settles_preimage_without_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_plan(fixture)
            reconcile_canonical_workflows(plan, mode="prepare")
            state = read_state(fixture)
            state["graph_noop"] = True
            write_state(fixture, state)
            report = reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            self.assertEqual(report["phase"], "in_progress")
            self.assertEqual(report["settled"], "preimage")
            with OperationJournal(
                fixture["root"] / "canonical-state",
                "canonical-workflow-reconcile",
                plan,
            ) as journal:
                effect = journal.state["effects"][f"apply:{workflow_id(0)}:graph"]
                self.assertEqual(effect["status"], "settled_preimage")

    def test_later_cycle_retry_reuses_identity_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_plan(fixture)
            reconcile_canonical_workflows(plan, mode="prepare")
            state = read_state(fixture)
            state["graph_noop"] = True
            write_state(fixture, state)
            reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            state = read_state(fixture)
            state["graph_noop"] = False
            write_state(fixture, state)
            report = reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            self.assertEqual(report["phase"], "complete")
            with OperationJournal(
                fixture["root"] / "canonical-state",
                "canonical-workflow-reconcile",
                plan,
            ) as journal:
                effect = journal.state["effects"][f"apply:{workflow_id(0)}:graph"]
                self.assertEqual(effect["attempt"], 2)

    def test_exact_target_recovery_does_not_replay_graph_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_plan(fixture)
            reconcile_canonical_workflows(plan, mode="prepare")
            stage = install_effect(
                fixture,
                plan,
                member_status="prepared",
                effect_status="unresolved",
            )
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            workflow["version"] = 2
            workflow["graph"] = {
                key: value["workflows"][0]["graph"][key]
                for key in ("nodes", "edges", "node_groups", "transition_groups")
            }
            write_state(fixture, state)
            report = reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            self.assertEqual(report["phase"], "complete")
            self.assertEqual(graph_apply_rows(fixture), [])
            self.assertEqual(stage["after"]["version"], 2)

    def test_graph_and_metadata_resume_preserves_progressive_postimages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            metadata = {
                "name": "Canonical v2",
                "description": "All metadata fields",
                "execution_target": "head",
            }
            plan, value = canonical_plan(
                fixture,
                intent="graph-and-metadata",
                metadata=metadata,
            )
            reconcile_canonical_workflows(plan, mode="prepare")
            install_effect(
                fixture,
                plan,
                member_status="graph_verified",
                effect_status="verified",
            )
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            workflow["version"] = 2
            workflow["graph"] = {
                key: value["workflows"][0]["graph"][key]
                for key in ("nodes", "edges", "node_groups", "transition_groups")
            }
            write_state(fixture, state)
            report = reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            self.assertEqual(report["phase"], "complete")
            state = read_state(fixture)
            self.assertEqual(state["workflows"][workflow_id(0)]["metadata"], metadata)
            self.assertEqual(graph_apply_rows(fixture), [])
            self.assertEqual(
                state["last_update_argv"],
                [
                    "workflow",
                    "update",
                    workflow_id(0),
                    "--name",
                    metadata["name"],
                    "--description",
                    metadata["description"],
                    "--execution-target",
                    metadata["execution_target"],
                    "--json",
                ],
            )

    def test_concurrent_task_allocation_blocks_before_graph_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_plan(fixture)
            reconcile_canonical_workflows(plan, mode="prepare")
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            status = {"kind": "done", "native_state": "terminal"}
            workflow["tasks"].append({"task_id": "TASK-2", "status": status})
            workflow["details"]["TASK-2"] = {
                "summary": {"id": "TASK-2", "done": True},
                "status": status,
                "current_nodes": [{"node_id": "terminal"}],
                "pending_approvals": [],
                "live_sessions": [],
                "retained_session_count": 0,
            }
            write_state(fixture, state)
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_terminal_anchor_loss_blocks_source_and_target_graphs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            bad_target = {
                "version": 2,
                "nodes": [],
                "edges": [],
                "node_groups": [],
                "transition_groups": [],
            }
            plan, _ = canonical_plan(fixture, graph=bad_target)
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(plan, mode="prepare")
            state = read_state(fixture)
            state["workflows"][workflow_id(0)]["graph"]["nodes"] = []
            write_state(fixture, state)
            good_plan, _ = canonical_plan(fixture, name="good-plan.json")
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(good_plan, mode="prepare")

    def test_metadata_only_preserves_graph_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            metadata = {
                "name": "Metadata only",
                "description": "No graph mutation",
                "execution_target": "none",
            }
            plan, _ = canonical_plan(
                fixture,
                intent="metadata-only",
                metadata=metadata,
            )
            reconcile_canonical_workflows(plan, mode="prepare")
            report = reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            self.assertEqual(report["phase"], "complete")
            state = read_state(fixture)
            self.assertEqual(state["workflows"][workflow_id(0)]["version"], 1)
            self.assertEqual(state["workflows"][workflow_id(0)]["metadata"], metadata)
            self.assertEqual(graph_apply_rows(fixture), [])

    def test_forward_restore_uses_captured_preimage_and_fresh_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, value = canonical_plan(fixture)
            original_graph = json.loads(
                json.dumps(read_state(fixture)["workflows"][workflow_id(0)]["graph"])
            )
            reconcile_canonical_workflows(plan, mode="prepare")
            install_effect(
                fixture,
                plan,
                member_status="graph_verified",
                effect_status="verified",
            )
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            workflow["version"] = 2
            workflow["graph"] = {
                key: value["workflows"][0]["graph"][key]
                for key in ("nodes", "edges", "node_groups", "transition_groups")
            }
            write_state(fixture, state)
            report = reconcile_canonical_workflows(plan, mode="rollback", confirm=True)
            self.assertEqual(report["phase"], "rolled_back")
            state = read_state(fixture)
            workflow = state["workflows"][workflow_id(0)]
            self.assertEqual(workflow["version"], 3)
            self.assertEqual(workflow["graph"], original_graph)
            self.assertEqual(state["last_graph_document"]["expected_version"], 2)

    def test_prepared_rollback_has_no_effect_and_complete_requires_new_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_plan(fixture)
            reconcile_canonical_workflows(plan, mode="prepare")
            report = reconcile_canonical_workflows(plan, mode="rollback", confirm=True)
            self.assertEqual(report["phase"], "rolled_back")
            self.assertEqual(graph_apply_rows(fixture), [])
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_d9_fixture(Path(temporary))
            plan, _ = canonical_plan(fixture)
            reconcile_canonical_workflows(plan, mode="prepare")
            reconcile_canonical_workflows(plan, mode="apply", confirm=True)
            with self.assertRaises(JournalError):
                reconcile_canonical_workflows(plan, mode="rollback", confirm=True)


if __name__ == "__main__":
    unittest.main()
