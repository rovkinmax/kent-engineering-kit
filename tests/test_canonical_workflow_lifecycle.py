from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workflowkit.operations import (
    EffectBlocked,
    PlanValidationError,
    canonical_bytes,
    load_plan,
    reconcile_canonical_workflows,
)


def plan_file(root: Path, workflow: dict) -> tuple[Path, str]:
    kent = Path("/usr/bin/python3")
    value = {
        "schema": "canonical-workflow-reconcile-plan-v1",
        "state_dir": str(root / "state"),
        "kent": {
            "path": str(kent),
            "sha256": hashlib.sha256(kent.read_bytes()).hexdigest(),
        },
        "workflows": [workflow],
    }
    path = root / "plan.json"
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


class CanonicalWorkflowLifecycleTest(unittest.TestCase):
    def test_nonterminal_task_is_rejected_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = {
                "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
                "intent": "graph-only",
                "tasks": [{"id": "TASK-1", "terminal": False}],
                "current_nodes": [],
                "pending_approvals": [],
                "graph": {"nodes": []},
                "metadata": {},
                "allow_create": False,
            }
            path, digest = plan_file(root, workflow)
            plan = load_plan(
                path,
                schema="canonical-workflow-reconcile-plan-v1",
                expected_sha256=digest,
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(plan, mode="prepare")
            self.assertFalse((root / "state").exists())

    def test_raw_phase_command_is_not_a_typed_canonical_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = {
                "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
                "intent": "graph-only",
                "tasks": [],
                "graph": {"nodes": []},
                "metadata": {},
                "allow_create": False,
                "command": ["/bin/echo", "bad"],
            }
            path, digest = plan_file(root, workflow)
            plan = load_plan(
                path,
                schema="canonical-workflow-reconcile-plan-v1",
                expected_sha256=digest,
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(plan, mode="prepare")

    def test_opaque_preimage_and_rollback_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = {
                "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
                "project_id": "123e4567-e89b-12d3-a456-426614174001",
                "intent": "graph-only",
                "expected_version": 1,
                "terminal_tasks": [],
                "terminal_anchors": [],
                "links": [],
                "default": None,
                "graph": {"version": 2, "nodes": [], "edges": []},
                "preimage": {},
            }
            path, digest = plan_file(root, workflow)
            plan = load_plan(path, schema="canonical-workflow-reconcile-plan-v1", expected_sha256=digest)
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(plan, mode="prepare")

    def test_missing_d9_dependency_blocks_before_live_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = {
                "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
                "project_id": "123e4567-e89b-12d3-a456-426614174001",
                "intent": "graph-only",
                "expected_version": 1,
                "terminal_tasks": [],
                "terminal_anchors": [],
                "links": [],
                "default": None,
                "graph": {"version": 2, "nodes": [], "edges": []},
            }
            path, digest = plan_file(root, workflow)
            value = json.loads(path.read_text())
            value["d9"] = {
                "none": False,
                "path": str(root / "missing.journal"),
                "sha256": "a" * 64,
                "operation": "workflow-retirement-batch",
                "phase": "complete",
                "members": [workflow["workflow_id"]],
            }
            raw = canonical_bytes(value)
            path.write_bytes(raw)
            plan = load_plan(path, schema="canonical-workflow-reconcile-plan-v1", expected_sha256=hashlib.sha256(raw).hexdigest())
            with self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(plan, mode="prepare")
            self.assertFalse((root / "state").exists())


if __name__ == "__main__":
    unittest.main()
