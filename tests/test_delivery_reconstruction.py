from __future__ import annotations

import hashlib
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workflowkit.delivery import (
    capture_producer,
    pack_delivery,
    producer_locator,
    producer_reference_closure,
    reconstruct_producer,
    transition_contract_reference,
)

class ContextInspectionTests(unittest.TestCase):
    def test_memory_call_deadline_covers_source_inspection_budget(self):
        call = self.module["memory_call"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "workflow-project-memory"
            helper.write_text("fixture")

            def invoke(*args, **kwargs):
                self.assertEqual(kwargs["timeout"], 600)
                kwargs["stdout"].write(
                    b'{"schema":"r2-workflow-response-v1","result":{"mode":"r2"}}')
                return subprocess.CompletedProcess(args, 0)

            with patch.dict(call.__globals__, {"__file__": str(root / "workflow-plan-contract")}), \
                    patch("subprocess.run", side_effect=invoke):
                self.assertEqual(call(root, {}, "resolve-task"), {"mode": "r2"})

    def test_clean_session_cases_have_explicit_inputs_and_acceptance(self):
        fixture = Path(__file__).with_name("fixtures") / "delivery-reliability-cases.json"
        cases = json.loads(fixture.read_text())
        self.assertEqual(cases["schema"], "delivery-reliability-cases-v1")
        self.assertIn("not historical evidence", cases["purpose"])
        expected = {
            "decision-through-summary", "budget-is-not-replenished",
            "producer-transport", "recovered-gap", "external-memory",
            "provider-boundary", "means-versus-obligations"}
        self.assertEqual({case["id"] for case in cases["cases"]}, expected)
        self.assertEqual(len(cases["cases"]), len(expected))
        for case in cases["cases"]:
            self.assertEqual(set(case), {"id", "input", "required", "reject"})
            self.assertTrue(case["input"])
            for field in ("required", "reject"):
                self.assertTrue(case[field])
                self.assertTrue(all(isinstance(item, str) and item.strip()
                                    for item in case[field]))

    def setUp(self):
        self.module = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                        "templates/project/workflow-plan-contract"))

    def test_summary_pointer_and_narrative_remain_discoverable_not_authorized(self):
        candidates, unresolved, evidence = [], [], []
        self.module["inspection_add_records"](
            [{"sequence": 1, "event_hash": "a" * 64,
              "summary": "Human decision: question_answer(session/call/proposal+answer/digest)",
              "decisions": ["task_comment(project/task/comment/digest)"],
              "checks": ["session_message(project/session/event/digest)"],
              "artifacts": []}],
            path=Path("/workspace/ledger.jsonl"), candidates=candidates,
            evidence_locators=evidence, unresolved=unresolved)
        self.assertEqual({item["kind"] for item in candidates},
                         {"question_answer", "task_comment", "session_message"})
        self.assertTrue(all(item["verification"] == "unverified_candidate" for item in candidates))
        self.assertTrue(any("field=summary" in item["locator"] for item in unresolved))

    def test_acceptance_recovers_summary_locators_without_retyping_them(self):
        recover = self.module["acceptance_references"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / ".kent/runtime/TASK-123/evidence-ledger.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text("read only through the validated logical view")
            reference = "question_answer(session/call/proposal+answer/digest)"
            records = [{"sequence": 1, "event_hash": "a" * 64,
                        "summary": reference, "decisions": [], "checks": [], "artifacts": []}]
            with patch.dict(recover.__globals__, {
                    "inspection_evidence_view": lambda *args, **kwargs: (
                        records, "recovered_with_gap")}):
                self.assertEqual(recover(root, "TASK-123", ("reviewed", "Plan")), [reference])
                self.assertEqual(recover(root, "TASK-123", (reference, reference)), [reference])
            with patch.dict(recover.__globals__, {
                    "inspection_evidence_view": unittest.mock.Mock(
                        side_effect=ValueError("invalid evidence"))}):
                with self.assertRaisesRegex(ValueError, "invalid evidence"):
                    recover(root, "TASK-123", ("reviewed", "Plan"))

    def test_inspect_mode_report_only_is_read_only_and_incomplete_without_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            request = {
                "schema": "workflow-context-inspection-request-v1",
                "workspace_path": str(root), "task_short_id": "TASK-1",
                "plan_path": "not-applicable", "evidence_refs": [],
            }
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] /
                                     "templates/project/workflow-plan-contract")],
                input=json.dumps(request), text=True, capture_output=True,
                env={**os.environ, "KENT_PLAN_CONTRACT_MODE": "inspect"})
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertFalse(output["discovery_complete"])
            self.assertTrue(output["authority_verification_required"])
            self.assertFalse((root / ".kent").exists())
            self.assertFalse((root / ".todo").exists())


class ProducerTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.identity = {
            "task_id": "native-task", "task_short_id": "KIT-1",
            "project_id": "native-project", "workspace_path": str(self.root),
            "session_id": "session", "run_id": "run", "step_id": "step",
        }
        self.outer = ' { "schema": "workflow-delivery-context-v1", "phase":"pre_pr" } \n'
        self.result = {
            "transition": "failed", "delivery_context": self.outer,
            "ci_report": ' {\n"status": "failed", "head":"old"\n} ',
            "commentary": "Original report.\n",
        }

    def capture(self, **kwargs):
        return capture_producer(
            self.root, identity=self.identity, producer_kind="test",
            inputs={"delivery_context": self.outer},
            result=kwargs.pop("result", self.result), **kwargs,
        )

    def read(self, locator):
        return reconstruct_producer(self.root, locator, identity=self.identity)

    def test_retention_closure_keeps_exact_original_and_packed_bytes(self):
        original, first = self.capture()
        packed, second = self.capture(binding={
            "producer_refs": json.dumps({"refs": [original]})})
        expected = {
            original, packed,
            str(Path(original).with_name("emitted-" + hashlib.sha256(first).hexdigest() + ".json")),
            str(Path(packed).with_name("emitted-" + hashlib.sha256(second).hexdigest() + ".json")),
        }
        self.assertEqual(set(producer_reference_closure(
            self.root, packed, identity=self.identity)), expected)
        (self.root / original).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            producer_reference_closure(self.root, packed, identity=self.identity)

    def test_exact_strings_and_historical_failure_survive_resume(self):
        locator, emitted = self.capture()
        self.identity["step_id"] = "later-step"
        self.identity["run_id"] = "later-run"
        recovered = self.read(locator)
        self.assertEqual(recovered["emitted"], emitted)
        self.assertEqual(recovered["result"]["ci_report"], self.result["ci_report"])
        self.assertEqual(recovered["result"]["delivery_context"], self.outer)
        self.assertEqual(json.loads(emitted)["transition"], "failed")
        self.assertEqual(self.capture()[1], self.capture()[1])

    def test_double_footer_and_nonstring_and_control_values_fail(self):
        for extra in (
            {"commentary": "KENT_PACKET_SOURCE_V1 unsafe"},
            {"ci_report": {}}, {"task_id": "foreign"}, {"target_node": "skip"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.capture(result={**self.result, **extra})

    def test_digest_corruption_and_missing_emitted_fail(self):
        locator, emitted = self.capture()
        base = self.root / locator
        original = base.read_bytes()
        base.write_bytes(original + b" ")
        with self.assertRaises(ValueError):
            self.read(locator)
        base.write_bytes(original)
        emitted_path = base.parent / ("emitted-" + hashlib.sha256(emitted).hexdigest() + ".json")
        emitted_path.unlink()
        with self.assertRaises(ValueError):
            self.read(locator)

    def test_paths_and_foreign_identity_rejected(self):
        locator, _ = self.capture()
        for path in ("../" + locator, "/" + locator, locator.replace("/base-", "/../base-")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.read(path)
        for key in ("task_id", "project_id", "task_short_id", "workspace_path"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                reconstruct_producer(self.root, locator, identity={**self.identity, key: "foreign"})

    def test_private_no_symlink_no_hardlink_files(self):
        locator, _ = self.capture()
        base = self.root / locator
        self.assertEqual(base.stat().st_mode & 0o777, 0o600)
        self.assertEqual(base.parent.stat().st_mode & 0o777, 0o700)
        linked = base.with_name("extra")
        os.link(base, linked)
        with self.assertRaises(ValueError):
            self.read(locator)
        linked.unlink()
        original = base.read_bytes()
        base.unlink()
        target = self.root / "target"
        target.write_bytes(original)
        base.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            self.read(locator)

    def test_crash_after_base_before_emit_is_not_success(self):
        import workflowkit.delivery as delivery
        original = delivery._packet_publish
        calls = []

        def crash(fd, name, raw):
            calls.append(name)
            if name.startswith("emitted-"):
                raise OSError("simulated crash")
            return original(fd, name, raw)

        with patch.object(delivery, "_packet_publish", side_effect=crash):
            with self.assertRaises(OSError):
                self.capture()
        locator = "build/kent-workflow/KIT-1/packets/" + calls[0]
        with self.assertRaises(ValueError):
            self.read(locator)
        self.assertEqual(self.read(self.capture()[0])["result"]["ci_report"], self.result["ci_report"])

    def test_actual_process_death_during_immutable_publication_is_retryable(self):
        import workflowkit.delivery as delivery
        for boundary in ("before_link", "after_link"):
            identity = {**self.identity, "step_id": boundary}
            child = (
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "from unittest.mock import patch\n"
                "from workflowkit.delivery import capture_producer\n"
                "data = json.loads(sys.argv[1])\n"
                "link = os.link\n"
                "def crash(*args, **kwargs):\n"
                "    if data['boundary'] == 'after_link': link(*args, **kwargs)\n"
                "    os._exit(73)\n"
                "with patch('workflowkit.delivery.os.link', side_effect=crash):\n"
                "    capture_producer(Path(data['root']), identity=data['identity'], "
                "producer_kind='test', inputs={}, result=data['result'])\n"
            )
            data = {"boundary": boundary, "identity": identity,
                    "root": str(self.root), "result": self.result}
            process = subprocess.run([sys.executable, "-c", child, json.dumps(data)],
                                     capture_output=True, check=False)
            self.assertEqual(process.returncode, 73, process.stderr)
            locator, raw = delivery.capture_producer(
                self.root, identity=identity, producer_kind="test",
                inputs={}, result=self.result,
            )
            self.assertEqual(delivery.reconstruct_producer(
                self.root, locator, identity=identity,
            )["emitted"], raw)

    def test_corrupt_emitted_and_unsafe_directory_are_rejected(self):
        locator, emitted = self.capture()
        path = self.root / locator
        emitted_path = path.parent / ("emitted-" + hashlib.sha256(emitted).hexdigest() + ".json")
        emitted_path.write_bytes(b"{}")
        with self.assertRaises(ValueError):
            self.read(locator)
        path.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            self.read(locator)

    def test_conflicting_immutable_content_and_limits_rejected(self):
        locator, _ = self.capture()
        (self.root / locator).write_bytes(b"foreign")
        with self.assertRaises(ValueError):
            self.capture()
        with self.assertRaises(ValueError):
            self.capture(result={"commentary": "x" * (1024 * 1024 + 1)})

    def test_footer_is_strict_and_legacy_is_distinct(self):
        locator, emitted = self.capture()
        commentary = json.loads(emitted)["commentary"]
        self.assertEqual(producer_locator(commentary), locator)
        self.assertIsNone(producer_locator("legacy report"))
        for text in (commentary + "\n" + commentary, "prefix KENT_PACKET_SOURCE_V1 " + locator,
                     "KENT_PACKET_SOURCE_V1 ../foreign"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                producer_locator(text)

    def graph(self):
        return {
            "workflow_id": "workflow", "expected_version": 1,
            "graph": {
                "nodes": [{"id": "source", "key": "gate", "kind": "agent"}],
                "transition_groups": [{
                    "id": "group", "source_node_id": "source",
                    "transition_id": "failed",
                }],
                "edges": [{
                    "id": "edge", "key": "repair", "transition_group_id": "group",
                    "target_node_id": "fix", "parameters": [
                        {"key": key, "description": key, "purpose": "ordinary"}
                        for key in ("delivery_context", "ci_report")
                    ],
                }],
            },
        }

    def detail(self):
        return {
            "summary": {"id": "native-task", "project_id": "native-project",
                        "workflow_id": "workflow", "short_id": "KIT-1"},
            "current_nodes": [{"node_id": "source", "session_id": "session"}],
        }

    def request(self, locator):
        return {
            "schema": "workflow-pack-delivery-v1", "transition": "failed",
            "values": {}, "delivery_mode": "preserve",
            "producer_refs": [locator],
            "transition_contract_ref": transition_contract_reference(
                self.identity, self.detail(), self.graph(), "failed",
            ),
        }

    def pack(self, request, graph=None, detail=None):
        return pack_delivery(
            self.root, request, identity=self.identity,
            task_detail=detail or self.detail(), graph=graph or self.graph(),
        )

    def test_pack_exact_source_fields_and_contract_preserving_refresh(self):
        locator, _ = self.capture()
        request = self.request(locator)
        refreshed = self.graph()
        refreshed["expected_version"] = 2
        _, raw = self.pack(request, graph=refreshed)
        value = json.loads(raw)
        self.assertEqual(value["delivery_context"], self.outer)
        self.assertEqual(value["ci_report"], self.result["ci_report"])
        self.assertEqual(value["transition"], "failed")
        self.assertNotIn("parameters", value)
        changed = self.graph()
        changed["graph"]["edges"][0]["target_node_id"] = "wrong"
        with self.assertRaises(ValueError):
            self.pack(request, graph=changed)

    def test_pack_rejects_overrides_forged_packets_and_foreign_contract(self):
        locator, _ = self.capture()
        request = self.request(locator)
        for extra in ({"ci_report": "rewritten"}, {"force": "yes"},
                      {"ci_report": {}}, {"session_id": "foreign"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.pack({**request, "values": extra})
        detail = self.detail()
        detail["current_nodes"][0]["node_id"] = "other"
        with self.assertRaises(ValueError):
            self.pack(request, detail=detail)
        with self.assertRaises(ValueError):
            self.pack({**request, "producer_refs": []})

    def test_post_pr_assembly_preserves_exact_nested_strings(self):
        from workflowkit.runtime import canonical_bytes, make_dynamic_pr_feedback_cursor
        cursor = canonical_bytes(make_dynamic_pr_feedback_cursor(
            repository="owner/repo", pull_number=7, items=[],
        )).decode("utf-8")
        contract = ' { "historical": "contract" }\n'
        fields = {
            "pr_url": "https://github.com/owner/repo/pull/7",
            "branch_name": "feat/exact", "merge_strategy": "rebase",
            "pr_feedback_cursor": cursor, "ci_contract": contract,
            "ci_report": self.result["ci_report"],
        }
        locator, _ = capture_producer(
            self.root, identity=self.identity, producer_kind="pr-watch",
            inputs={}, result={"transition": "failed", **fields},
        )
        request = {**self.request(locator), "delivery_mode": "post_pr"}
        _, raw = self.pack(request)
        outer = json.loads(json.loads(raw)["delivery_context"])
        self.assertEqual(outer["ci_contract"], contract)
        self.assertEqual(outer["ci_report"], self.result["ci_report"])
        self.assertEqual(outer["pr_feedback_cursor"], cursor)
        self.assertEqual(outer["phase"], "post_pr")

    def test_duplicate_native_graph_identities_and_undeclared_values_fail(self):
        locator, _ = self.capture()
        request = self.request(locator)
        for key in ("nodes", "edges", "transition_groups"):
            graph = self.graph()
            graph["graph"][key].append(dict(graph["graph"][key][0]))
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.pack(request, graph=graph)
        with self.assertRaises(ValueError):
            self.pack({**request, "values": {"undeclared": "x"}})

    def test_symlink_directory_and_unsafe_parent_fail_without_writes(self):
        target = self.root / "outside"
        target.mkdir()
        (self.root / "build").symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            self.capture()
        self.assertEqual(list(target.iterdir()), [])
        (self.root / "build").unlink()
        (self.root / "build").mkdir(mode=0o777)
        (self.root / "build").chmod(0o777)
        with self.assertRaises(ValueError):
            self.capture()

    def test_duplicate_json_and_nonfinite_rejected(self):
        from workflowkit.delivery import packet_decode
        for raw in (b'{"field":"one","field":"two"}', b'{"field":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                packet_decode(raw)


if __name__ == "__main__":
    unittest.main()
