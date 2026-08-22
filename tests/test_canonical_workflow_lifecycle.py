from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import workflowkit.operations as operations
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
    PROJECT_ID,
    make_d9_fixture,
    read_log,
    read_state,
    workflow_id,
    write_state,
)


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
            "execution_target": "ask_on_first_execution",
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


def install_effect(
    fixture: dict,
    plan: operations.LoadedPlan,
    *,
    member_status: str,
    effect_status: str,
) -> dict:
    parsed = operations._validate_canonical_plan(plan)
    wid = workflow_id(0)
    with OperationJournal(
        fixture["root"] / "canonical-state",
        "canonical-workflow-reconcile",
        plan,
    ) as journal:
        prepared = journal.state["preimage"][0]
        stage = operations._canonical_progress(parsed, parsed["workflows"][0], prepared)[0]
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
                    f"apply:{wid}:graph": {
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
                "execution_target": "local",
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
