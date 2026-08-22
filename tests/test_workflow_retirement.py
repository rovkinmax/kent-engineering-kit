from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workflowkit.operations import (
    OperationJournal,
    PlanValidationError,
    canonical_bytes,
    load_plan,
    recover_effect,
    retire_workflow_batch,
)


def plan_file(root: Path, value: dict) -> tuple[Path, str]:
    path = root / "plan.json"
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def base_plan(root: Path, *, terminal: bool = True) -> dict:
    kent = Path("/usr/bin/python3")
    return {
        "schema": "workflow-retirement-batch-plan-v1",
        "project_id": "123e4567-e89b-12d3-a456-426614174001",
        "state_dir": str(root / "state"),
        "kent": {
            "path": str(kent),
            "sha256": hashlib.sha256(kent.read_bytes()).hexdigest(),
        },
        "database": {
            "path": str(root / "kent.sqlite"),
            "schema": "sqlite",
            "project_root": str(root),
            "session_roots": [],
        },
        "members": [
            {
                "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
                "revision": "a" * 40,
                "tasks": [
                    {
                        "id": "TASK-1",
                        "terminal": terminal,
                        "current_node": None,
                        "approval_pending": False,
                    }
                ],
                "links": [],
                "sessions": [],
                "worktrees": [],
                "retained": [],
                "absent": [],
            }
        ],
    }


class WorkflowRetirementTest(unittest.TestCase):
    def test_nonterminal_task_blocks_before_any_read_or_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, digest = plan_file(root, base_plan(root, terminal=False))
            plan = load_plan(
                path,
                schema="workflow-retirement-batch-plan-v1",
                expected_sha256=digest,
            )
            with self.assertRaises(PlanValidationError):
                retire_workflow_batch(plan, mode="apply")
            self.assertFalse((root / "state").exists())

    def test_arbitrary_command_and_probe_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = base_plan(root)
            value["members"][0]["command"] = ["/bin/echo", "bad"]
            path, digest = plan_file(root, value)
            plan = load_plan(
                path,
                schema="workflow-retirement-batch-plan-v1",
                expected_sha256=digest,
            )
            with self.assertRaises(PlanValidationError):
                retire_workflow_batch(plan, mode="apply")

    def test_recovery_settles_preimage_without_signalling_a_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, digest = plan_file(root, base_plan(root))
            plan = load_plan(
                path,
                schema="workflow-retirement-batch-plan-v1",
                expected_sha256=digest,
            )
            with OperationJournal(root / "state", "workflow-retirement-batch", plan) as journal:
                journal.persist(
                    {
                        "phase": "in_progress",
                        "effects": {
                            "delete:x": {
                                "status": "attempted",
                                "child": {"guardian_pid": 999999, "child_pid": 999998},
                            }
                        },
                    }
                )
                self.assertEqual(
                    recover_effect(
                        journal,
                        effect_key="delete:x",
                        preimage_sha256="a" * 64,
                        postimage_sha256="b" * 64,
                        current_sha256=lambda: "a" * 64,
                    ),
                    "preimage",
                )


if __name__ == "__main__":
    unittest.main()
