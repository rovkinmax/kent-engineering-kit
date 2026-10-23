from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workflowkit.project_memory import ProjectMemory


ROOT = Path(__file__).resolve().parents[1]


class PlanAuditRawTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.memory_root = self.base / "memory"
        self.store = ProjectMemory.initialize(self.memory_root, "kit", "project-a")
        self.store.initialize_task("TASK-1", "task-a", [])
        self.contract = runpy.run_path(str(ROOT / "templates/project/workflow-plan-contract"))
        self.adapter = runpy.run_path(str(ROOT / "templates/project/workflow-project-memory"))

    def test_core_and_plan_contract_raw_comparators_conform(self) -> None:
        compare_core = self.contract["inspection_raw_comparison"]

        import workflowkit.project_memory as core

        cases = (
            (b"Same\n", b"Same\n", "exact"),
            (b"- [ ] one\n1. [ ] two\n## [ ] three\n",
             b"- [x] one\n1. [X] two\n## [x] three\n", "checkbox_progress"),
            (b"- [ ] one\n", b"- [ ] two\n", "changed"),
            (b"```text\n- [ ] literal\n```\n", b"```text\n- [x] literal\n```\n", "changed"),
            (b"    - [ ] literal\n", b"    - [x] literal\n", "changed"),
            (b"`open\n- [ ] literal\nclose`\n",
             b"`open\n- [x] literal\nclose`\n", "changed"),
            (b"``open ` in span\n- [ ] literal\nclose``\n",
             b"``open ` in span\n- [x] literal\nclose``\n", "changed"),
            (b"``open ` in span\n- [ ] literal\nclose``\n",
             b"``open ` in span\n- [x] literal\nclose``\n", "changed"),
            (b"<!--\n- [ ] literal\n-->\n", b"<!--\n- [x] literal\n-->\n", "changed"),
            (b"- [ ] task <!--\n- [ ] literal\n-->\n",
             b"- [ ] task <!--\n- [x] literal\n-->\n", "changed"),
            (b"<pre>\n\n- [ ] literal\n\n</pre>\n",
             b"<pre>\n\n- [x] literal\n\n</pre>\n", "changed"),
            (b"<Warning>\n- [ ] literal\n</Warning>\n",
             b"<Warning>\n- [x] literal\n</Warning>\n", "changed"),
            (b"</Warning>\n- [ ] literal\n",
             b"</Warning>\n- [x] literal\n", "changed"),
            (b"</Warning>\n- [ ] literal\n",
             b"</Warning>\n- [x] literal\n", "changed"),
            (b'<a href="foo">\n- [ ] literal\n</a>\n',
             b'<a href="foo">\n- [x] literal\n</a>\n', "changed"),
            (b":::\n- [ ] literal\n:::\n",
             b":::\n- [x] literal\n:::\n", "changed"),
            (b"---\n- [ ] frontmatter\n---\n",
             b"---\n- [x] frontmatter\n---\n", "changed"),
            (b"- [ ] one  \n", b"- [x] one\n", "changed"),
            (b"- [ ] one\r\n", b"- [x] one\n", "changed"),
            (b"- [ ] first\r- [ ] second\r",
             b"- [x] first\r- [X] second\r", "checkbox_progress"),
            (b"~~~\n- [ ] literal\n", b"~~~\n- [x] literal\n", "changed"),
        )
        for accepted, working, expected in cases:
            with self.subTest(expected=expected, accepted=accepted):
                self.assertEqual(core._plan_content_comparison(accepted, working), expected)
                self.assertEqual(compare_core(accepted, working), expected)

        for separator in (
            "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
        ):
            accepted = f"paragraph{separator}- [ ] literal\n".encode("utf-8")
            working = accepted.replace(b"[ ]", b"[x]")
            with self.subTest(separator=repr(separator)):
                self.assertEqual(
                    core._plan_content_comparison(accepted, working), "changed",
                )
                self.assertEqual(compare_core(accepted, working), "changed")

    def test_dense_code_spans_and_checklists_preserve_progress(self) -> None:
        import workflowkit.project_memory as core

        contents = "\n".join(
            [f"Inline `item-{index}` example." for index in range(700)]
            + [f"- [ ] Task {index}" for index in range(700)]
        ) + "\n"
        accepted = contents.encode()
        working = accepted.replace(b"[ ]", b"[x]")

        self.assertEqual(
            core._plan_content_comparison(accepted, working),
            "checkbox_progress",
        )
        self.assertEqual(
            self.contract["inspection_raw_comparison"](accepted, working),
            "checkbox_progress",
        )

    def test_r2_plan_check_detects_literal_checkbox_drift_hidden_by_history_normalization(self) -> None:
        original = b"Reviewed example:\n```text\n- [ ] literal\n```\n"
        changed = b"Reviewed example:\n```text\n- [x] literal\n```\n"
        first = self.store.publish("tasks/TASK-1/plan", None, original.decode())
        self.store.accept_plan("TASK-1", first.token, None, {
            "work_kind": "feature", "authority_refs": ["session_message(p/s/e/d)"],
        })
        second = self.store.publish(
            "tasks/TASK-1/plan", first.token, changed.decode(),
        )

        status = self.store.check_plan("TASK-1")

        self.assertNotEqual(
            hashlib.sha256(original).hexdigest(),
            hashlib.sha256(changed).hexdigest(),
        )
        self.assertEqual(
            hashlib.sha256(status["accepted"]["normalized_plan"].encode()).hexdigest(),
            status["accepted"]["normalized_sha256"],
        )
        self.assertEqual(status["working_revision"], second.token)
        self.assertEqual(status["raw_comparison"], "changed")
        self.assertEqual(status["comparison"], "changed")

    def test_resolve_plan_exposes_verified_immutable_subject_and_progress_comparison(self) -> None:
        reviewed = self.store.publish("tasks/TASK-1/plan", None, "- [ ] reviewed\n")
        working = self.store.publish(
            "tasks/TASK-1/plan", reviewed.token, "- [x] reviewed\n",
        )
        request = {
            "schema": "r2-workflow-request-v1", "operation": "resolve-plan",
            "workspace_path": str(self.base), "plan_path": str(reviewed.path),
        }
        with patch.dict(self.adapter["workflow_dispatch"].__globals__, {
            "resolve_native": lambda *_args, **_kwargs: (self.store, "TASK-1", None),
        }):
            result = self.adapter["workflow_dispatch"](
                __import__("workflowkit.project_memory", fromlist=["ProjectMemory"]),
                request,
            )

        self.assertEqual(result["supplied_revision"], reviewed.token)
        self.assertEqual(result["working_revision"], working.token)
        self.assertEqual(result["supplied_sha256"], reviewed.sha256)
        self.assertEqual(result["working_sha256"], working.sha256)
        self.assertEqual(result["supplied_comparison"], "checkbox_progress")

    def test_plan_acceptance_revalidates_when_working_content_drifted_from_reviewed_revision(self) -> None:
        root = self.base / "r2-workspace"
        (root / ".kent").mkdir(parents=True)
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        (root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ncoder_selection = "disabled"\n'
            '[project_memory]\nmode = "r2"\n')
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        reviewed = self.store.publish(
            "tasks/TASK-1/plan", None,
            "Reviewed details:\n```text\n- [ ] literal\n```\n",
        )
        working = self.store.publish(
            "tasks/TASK-1/plan", reviewed.token,
            "Reviewed details:\n```text\n- [x] literal\n```\n",
        )
        operations: list[str] = []

        def memory_call(_root, _payload, operation, **_fields):
            operations.append(operation)
            if operation == "resolve-task":
                return {"mode": "r2"}
            if operation == "accepted-head":
                return {"accepted_snapshot": None}
            if operation == "resolve-plan":
                return {
                    "supplied_revision": reviewed.token,
                    "supplied_sha256": reviewed.sha256,
                    "supplied_comparison": "changed",
                    "working_revision": working.token,
                    "working_path": str(working.path),
                    "working_sha256": working.sha256,
                }
            raise AssertionError(f"unexpected memory operation: {operation}")

        payload = {
            "workspace_path": str(root), "task_short_id": "TASK-1",
            "plan_path": str(reviewed.path), "work_kind": "feature",
            "review_context": "Reviewed session_message(project/session/event/digest)",
            "plan_route": "continue", "plan_route_context": "not-applicable",
        }
        with (
            patch.dict(self.contract["execute"].__globals__, {
                "memory_call": memory_call,
            }),
            patch.dict(os.environ, {
                "KENT_PLAN_CONTRACT_MODE": "accept",
                "KENT_PLAN_CONTRACT_STABLE_TRANSITION": "stable",
                "KENT_PLAN_CONTRACT_CHANGED_TRANSITION": "changed",
            }),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.contract["execute"](payload), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "plan_contract_revalidate")
        self.assertIn("reviewed", result["plan_change_report"].lower())
        self.assertNotIn("accept-plan", operations)
        self.assertFalse((root / ".kent/runtime/TASK-1/plan-contract.json").exists())

    def test_r2_acceptance_reuses_review_for_proven_checkbox_progress(self) -> None:
        root = self.base / "r2-checkbox-progress"
        (root / ".kent").mkdir(parents=True)
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        (root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ncoder_selection = "disabled"\n'
            '[project_memory]\nmode = "r2"\n')
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        reviewed = self.store.publish(
            "tasks/TASK-1/plan", None,
            "- [ ] Deliver\nsession_message(project/session/event/digest)\n",
        )
        working = self.store.publish(
            "tasks/TASK-1/plan", reviewed.token,
            "- [x] Deliver\nsession_message(project/session/event/digest)\n",
        )
        calls: list[str] = []
        task_record = self.store._task("TASK-1")
        binding_sha256 = hashlib.sha256(
            (self.store.path / "tasks/TASK-1/task.json").read_bytes(),
        ).hexdigest()

        def memory_call(_root, _payload, operation, **fields):
            calls.append(operation)
            if operation == "resolve-task":
                return {"mode": "r2"}
            if operation == "accepted-head":
                return {"accepted_snapshot": self.store.accepted_head("TASK-1")}
            if operation == "resolve-plan":
                comparison = self.store.compare_plan_revisions(
                    "TASK-1", reviewed.token, working.token,
                )
                return {
                    "supplied_revision": reviewed.token,
                    "supplied_sha256": comparison["supplied_sha256"],
                    "supplied_comparison": comparison["comparison"],
                    "working_revision": working.token,
                    "working_path": str(working.path),
                    "working_sha256": comparison["working_sha256"],
                }
            if operation == "accept-plan":
                accepted = self.store.accept_plan(
                    "TASK-1", fields["expected_revision"],
                    fields["expected_accepted"], fields["metadata"],
                )
                return {"token": accepted.token}
            if operation == "check-plan":
                status = self.store.check_plan(
                    "TASK-1", coder_selection=fields["coder_selection"],
                )
                status["cache_ref"] = {
                    "schema": "r2-plan-contract-cache-v1",
                    "task_short_id": "TASK-1",
                    "project_uid": "project-a",
                    "task_uid": task_record["task_uid"],
                    "task_binding_sha256": binding_sha256,
                    "accepted_snapshot_path": status["accepted_path"],
                    "accepted_snapshot_sha256": status["accepted_snapshot"],
                }
                return status
            raise AssertionError(f"unexpected memory operation: {operation}")

        payload = {
            "workspace_path": str(root), "task_short_id": "TASK-1",
            "plan_path": str(reviewed.path), "work_kind": "feature",
            "review_context": "Reviewed session_message(project/session/event/digest)",
            "plan_route": "start", "plan_route_context": "not-applicable",
        }
        with (
            patch.dict(self.contract["execute"].__globals__, {"memory_call": memory_call}),
            patch.dict(os.environ, {"KENT_PLAN_CONTRACT_MODE": "accept"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.contract["execute"](payload), 0)

        self.assertEqual(json.loads(output.getvalue())["transition"], "plan_contract_start")
        self.assertEqual(self.store.check_plan("TASK-1")["comparison"], "unchanged")
        self.assertIn("accept-plan", calls)

    def test_new_legacy_acceptance_records_raw_plan_for_checkbox_progress(self) -> None:
        root = self.base / "legacy-new-snapshot"
        (root / ".kent").mkdir(parents=True)
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        (root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ncoder_selection = "disabled"\n')
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        plan = root / ".todo/TASK-1/plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_bytes(b"- [ ] Deliver\n")
        payload = {
            "workspace_path": str(root), "task_short_id": "TASK-1",
            "plan_path": str(plan), "work_kind": "feature",
            "review_context": "Reviewed",
            "plan_route": "start", "plan_route_context": "not-applicable",
        }
        with (
            patch.dict(os.environ, {"KENT_PLAN_CONTRACT_MODE": "accept"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.contract["execute"](payload), 0)
        self.assertEqual(json.loads(output.getvalue())["transition"], "plan_contract_start")
        cache = root / ".kent/runtime/TASK-1/plan-contract.json"
        snapshot = json.loads(cache.read_text())
        self.assertEqual(snapshot["raw_plan"], "- [ ] Deliver\n")
        self.assertEqual(snapshot["raw_sha256"], hashlib.sha256(plan.read_bytes()).hexdigest())

        plan.write_bytes(b"- [x] Deliver\n")
        with (
            patch.dict(os.environ, {
                "KENT_PLAN_CONTRACT_MODE": "check",
                "KENT_PLAN_CONTRACT_ROUTE": "continue",
                "KENT_PLAN_CONTRACT_STABLE_TRANSITION": "stable",
                "KENT_PLAN_CONTRACT_CHANGED_TRANSITION": "changed",
            }),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.contract["execute"]({
                "workspace_path": str(root), "task_short_id": "TASK-1",
                "review_context": "Continue",
            }), 0)
        self.assertEqual(json.loads(output.getvalue())["transition"], "stable")

    def test_legacy_normalized_only_snapshot_is_unknown_not_stable(self) -> None:
        root = self.base / "legacy"
        (root / ".kent/runtime/TASK-1").mkdir(parents=True)
        (root / ".kent/workflow-profile.toml").parent.mkdir(parents=True, exist_ok=True)
        (root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ncoder_selection = "disabled"\n')
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        plan = root / ".todo/TASK-1/plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("Reviewed details:\n```text\n- [x] literal\n```\n")
        normalized = self.contract["normalized_plan"](plan)
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        cache = root / ".kent/runtime/TASK-1/plan-contract.json"
        cache.write_text(json.dumps({
            "schema_version": 1, "task_short_id": "TASK-1",
            "plan_path": ".todo/TASK-1/plan.md", "work_kind": "feature",
            "normalized_sha256": digest, "normalized_plan": normalized,
        }))
        payload = {
            "workspace_path": str(root), "task_short_id": "TASK-1",
            "review_context": "Reviewed",
        }
        with (
            patch.dict(os.environ, {
                "KENT_PLAN_CONTRACT_MODE": "check",
                "KENT_PLAN_CONTRACT_ROUTE": "continue",
                "KENT_PLAN_CONTRACT_STABLE_TRANSITION": "stable",
                "KENT_PLAN_CONTRACT_CHANGED_TRANSITION": "changed",
            }),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.contract["execute"](payload), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "changed")
        self.assertIn("raw", result["plan_change_report"].lower())

    def test_stable_resume_checks_terminal_ownership_but_inspection_does_not(self) -> None:
        root = self.base / "terminal"
        (root / ".kent/runtime/TASK-1").mkdir(parents=True)
        (root / ".kent/workflow-profile.toml").parent.mkdir(parents=True, exist_ok=True)
        (root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ncoder_selection = "disabled"\n')
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        plan = root / ".todo/TASK-1/plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("- [ ] task\n")
        normalized = self.contract["normalized_plan"](plan)
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        cache = root / ".kent/runtime/TASK-1/plan-contract.json"
        cache.write_text(json.dumps({
            "schema_version": 1, "task_short_id": "TASK-1",
            "plan_path": ".todo/TASK-1/plan.md", "work_kind": "feature",
            "normalized_sha256": digest, "normalized_plan": normalized,
            "raw_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
            "raw_plan": plan.read_text(),
        }))
        preparation = root / "build/kent-workflow/TASK-1/cleanup-preparation.json"
        preparation.parent.mkdir(parents=True)
        preparation.write_text('{"state":"prepared"}')
        payload = {
            "workspace_path": str(root), "task_short_id": "TASK-1",
            "review_context": "Reviewed",
        }

        with (
            patch.dict(os.environ, {
                "KENT_PLAN_CONTRACT_MODE": "check",
                "KENT_PLAN_CONTRACT_ROUTE": "continue",
                "KENT_PLAN_CONTRACT_STABLE_TRANSITION": "stable",
                "KENT_PLAN_CONTRACT_CHANGED_TRANSITION": "changed",
            }),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "terminal|acceptance is closed"),
        ):
            self.contract["execute"](payload)

        with patch.dict(os.environ, {"KENT_PLAN_CONTRACT_MODE": "inspect"}), \
                patch.dict(self.contract["execute"].__globals__, {
                    "inspect_workflow_context": lambda _payload: {"plan_comparison": "unchanged"},
                }), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.contract["execute"]({}), 0)
        self.assertEqual(json.loads(output.getvalue()), {"plan_comparison": "unchanged"})

    def test_missing_r2_head_with_any_local_cache_blocks_before_writes(self) -> None:
        root = self.base / "r2-missing-head"
        (root / ".kent/runtime/TASK-1").mkdir(parents=True)
        (root / ".kent/workflow-profile.toml").parent.mkdir(parents=True, exist_ok=True)
        (root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ncoder_selection = "disabled"\n'
            '[project_memory]\nmode = "r2"\n')
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        cache = root / ".kent/runtime/TASK-1/plan-contract.json"
        cache.write_bytes(b"conflicting cache bytes\n")
        before = cache.read_bytes()
        calls: list[str] = []

        def memory_call(_root, _payload, operation, **_fields):
            calls.append(operation)
            if operation == "resolve-task":
                return {"mode": "r2"}
            if operation == "accepted-head":
                return {"accepted_snapshot": None}
            raise AssertionError(f"write attempted before conflict detection: {operation}")

        payload = {
            "workspace_path": str(root), "task_short_id": "TASK-1",
            "plan_path": "not-applicable", "work_kind": "feature",
            "review_context": "Reviewed",
            "plan_route": "start", "plan_route_context": "not-applicable",
        }
        with (
            patch.dict(self.contract["execute"].__globals__, {
                "memory_call": memory_call,
            }),
            patch.dict(os.environ, {"KENT_PLAN_CONTRACT_MODE": "accept"}),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "cache|head"),
        ):
            self.contract["execute"](payload)

        self.assertEqual(calls, ["resolve-task", "accepted-head"])
        self.assertEqual(cache.read_bytes(), before)
        self.assertFalse((self.store.path / "tasks/TASK-1/plan").exists())


if __name__ == "__main__":
    unittest.main()
