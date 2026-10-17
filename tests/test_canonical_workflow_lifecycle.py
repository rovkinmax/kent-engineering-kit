from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import runpy
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


def create_verified_source_fixture(
    testcase: unittest.TestCase,
    project_id: str,
    workflow_id_value: str,
) -> tuple[DeliverySourceAdmissionTests, operations.VerifiedSourceAdmission]:
    """Create an offline native/install/source fixture and verify it end to end."""
    fixture = DeliverySourceAdmissionTests(
        "test_published_closure_and_cli_are_read_only"
    )
    fixture.setUp()
    testcase.addCleanup(fixture.doCleanups)
    fixture.native_adapter_fixture()

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
    detail = workflow["details"]["TASK-1"]
    detail["live_sessions"] = []
    detail["current_nodes"] = [{"node_id": "node-terminal"}]
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
        ],
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


def install_v2_unresolved_graph_postimage(
    fixture: dict,
    plan: operations.LoadedPlan,
) -> dict:
    parsed = operations._validate_canonical_v2_plan(plan)
    item = parsed["workflows"][0]
    item["graph"] = item["target_graph"]
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

    def test_source_admission_rejects_duplicate_native_task_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
                "ingress-suffix-changed": changed(lambda graph: graph["edges"][0].update(
                    prompt_template=graph["edges"][0]["prompt_template"].replace(
                        "\n\nBegin plan revalidation.",
                        "\n\nUnreviewed prompt change.",
                    )
                )),
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
