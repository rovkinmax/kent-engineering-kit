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
    OperationJournal,
    PlanValidationError,
    canonical_bytes,
    canonical_sha256,
    load_plan,
    reconcile_canonical_workflows,
)
from tests.test_workflow_retirement import (
    PROJECT_ID as RETIREMENT_PROJECT_ID,
    make_workflow_fixture as make_base_workflow_fixture,
    read_log,
    read_state,
    workflow_id as legacy_workflow_id,
    write_state,
)
from tests import test_delivery_source_admission as source_admission

PROJECT_ID = "project-" + RETIREMENT_PROJECT_ID


def workflow_id(index: int) -> str:
    """Use canonical UUIDv4 native identities in this protocol test suite."""
    return f"123e4567-e89b-42d3-a456-{426614174000 + index:012d}"


class NativeGraphTransportTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("KENT_NATIVE_SOURCE_GATE_REQUEST"), "private gate request not supplied")
    def test_captured_puber_gate_only_ingress_survives_native_encoding(self):
        from workflowkit.delivery import source_admission_gate
        from workflowkit.graph import native_graph_postimage

        raw = Path(os.environ["KENT_NATIVE_SOURCE_GATE_REQUEST"]).read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "9704afcc9bb28264aaa83d7af523b558aa316179c96977dcf4997eef8d6fccd0",
        )
        request = json.loads(raw)
        gate = source_admission_gate(
            request["source_admission"], project_id=request["project_id"],
            workflow_id=request["workflow_id"], project_root=request["project_root"],
        )
        target = request["target_graph"]
        ingress = operations._v2_exact_ingress_edge(
            target, request["source_admission"]["ingress"], "captured Puber",
        )
        self.assertEqual(ingress["prompt_template"], gate + "\n\n")
        encoded = native_graph_postimage(target, workflow_id=request["workflow_id"])
        encoded_ingress = operations._v2_exact_ingress_edge(
            encoded, request["source_admission"]["ingress"], "encoded Puber",
        )
        self.assertEqual(encoded_ingress["prompt_template"], gate)
        operations._v2_source_gate_check(
            request["source_graph"], encoded, request["source_admission"],
            project_id=request["project_id"], workflow_id=request["workflow_id"],
            project_root=Path(request["project_root"]),
        )

    def test_exact_native_encoder_materializes_omitted_source_defaults(self):
        from workflowkit.graph import native_graph_postimage
        workflow = "c3150bf8-b900-4c87-bdf6-a1a47c74927c"
        source = {
            "version": 10, "nodes": [], "node_groups": [], "transition_groups": [],
            "edges": [{"id": workflow, "parameters": [{"key": "input", "description": "Input"}]}],
        }
        self.assertEqual(native_graph_postimage(source, workflow_id=workflow)["edges"], [{
            "id": workflow, "assignee_selection": "configured", "thinking_selection": "configured",
            "context_mode": "new_session", "context_source": {"kind": "immediate_source"},
            "parameters": [{"key": "input", "description": "Input", "purpose": "ordinary"}],
        }])
        for invalid in ("", "invented", None):
            source["edges"][0]["context_mode"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(operations.SpecError):
                native_graph_postimage(source, workflow_id=workflow)
        source["edges"][0].update({
            "assignee_selection": "previous_node", "thinking_selection": "previous_node",
            "context_mode": "compact_and_continue_session",
            "context_source": {"kind": "previous_target"},
            "parameters": [{"key": "target", "description": "Target", "purpose": "target_assignee"}],
        })
        self.assertEqual(
            native_graph_postimage(source, workflow_id=workflow)["edges"], source["edges"],
        )

    @unittest.skipUnless(os.environ.get("KENT_NATIVE_POSTIMAGE_CAPTURE"), "private native capture not supplied")
    def test_captured_release_and_puber_native_defaults_roundtrip_in_full(self):
        from workflowkit.graph import native_graph_postimage, native_graph_projection
        root = Path(os.environ["KENT_NATIVE_POSTIMAGE_CAPTURE"]).parent / "d29-final-admission-20261024"
        for member, version, expected_edges in (("appsome-release", 9, 54), ("puber-release", 96, 51)):
            with self.subTest(member=member):
                captured = json.loads((root / f"native-{member}-d29.json").read_bytes())
                actual = {"version": captured["expected_version"], **captured["graph"]}
                self.assertEqual(actual["version"], version)
                self.assertEqual(len(actual["edges"]), expected_edges)
                source = native_graph_projection(actual, workflow_id=captured["workflow_id"])
                self.assertTrue(any("context_mode" not in edge for edge in source["edges"]))
                # Compare every row/field, not merely the default field subset.
                self.assertEqual(
                    native_graph_postimage(source, workflow_id=captured["workflow_id"]), actual,
                )

    @unittest.skipUnless(os.environ.get("KENT_NATIVE_POSTIMAGE_CAPTURE"), "private native capture not supplied")
    def test_full_captured_slack_native_postimage(self):
        """Optional read-only qualification against the original private capture."""
        from workflowkit.graph import native_graph_postimage, native_graph_projection

        captured = json.loads(Path(os.environ["KENT_NATIVE_POSTIMAGE_CAPTURE"]).read_bytes())
        original = captured["original_plan"]
        source_raw = Path(original["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(source_raw).hexdigest(), original["sha256"])
        item = json.loads(source_raw)["workflows"][0]
        source = item["target_graph"]
        actual = captured["actual"]["graph"]
        self.assertEqual(item["workflow_id"], captured["actual"]["workflow_id"])
        self.assertEqual(sum(e.get("prompt_template") == "" for e in source["edges"]), 23)
        self.assertEqual(sum(e.get("assignee_selection") == "configured" for e in actual["edges"]), 42)
        self.assertEqual(sum(e.get("context_mode") == "new_session" for e in actual["edges"]), 27)
        self.assertEqual(
            native_graph_postimage(source, workflow_id=item["workflow_id"]), actual,
        )
        self.assertNotEqual(native_graph_projection(source, workflow_id=item["workflow_id"]), actual)
        self.assertEqual(
            canonical_sha256(actual),
            "8c26ad4c1419d155cfdfdd3c842336ea20959090e35ec9c8f9f79b3ee9e7d8e2",
        )

    def test_native_identity_is_original_canonical_uuid4(self):
        valid = "c3150bf8-b900-4c87-bdf6-a1a47c74927c"
        for identity in (valid.upper(), str(uuid.uuid5(uuid.NAMESPACE_URL, "edge")),
                         "not-a-native-id", valid.replace("-4c87-", "-1c87-")):
            with self.subTest(identity=identity), self.assertRaises(PlanValidationError):
                operations._v2_graph_rows({"edges": [{"id": identity}]}, "edges", "target")
        self.assertEqual(
            list(operations._v2_graph_rows({"edges": [{"id": valid}]}, "edges", "target")),
            [valid],
        )
        for field in ("workflow_id", "transition_group_id", "target_node_id", "group_id"):
            with self.subTest(field=field), self.assertRaises(PlanValidationError):
                operations._v2_graph_rows(
                    {"edges": [{"id": valid, field: str(uuid.uuid5(uuid.NAMESPACE_URL, field))}]},
                    "edges", "target",
                )
        row = {
            "id": valid, "context_source": {"kind": "selected_node", "node_key": "plan", "node_id": valid},
            "join_input_providers": [{"input_name": "input", "provider_edge_id": valid}],
        }
        self.assertEqual(operations._v2_graph_rows({"nodes": [row]}, "nodes", "target"), {valid: row})

    def test_exact_native_postimage_is_distinct_from_semantic_projection(self):
        from workflowkit.graph import native_graph_postimage, native_graph_projection

        workflow = "c3150bf8-b900-4c87-bdf6-a1a47c74927c"
        source = {
            "version": 11,
            "nodes": [{"id": workflow, "workflow_id": workflow, "future": "keep"}],
            "node_groups": [], "transition_groups": [],
            "edges": [{
                "id": workflow, "workflow_id": workflow, "prompt_template": "",
                "parameters": [], "assignee_selection": "configured",
                "thinking_selection": "configured", "context_mode": "new_session",
                "context_source": {"kind": "immediate_source"}, "future": {"keep": True},
            }],
        }
        original = json.loads(json.dumps(source))
        expected = {
            "version": 11, "nodes": [{"id": workflow, "future": "keep"}],
            "node_groups": [], "transition_groups": [],
            "edges": [{
                "id": workflow, "assignee_selection": "configured",
                "thinking_selection": "configured", "context_mode": "new_session",
                "context_source": {"kind": "immediate_source"}, "future": {"keep": True},
            }],
        }
        self.assertEqual(native_graph_postimage(source, workflow_id=workflow), expected)
        self.assertNotEqual(native_graph_projection(source, workflow_id=workflow), expected)
        self.assertEqual(source, original)

    def test_failed_effect_keeps_bounded_private_exact_diagnostics(self):
        from tests.test_workflow_retirement import load_test_plan, PYTHON

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with OperationJournal(root / "state", "effect-test", load_test_plan(root)) as journal:
                journal.persist({"phase": "in_progress", "effects": {}})
                result = operations.run_effect(
                    journal, effect_key="failed", command=[PYTHON, "-c",
                        "import sys; sys.stdout.buffer.write(b'error\\xff'); "
                        "sys.stderr.buffer.write(b'e'*20000); sys.exit(7)"],
                    cwd=root, preimage_sha256="a" * 64, postimage_sha256="b" * 64,
                    current_sha256=lambda: "a" * 64,
                )
                self.assertEqual(result.returncode, 7)
                self.assertEqual(result.settlement, "preimage")
                saved = journal.state["effects"]["failed"]["result"]
                import base64
                diagnostics = saved["diagnostics"]
                self.assertEqual(base64.b64decode(diagnostics["stdout"]["base64"]), b"error\xff")
                self.assertFalse(diagnostics["stdout"]["truncated"])
                self.assertEqual(len(base64.b64decode(diagnostics["stderr"]["base64"])), 16384)
                self.assertTrue(diagnostics["stderr"]["truncated"])
                self.assertEqual(saved["stderr_sha256"], hashlib.sha256(b"e" * 20000).hexdigest())
                self.assertEqual(stat.S_IMODE(journal.path.stat().st_mode), 0o600)

    def test_failure_output_reports_capture_limit_honestly(self):
        import base64
        output = operations._failure_output(b"x" * operations.MAX_OUTPUT)
        self.assertTrue(output["capture_limit_reached"])
        self.assertTrue(output["truncated"])
        self.assertEqual(output["captured_bytes"], operations.MAX_OUTPUT)
        self.assertEqual(len(base64.b64decode(output["base64"])), 16384)

    def test_native_graph_projection_encodes_source_rows_and_defaults(self):
        from workflowkit.graph import native_graph_projection

        workflow = "123e4567-e89b-12d3-a456-426614174000"
        source = {
            "version": 8,
            "nodes": [{"id": "f4d02953-01dd-4918-b9a9-fab7701b9bec", "workflow_id": workflow, "key": "janitor",
                       "kind": "script", "future": "preserved"}],
            "node_groups": [],
            "transition_groups": [{"id": "3d9953f1-20c4-4ad2-acae-32f4a429576a", "workflow_id": workflow,
                                   "source_node_id": "f4d02953-01dd-4918-b9a9-fab7701b9bec", "transition_id": "run"}],
            "edges": [
                {"id": "87e4a2a8-d512-4bc9-8fc5-a21a2eb77b02", "workflow_id": workflow, "parameters": [],
                 "assignee_selection": "configured", "thinking_selection": "configured",
                 "context_mode": "new_session",
                 "context_source": {"kind": "immediate_source"},
                 "prompt_template": " \t\nRun janitor.\n ", "future": {"keep": True}},
                {"id": "ae821784-4abb-4fa2-941f-d37e84be55ad", "workflow_id": workflow,
                 "assignee_selection": "previous_node", "thinking_selection": "configured",
                 "context_mode": "continue_session",
                 "context_source": {"kind": "selected_node", "node_key": "plan"}},
            ],
        }
        original = json.loads(json.dumps(source))
        projected = native_graph_projection(source, workflow_id=workflow)
        self.assertEqual(source, original)
        self.assertNotIn("workflow_id", projected["nodes"][0])
        self.assertEqual(projected["nodes"][0]["future"], "preserved")
        self.assertNotIn("workflow_id", projected["transition_groups"][0])
        self.assertEqual(projected["edges"][0]["prompt_template"], "Run janitor.")
        self.assertNotIn("parameters", projected["edges"][0])
        for key in ("assignee_selection", "thinking_selection", "context_mode", "context_source"):
            self.assertNotIn(key, projected["edges"][0])
        self.assertEqual(projected["edges"][0]["future"], {"keep": True})
        explicit = projected["edges"][1]
        self.assertNotIn("workflow_id", explicit)
        self.assertEqual(explicit["assignee_selection"], "previous_node")
        self.assertNotIn("thinking_selection", explicit)
        self.assertEqual(explicit["context_mode"], "continue_session")
        self.assertEqual(
            native_graph_projection(projected, workflow_id=workflow),
            projected,
        )
        mismatched = json.loads(json.dumps(source))
        mismatched["edges"][0]["workflow_id"] = "223e4567-e89b-12d3-a456-426614174000"
        with self.assertRaisesRegex(operations.SpecError, "Workflow identity differs"):
            native_graph_projection(mismatched, workflow_id=workflow)

    def test_native_readback_is_not_source_projected(self):
        raw = {
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "expected_version": 8,
            "graph": {
                "nodes": [{"id": "f4d02953-01dd-4918-b9a9-fab7701b9bec", "workflow_id": "unexpected"}],
                "edges": [{"id": "1aadda4e-41db-4f3b-87ef-7c3ad916a724", "parameters": [],
                           "assignee_selection": "configured"}],
                "node_groups": [],
                "transition_groups": [],
            },
        }
        observed = operations._canonical_graph(raw, 8)
        self.assertEqual(observed["nodes"], raw["graph"]["nodes"])
        self.assertEqual(observed["edges"], raw["graph"]["edges"])

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
    receipt_workflow_id: str | None = None,
) -> tuple[source_admission.DeliverySourceAdmissionTests, operations.VerifiedSourceAdmission]:
    """Create an offline native/install/source fixture and verify it end to end."""
    fixture = source_admission.DeliverySourceAdmissionTests(
        "test_published_closure_and_cli_are_read_only"
    )
    fixture.setUp()
    testcase.addCleanup(fixture.doCleanups)
    fixture.native_adapter_fixture()
    if local_instruction:
        fixture.add_local_instruction()

    fixture.receipt["project_id"] = project_id
    fixture.receipt["workflow_id"] = receipt_workflow_id or workflow_id_value
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
    if receipt_workflow_id is not None:
        admission["schema"] = operations.SOURCE_ADMISSION_V2_SCHEMA
        admission["binding"] = {
            "kind": "r2-installed",
            "installation_binding_sha256": admission.pop("installation_binding_sha256"),
            "receipt_workflow_id": receipt_workflow_id,
        }
        admission["ingress"] = {"kind": "agent-edge", "edge_id": "entry"}
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

def make_workflow_fixture(root: Path) -> dict:
    fixture = make_base_workflow_fixture(root)
    state = read_state(fixture)
    # Adapt the imported legacy inventory without changing its shared fixture.
    state = json.loads(json.dumps(state).replace(legacy_workflow_id(0), workflow_id(0)))
    state["project_id"] = PROJECT_ID
    write_state(fixture, state)
    return fixture

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
    approval_parameter_description: str | None = None,
    approval_context_mode: str | None = None,
    prefix_only: bool = False,
) -> tuple[operations.LoadedPlan, dict]:
    state = read_state(fixture)
    wid = workflow_id(0)
    workflow = state["workflows"][wid]
    workflow["graph"]["nodes"] = [
        {"id": "85142bca-99b5-4f0b-b364-f5296df95187", "key": "backlog", "kind": "agent"},
        {"id": "90e31940-4c84-466e-8e72-26f53c2f36f4", "key": "terminal", "kind": "terminal"},
        {"id": "7d280478-d98b-4462-9ae6-f58bf0d5a8d0", "key": "implement", "kind": "agent"},
        {"id": "8bda2875-9d3e-40e4-886c-25bb48f5f3aa", "key": "plan", "kind": "agent"},
        {"id": "4963546f-c7fe-490b-aadb-ae4aaacedb67", "key": "fix", "kind": "agent"},
        {
            "id": "0b234cff-2952-438d-be39-83d1165dbbe0",
            "key": "plan_revalidation",
            "kind": "agent",
        },
    ]
    if script_path_change:
        workflow["graph"]["nodes"].append({
            "id": "39fa54f4-f723-491e-a278-ba8547f7ba06",
            "key": "janitor",
            "kind": "script",
            "script_path": ".kent/scripts/old-janitor",
        })
    if not any(edge.get("id") == "bfabd30b-f1b3-4fec-92cc-b6b4af71b845" for edge in workflow["graph"]["edges"]):
        workflow["graph"]["transition_groups"].append({
            "id": "7660f06c-c962-4d80-9c3d-b4ea45a15a21",
            "source_node_id": "85142bca-99b5-4f0b-b364-f5296df95187",
            "transition_id": "backlog_plan",
            "description": "Start planning.",
        })
        workflow["graph"]["edges"].append({
            "id": "bfabd30b-f1b3-4fec-92cc-b6b4af71b845",
            "key": "backlog_plan",
            "transition_group_id": "7660f06c-c962-4d80-9c3d-b4ea45a15a21",
            "target_node_id": "8bda2875-9d3e-40e4-886c-25bb48f5f3aa",
            "assignee_selection": "configured",
            "thinking_selection": "configured",
            "requires_approval": False,
            "context_mode": "continue_session",
            "context_source": {"kind": "selected_node", "node_key": "backlog"},
            "prompt_template": "Begin plan revalidation.",
        })
    if not any(edge.get("key") == "fix_replan" for edge in workflow["graph"]["edges"]):
        workflow["graph"]["transition_groups"].append({
            "id": "c60a77d2-3672-49fc-8aea-fe5c37f0110a", "source_node_id": "4963546f-c7fe-490b-aadb-ae4aaacedb67",
            "transition_id": "fix_replan",
        })
        workflow["graph"]["edges"].append({
            "id": "9c264349-da25-4402-adb6-53780bd0b7fd", "key": "fix_replan",
            "transition_group_id": "c60a77d2-3672-49fc-8aea-fe5c37f0110a",
            "target_node_id": "0b234cff-2952-438d-be39-83d1165dbbe0",
            "parameters": [], "prompt_template": "Revalidate after a fix.",
        })
    if approval_parameter_description is not None:
        edge = next(row for row in workflow["graph"]["edges"] if row["key"] == "backlog_plan")
        edge["parameters"] = [
            {"key": "delivery_context", "purpose": "ordinary", "description": approval_parameter_description},
            {"key": "other", "purpose": "ordinary", "description": "Unchanged"},
        ]
    if approval_context_mode is not None:
        edge = next(row for row in workflow["graph"]["edges"] if row["key"] == "backlog_plan")
        edge["context_mode"] = approval_context_mode
        edge["context_source"] = {"kind": "immediate_source"}
    detail = workflow["details"]["TASK-1"]
    detail["live_sessions"] = []
    detail["current_nodes"] = [{"node_id": "90e31940-4c84-466e-8e72-26f53c2f36f4"}]
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
    if not prefix_only and not any(edge.get("key") == "implement_replan" for edge in source_graph["edges"]):
        target_graph["transition_groups"].append({
            "id": "c6a3f68e-7629-404a-8153-2d5f0e32ff0d",
            "source_node_id": "7d280478-d98b-4462-9ae6-f58bf0d5a8d0",
            "transition_id": "implement_replan",
            "description": "Return to plan revalidation.",
        })
        target_graph["edges"].append({
            "id": "045627f4-054e-46bc-9576-17a2d934d58a",
            "key": "implement_replan",
            "transition_group_id": "c6a3f68e-7629-404a-8153-2d5f0e32ff0d",
            "target_node_id": "0b234cff-2952-438d-be39-83d1165dbbe0",
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
        next(node for node in target_graph["nodes"] if node["id"] == "39fa54f4-f723-491e-a278-ba8547f7ba06")[
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
    from workflowkit.graph import native_graph_postimage, native_graph_projection

    review_source_graph = native_graph_projection(source_graph, workflow_id=wid)
    review_target_graph = native_graph_projection(full_target_graph, workflow_id=wid)
    target_graph_sha256 = canonical_sha256(
        native_graph_postimage(full_target_graph, workflow_id=wid)
        if intent != "metadata-only" else full_target_graph
    )
    target_metadata_sha256 = canonical_sha256(full_target_metadata)
    changed_paths = sorted(operations._v2_required_review_paths(
        review_source_graph,
        review_target_graph,
        source_metadata,
        full_target_metadata,
        compatibility_snapshot,
        source_admission,
    ))
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
        "anchors": [{"id": "90e31940-4c84-466e-8e72-26f53c2f36f4", "kind": "terminal"}],
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
            node_id = "90e31940-4c84-466e-8e72-26f53c2f36f4" if terminal else "85142bca-99b5-4f0b-b364-f5296df95187"
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
                "source_node_id": "85142bca-99b5-4f0b-b364-f5296df95187",
                "transition_id": "backlog_plan",
                "commentary": f"Reviewed native approval {index + 1}",
            }
            connection.execute(
                "INSERT INTO task_pending_approvals VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    approval_id,
                    task_id,
                    "85142bca-99b5-4f0b-b364-f5296df95187",
                    "backlog_plan",
                    workflow["version"],
                    canonical_bytes(transition).decode("utf-8"),
                    canonical_bytes({"approval_index": index + 1}).decode("utf-8"),
                    index + 1,
                ),
            )
            target = {
                "node_id": "8bda2875-9d3e-40e4-886c-25bb48f5f3aa",
                "node_kind": "agent",
                "entered_by_edge_id": edge["id"],
            }
            effective_edge = {**edge, "workflow_id": workflow_id(0)}
            # Materialize the native representation for the actual edge mode.
            if edge["context_mode"] == "new_session":
                context = {
                    "active_source": {"kind": "deferred_self"},
                    "target_session": {"kind": "create"},
                }
            else:
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
    approval_descriptions: tuple[str, str, str] | None = None,
    approval_context_mode: str = "new_session",
    prefix_only: bool = False,
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
        import sqlite3
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
    plan, value = canonical_v2_plan(
        fixture, intent=intent,
        approval_parameter_description=approval_descriptions[1] if approval_descriptions else None,
        approval_context_mode=approval_context_mode if with_approvals else None,
        prefix_only=prefix_only,
    )
    home, database, sessions_root, connection, sessions, _, question_input = make_native_persistence(
        fixture,
        production_shape=production_shape,
        with_approvals=with_approvals,
        unknown_backlog=unknown_backlog,
        with_questions=with_questions,
    )
    testcase.addCleanup(connection.close)
    if approval_descriptions is not None:
        for identity, raw in connection.execute(
            "SELECT approval_id,effective_edge_configuration_json FROM task_pending_approval_branches"
        ).fetchall():
            edge = json.loads(raw)
            edge["parameters"][0]["description"] = approval_descriptions[0]
            connection.execute(
                "UPDATE task_pending_approval_branches SET effective_edge_configuration_json=? WHERE approval_id=?",
                (canonical_bytes(edge).decode("utf-8"), identity),
            )
            delivery_context = '{"schema":"workflow-delivery-context-v1","phase":"pre_pr"}'
            materialized = json.loads(connection.execute(
                "SELECT materialized_values_json FROM task_pending_approvals WHERE id=?", (identity,),
            ).fetchone()[0])
            materialized["delivery_context"] = delivery_context
            connection.execute(
                "UPDATE task_pending_approvals SET materialized_values_json=? WHERE id=?",
                (canonical_bytes(materialized).decode("utf-8"), identity),
            )
            target = json.loads(connection.execute(
                "SELECT target_snapshot_json FROM task_pending_approval_branches WHERE approval_id=?", (identity,),
            ).fetchone()[0])
            target["current_input_values"] = {"delivery_context": delivery_context}
            connection.execute(
                "UPDATE task_pending_approval_branches SET target_snapshot_json=? WHERE approval_id=?",
                (canonical_bytes(target).decode("utf-8"), identity),
            )
        connection.commit()
        edge = next(row for row in value["workflows"][0]["target_graph"]["edges"]
                    if row["key"] == "backlog_plan")
        edge["parameters"][0]["description"] = approval_descriptions[2]
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
    if approval_descriptions is not None:
        from workflowkit.graph import native_graph_postimage, native_graph_projection
        target = native_graph_postimage(item["target_graph"], workflow_id=item["workflow_id"])
        review["target_graph_sha256"] = canonical_sha256(target)
        source = read_state(fixture)["workflows"][workflow_id(0)]
        required = operations._v2_required_review_paths(
            native_graph_projection(source["graph"], workflow_id=item["workflow_id"]),
            native_graph_projection(target, workflow_id=item["workflow_id"]),
            source["metadata"], item.get("target_metadata", source["metadata"]), snapshot,
            item["source_admission"],
        )
        existing = {row["path"] for row in review["dispositions"]}
        for path in sorted(required - existing):
            review["dispositions"].append({
                "path": path, "disposition": "compatible", "evidence": history_refs,
            })
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


def review_native_description_decisions(fixture: dict, plan: operations.LoadedPlan, context: dict,
                                       identities: set[str] | None = None) -> operations.LoadedPlan:
    value = plan.value
    item = value["workflows"][0]
    review = json.loads(context["review_path"].read_bytes())
    evidence_path = Path(fixture["root"]) / "description-compatibility.txt"
    evidence_path.write_text(
        "Frozen delivery_context remains v1. The v1/v2 description expands the future contract, not this decision. "
        "The exact materialized payload and retained producer remain v1; future consumers accept those v1 bytes. "
        "No conversion, replacement, or permission to produce v2 is inferred.\n"
        + json.dumps(context["connection"].execute(
            "SELECT id,materialized_values_json FROM task_pending_approvals ORDER BY id",
        ).fetchall())
    )
    evidence = {"path": str(evidence_path), "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
    for task in item["compatibility_snapshot"]["tasks"]:
        for decision in task["pending_decisions"]:
            if identities is None or decision["id"] in identities:
                review["dispositions"].append({
                    "path": f"decision:{decision['id']}", "disposition": "compatible", "evidence": [evidence],
                })
    raw = canonical_bytes(review)
    context["review_path"].write_bytes(raw)
    item["compatibility_review_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
    return write_canonical_v2_plan(fixture, value)


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

class CanonicalWorkflowLifecycleTest(unittest.TestCase):
    def test_captured_slack_and_puber_use_complete_candidate_facade(self):
        capture_root = os.environ.get("KENT_INGRESS_CAPTURE_ROOT")
        if not capture_root:
            self.skipTest("explicit read-only audit snapshot binding is unavailable")
        for wid, project in (
            ("a2dbdf14-8b03-4451-8eb1-db77a4f8b532", "project-0295d918-95a8-47b4-b96e-ab700d586232"),
            ("7061fc98-aae5-4468-9d89-fa28bfe30c72", "project-6c1b6c4e-c8e2-4170-b9f7-a7d9c4602176"),
        ):
            with self.subTest(workflow=wid):
                native = json.loads((Path(capture_root) / f"{wid}.json").read_bytes())
                graph = {
                    name: native.get(name, [])
                    for name in ("nodes", "node_groups", "transition_groups", "edges")
                }
                edge = next(row for row in graph["edges"] if row["key"] == "start_plan")
                old_prefix, body = edge["prompt_template"].split("Plan {{.TaskShortId}}:", 1)
                old_commit = old_prefix.split("Qualified initial commit: ", 1)[1].splitlines()[0]
                self.assertNotEqual(old_commit, native["workflow"]["execution_target_policy"]["custom_ref"])
                fixture, admitted = create_verified_source_fixture(
                    self, project, wid, receipt_workflow_id=wid,
                )
                value = admitted.value
                value["ingress"]["edge_id"] = edge["id"]
                with mock.patch.object(operations.Path, "home", return_value=fixture.home), \
                        mock.patch.dict(os.environ, {"HOME": str(fixture.home)}):
                    admitted = REAL_ADMIT_SOURCE_ADMISSION(
                        value, project_id=project, workflow_id=wid,
                        project_root=fixture.project, kent_path=fixture.native_bin,
                    )
                capture = {
                    "workflow_id": wid, "expected_version": native["workflow"]["version"],
                    "graph": graph, "execution_target_policy": native["workflow"]["execution_target_policy"],
                }
                target = {
                    "commit_oid": fixture.commits[fixture.project],
                    "tree_oid": fixture.git(fixture.project, "rev-parse", "HEAD^{tree}").strip(),
                }
                arguments = dict(
                    ingress=value["ingress"], admission=admitted,
                    reviewed_prefix=old_prefix,
                    reviewed_prefix_sha256=hashlib.sha256(old_prefix.encode()).hexdigest(),
                    qualified_target=target,
                )
                before = canonical_bytes(capture)
                candidate = operations.render_source_ingress_candidate(capture, **arguments)
                self.assertEqual(canonical_bytes(capture), before)
                self.assertEqual(
                    operations.render_source_ingress_candidate(candidate, **arguments), candidate,
                )
                updated = next(row for row in candidate["graph"]["edges"] if row["id"] == edge["id"])
                self.assertTrue(updated["prompt_template"].endswith("Plan {{.TaskShortId}}:" + body))
                expected = json.loads(before)
                expected["execution_target_policy"] = candidate["execution_target_policy"]
                next(row for row in expected["graph"]["edges"] if row["id"] == edge["id"])[
                    "prompt_template"
                ] = updated["prompt_template"]
                self.assertEqual(expected, candidate)
                with self.assertRaises(PlanValidationError):
                    operations.render_source_ingress_candidate(
                        capture, **{**arguments, "qualified_target": {**target, "tree_oid": "f" * 40}},
                    )
                from workflowkit.delivery import validate_initial_source_selector
                validate_initial_source_selector(candidate["graph"], "ref:" + target["commit_oid"])
                self.assertIn("Expected ingress node:", updated["prompt_template"])
                self.assertIn("genuine inherited Session ownership", updated["prompt_template"])
                with self.assertRaises(ValueError):
                    validate_initial_source_selector(candidate["graph"], old_commit)
                # A second existing commit/tree is internally consistent but
                # is not the freshly admitted published source.
                fixture.put(fixture.project, "product.txt", "next product revision\n")
                fixture.commit(fixture.project)
                other = {
                    "commit_oid": fixture.git(fixture.project, "rev-parse", "HEAD").strip(),
                    "tree_oid": fixture.git(fixture.project, "rev-parse", "HEAD^{tree}").strip(),
                }
                self.assertNotEqual(other["commit_oid"], target["commit_oid"])
                forged = json.loads(canonical_bytes(candidate))
                next(row for row in forged["graph"]["edges"] if row["id"] == edge["id"])[
                    "prompt_template"
                ] = updated["prompt_template"].replace(
                    canonical_bytes(target).decode(), canonical_bytes(other).decode(),
                )
                with mock.patch.object(operations, "admit_source_admission", return_value=admitted):
                    with self.assertRaisesRegex(EffectBlocked, "admitted source commit"):
                        operations._admit_v2_source({
                            "project_root": fixture.project, "kent": fixture.native_bin,
                        }, {
                            "project_id": project, "workflow_id": wid,
                            "source_admission": admitted.value, "target_graph": forged["graph"],
                        })

    def test_bounded_ingress_renderer_preserves_body_and_rejects_unreviewed_prefix(self):
        fixture, admission = create_verified_source_fixture(self, PROJECT_ID, workflow_id(0))
        value = admission.value
        value["schema"] = operations.SOURCE_ADMISSION_V2_SCHEMA
        value["binding"] = {
            "kind": "r2-installed",
            "installation_binding_sha256": value.pop("installation_binding_sha256"),
        }
        value["ingress"] = {"kind": "agent-edge", "edge_id": "entry"}
        verified = synthetic_verified_source_admission(
            value, project_id=PROJECT_ID, workflow_id=workflow_id(0),
            project_root=fixture.project, kent_path=fixture.native_bin,
        )
        old = "INITIAL SOURCE ADMISSION\nQualified initial commit: " + "b" * 40 + "\n\n"
        capture = {
            "workflow_id": workflow_id(0), "expected_version": 13,
            "execution_target_policy": {"mode": "custom_ref", "custom_ref": "c" * 40},
            "graph": {
                "nodes": [{"id": "start", "key": "backlog", "kind": "agent"},
                          {"id": "plan", "key": "plan", "kind": "agent"}],
                "node_groups": [],
                "transition_groups": [{"id": "group", "source_node_id": "start"}],
                "edges": [{
                    "id": "entry", "key": "start_plan", "transition_group_id": "group",
                    "target_node_id": "plan", "requires_approval": True,
                    "context_mode": "continue_session",
                    "parameters": [{"key": "carrier"}],
                    "prompt_template": old + "Plan body remains byte exact.\n",
                }],
            },
        }
        from workflowkit.delivery import render_source_ingress
        arguments = dict(
            ingress=value["ingress"], admission=verified, reviewed_prefix=old,
            reviewed_prefix_sha256=hashlib.sha256(old.encode()).hexdigest(),
            qualified_target={"commit_oid": "d" * 40, "tree_oid": "e" * 40},
        )
        before = canonical_bytes(capture)
        after = render_source_ingress(capture, **arguments)
        self.assertEqual(canonical_bytes(capture), before)
        self.assertEqual(after["execution_target_policy"]["custom_ref"], "d" * 40)
        self.assertEqual(after["graph"]["edges"][0]["requires_approval"], True)
        self.assertTrue(after["graph"]["edges"][0]["prompt_template"].endswith(
            "Plan body remains byte exact.\n"))
        self.assertEqual(render_source_ingress(after, **arguments), after)
        for change in (
            {"reviewed_prefix_sha256": "0" * 64},
            {"reviewed_prefix": "different"},
            {"qualified_target": {"commit_oid": "wrong", "tree_oid": "e" * 40}},
            {"ingress": {"kind": "agent-edge", "edge_id": "foreign"}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                render_source_ingress(capture, **{**arguments, **change})
        bypass = json.loads(before)
        bypass["graph"]["edges"].append({
            **bypass["graph"]["edges"][0], "id": "bypass",
        })
        with self.assertRaises(ValueError):
            render_source_ingress(bypass, **arguments)
        script_value = verified.value
        script_value["ingress"]["kind"] = "script-edge"
        script_admission = synthetic_verified_source_admission(
            script_value, project_id=PROJECT_ID, workflow_id=workflow_id(0),
            project_root=fixture.project, kent_path=fixture.native_bin,
        )
        with self.assertRaisesRegex(ValueError, "exact ingress"):
            render_source_ingress(capture, **{
                **arguments, "ingress": script_value["ingress"], "admission": script_admission,
            })
        from workflowkit.delivery import SOURCE_ADMISSION_END
        for text in (SOURCE_ADMISSION_END, "Qualified initial Git tree: " + "f" * 40):
            altered = json.loads(before)
            altered["graph"]["edges"][0]["prompt_template"] += text
            with self.subTest(text=text), self.assertRaises(ValueError):
                render_source_ingress(altered, **arguments)
        altered = json.loads(canonical_bytes(after))
        altered["execution_target_policy"]["custom_ref"] = "f" * 40
        with self.assertRaises(ValueError):
            render_source_ingress(altered, **arguments)

    def test_one_installation_admits_explicit_receipt_owner_for_another_workflow(self):
        target, owner = workflow_id(0), workflow_id(1)
        fixture, verified = create_verified_source_fixture(
            self, PROJECT_ID, target, receipt_workflow_id=owner,
        )
        self.assertEqual(verified.workflow_id, target)
        self.assertEqual(verified.inspection["workflow_id"], owner)
        from workflowkit.delivery import source_admission_gate
        self.assertIn(f'"workflow_id":"{target}"', verified.gate)
        self.assertIn(f"--workflow-id {owner}", verified.gate)
        with mock.patch.object(operations, "admit_source_admission", return_value=verified):
            result = operations._admit_v2_source({
                "project_root": fixture.project, "kent": fixture.native_bin,
            }, {
                "project_id": PROJECT_ID, "workflow_id": target,
                "source_admission": verified.value,
            })
            self.assertEqual(result["inspection"]["workflow_id"], owner)
        with mock.patch.object(operations.Path, "home", return_value=fixture.home), \
                mock.patch.dict(os.environ, {"HOME": str(fixture.home)}):
            def inspect(value):
                return REAL_ADMIT_SOURCE_ADMISSION(
                    value, project_id=PROJECT_ID, workflow_id=target,
                    project_root=fixture.project, kent_path=fixture.native_bin,
                )
            for selected in (target, None):
                value = verified.value
                if selected is None:
                    value["binding"].pop("receipt_workflow_id")
                else:
                    value["binding"]["receipt_workflow_id"] = selected
                with self.subTest(owner=selected), self.assertRaises(PlanValidationError):
                    inspect(value)
            value = verified.value
            value["binding"]["unknown"] = owner
            with self.assertRaises(PlanValidationError):
                inspect(value)
            self.assertEqual(
                verified.gate, source_admission_gate(
                    verified.value, project_id=PROJECT_ID, workflow_id=target,
                    project_root=str(fixture.project),
                ),
            )
            profile = fixture.project / ".kent/workflow-profile.toml"
            profile.write_text(profile.read_text() + "\n# stale installed profile\n")
            with self.assertRaises(PlanValidationError):
                inspect(verified.value)

    def test_invalid_original_native_ids_and_references_never_dispatch(self):
        for invalid in (str(uuid.uuid5(uuid.NAMESPACE_URL, "native-edge")),
                        "C3150BF8-B900-4C87-BDF6-A1A47C74927C"):
            for field in ("id", "target_node_id", "transition_group_id", "workflow_id"):
                with self.subTest(invalid=invalid, field=field), tempfile.TemporaryDirectory() as temporary:
                    fixture = make_workflow_fixture(Path(temporary))
                    _, value = canonical_v2_plan(fixture)
                    value["workflows"][0]["target_graph"]["edges"][0][field] = invalid
                    plan = write_canonical_v2_plan(fixture, value)
                    with mock.patch.object(operations, "run_effect", side_effect=AssertionError("dispatch")):
                        with self.assertRaises(PlanValidationError):
                            reconcile_canonical_workflows(plan, mode="prepare")
                    self.assertEqual(graph_apply_rows(fixture), [])
                    self.assertFalse((Path(value["state_dir"]) / "canonical-workflow-reconcile.journal.json").exists())

    def test_invalid_nested_native_references_never_dispatch(self):
        for invalid in (str(uuid.uuid5(uuid.NAMESPACE_URL, "native-reference")),
                        "C3150BF8-B900-4C87-BDF6-A1A47C74927C"):
            for nested in ("context_source.node_id", "join_input_providers.provider_edge_id"):
                with self.subTest(invalid=invalid, nested=nested), tempfile.TemporaryDirectory() as temporary:
                    fixture = make_workflow_fixture(Path(temporary))
                    _, value = canonical_v2_plan(fixture)
                    graph = value["workflows"][0]["target_graph"]
                    if nested == "context_source.node_id":
                        graph["edges"][0]["context_source"]["node_id"] = invalid
                    else:
                        graph["nodes"][0]["join_input_providers"] = [{
                            "input_name": "input", "provider_edge_id": invalid,
                        }]
                    plan = write_canonical_v2_plan(fixture, value)
                    with self.assertRaisesRegex(PlanValidationError, "invalid digest"):
                        operations._validate_canonical_v2_plan(plan, verify_evidence=False)
                    with mock.patch.object(operations, "run_effect", side_effect=AssertionError("dispatch")):
                        with self.assertRaises(PlanValidationError):
                            reconcile_canonical_workflows(plan, mode="prepare")
                    self.assertEqual(graph_apply_rows(fixture), [])
                    self.assertFalse((Path(value["state_dir"]) / "canonical-workflow-reconcile.journal.json").exists())

    def metadata_completion_plan(self, fixture):
        """Model the saved graph separately from its remaining selector."""
        from workflowkit.delivery import SOURCE_ADMISSION_END, source_admission_gate
        from workflowkit.graph import native_graph_postimage, native_graph_projection

        _, initial = canonical_v2_plan(fixture, pending_approval=True)
        initial_item = initial["workflows"][0]
        admission = initial_item["source_admission"]
        admission["schema"] = "delivery-source-admission-v2"
        admission["binding"] = {
            "kind": "r2-installed",
            "installation_binding_sha256": admission.pop("installation_binding_sha256"),
        }
        ingress = operations._v2_ingress_edge(initial_item["target_graph"], "fixture")
        admission["ingress"] = {"kind": "agent-edge", "edge_id": ingress["id"]}
        suffix = ingress["prompt_template"].split(SOURCE_ADMISSION_END + "\n\n", 1)[1]
        ingress["prompt_template"] = source_admission_gate(
            admission, project_id=PROJECT_ID, workflow_id=initial_item["workflow_id"],
            project_root=str(fixture["root"]),
        ) + "\n\n" + suffix
        state = read_state(fixture)
        workflow = state["workflows"][initial_item["workflow_id"]]
        saved = native_graph_postimage(
            initial_item["target_graph"], workflow_id=initial_item["workflow_id"],
        )
        workflow["version"] = saved["version"]
        workflow["graph"] = {key: saved[key] for key in (
            "nodes", "edges", "node_groups", "transition_groups",
        )}
        write_state(fixture, state)
        _, value = canonical_v2_plan(
            fixture, intent="metadata-only", pending_approval=True,
            source_admission_enabled=False,
            metadata={**workflow["metadata"], "execution_target": "default-branch"},
            name="selector-completion.json",
        )
        item = value["workflows"][0]
        item["source_admission"] = admission
        review_path = Path(item["compatibility_review_ref"]["path"])
        review = json.loads(review_path.read_bytes())
        review["source_admission_sha256"] = canonical_sha256(admission)
        projected = native_graph_projection(saved, workflow_id=item["workflow_id"])
        required = operations._v2_required_review_paths(
            projected, projected, workflow["metadata"], item["target_metadata"],
            item["compatibility_snapshot"], admission,
        )
        evidence = review["dispositions"][0]["evidence"]
        review["dispositions"] = [
            {"path": path, "disposition": "compatible", "evidence": evidence}
            for path in sorted(required)
        ]
        raw = canonical_bytes(review)
        review_path.write_bytes(raw)
        item["compatibility_review_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
        return write_canonical_v2_plan(fixture, value, name="selector-completion.json"), value

    def test_metadata_only_completion_readmits_exact_saved_frame_without_graph_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            plan, _ = self.metadata_completion_plan(fixture)
            before = read_state(fixture)["workflows"][workflow_id(0)]
            with mock.patch.object(
                operations, "admit_source_admission",
                side_effect=synthetic_verified_source_admission,
            ) as admission:
                prepared = reconcile_canonical_workflows(plan, mode="prepare")
                self.assertEqual(prepared["phase"], "prepared")
                report = reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                self.assertGreaterEqual(admission.call_count, 2)
            self.assertEqual(report["phase"], "complete")
            after = read_state(fixture)["workflows"][workflow_id(0)]
            self.assertEqual(after["graph"], before["graph"])
            self.assertEqual(after["details"], before["details"])
            self.assertEqual(graph_apply_rows(fixture), [])
            self.assertEqual(after["metadata"]["execution_target"], "default-branch")

    def test_metadata_only_completion_rejects_unadmitted_or_drifted_actual_state(self):
        for mutation in ("missing-frame", "wrong-frame", "wrong-ingress", "extra-route",
                         "closure", "owner", "pending", "graph-payload", "metadata"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                fixture = make_workflow_fixture(Path(temporary))
                _, value = self.metadata_completion_plan(fixture)
                item = value["workflows"][0]
                state = read_state(fixture)
                workflow = state["workflows"][workflow_id(0)]
                graph = workflow["graph"]
                ingress = operations._v2_ingress_edge(graph, "fixture")
                if mutation == "missing-frame":
                    ingress["prompt_template"] = "Unadmitted."
                elif mutation == "wrong-frame":
                    ingress["prompt_template"] = ingress["prompt_template"].replace(
                        "refs/remotes/origin/main", "refs/remotes/origin/other",
                    )
                elif mutation == "wrong-ingress":
                    item["source_admission"]["ingress"]["edge_id"] = str(uuid.uuid4())
                elif mutation == "extra-route":
                    graph["edges"].append({**ingress, "id": str(uuid.uuid4())})
                elif mutation == "closure":
                    Path(item["source_admission"]["receipt_ref"]["path"]).write_bytes(b"drift")
                elif mutation == "owner":
                    workflow["default"] = False
                elif mutation == "pending":
                    workflow["details"]["TASK-1"]["pending_approvals"].append({"id": "foreign"})
                elif mutation == "graph-payload":
                    item["target_graph"] = {"version": workflow["version"] + 1, **graph}
                elif mutation == "metadata":
                    item["target_metadata"]["description"] = "Smuggled change."
                if mutation in {"missing-frame", "wrong-frame", "extra-route"}:
                    # Refresh the raw preimage hash to prove rejection is by
                    # actual ingress qualification, not an incidental digest.
                    item["source_graph_sha256"] = canonical_sha256({
                        "version": workflow["version"], **graph,
                    })
                write_state(fixture, state)
                plan = write_canonical_v2_plan(fixture, value)
                with mock.patch.object(
                    operations, "admit_source_admission",
                    # Exercise independent admission refusal; real closure
                    # validation has its own end-to-end source-admission suite.
                    side_effect=(
                        EffectBlocked("published closure changed")
                        if mutation == "closure" else synthetic_verified_source_admission
                    ),
                ), self.assertRaises((PlanValidationError, EffectBlocked)):
                    reconcile_canonical_workflows(plan, mode="prepare")
                self.assertEqual(graph_apply_rows(fixture), [])
                self.assertFalse(any(row[:2] == ["workflow", "update"] for row in read_log(fixture)))

    def test_metadata_only_completion_refuses_closure_drift_after_prepare(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            plan, _ = self.metadata_completion_plan(fixture)
            reconcile_canonical_workflows(plan, mode="prepare")
            with mock.patch.object(
                operations, "admit_source_admission", side_effect=EffectBlocked("closure drift"),
            ), self.assertRaises(EffectBlocked):
                reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
            self.assertFalse(any(row[:2] == ["workflow", "update"] for row in read_log(fixture)))

    def test_v2_plan_source_rows_are_projected_before_review_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            _, document = canonical_v2_plan(fixture)
            item = document["workflows"][0]
            target = item["target_graph"]
            workflow = item["workflow_id"]
            for collection in ("nodes", "edges", "node_groups", "transition_groups"):
                for row in target[collection]:
                    row["workflow_id"] = workflow
            for edge in target["edges"]:
                edge["assignee_selection"] = "configured"
                edge["thinking_selection"] = "configured"
                if edge.get("context_mode") == "new_session":
                    edge["context_source"] = {"kind": "immediate_source"}
                if "parameters" not in edge:
                    edge["parameters"] = []
            plan = write_canonical_v2_plan(fixture, document)
            parsed = operations._validate_canonical_v2_plan(plan, verify_evidence=False)
            parsed_item = parsed["workflows"][0]
            projected = parsed_item["_review_target_graph"]
            self.assertTrue(all(
                "workflow_id" not in row
                for collection in ("nodes", "edges", "node_groups", "transition_groups")
                for row in projected[collection]
            ))
            from workflowkit.graph import native_graph_postimage
            self.assertEqual(
                parsed_item["target_graph"], native_graph_postimage(target, workflow_id=workflow),
            )
            for edge in projected["edges"]:
                self.assertNotEqual(edge.get("assignee_selection"), "configured")
                self.assertNotEqual(edge.get("thinking_selection"), "configured")
                self.assertNotEqual(edge.get("parameters"), [])
                if edge.get("context_mode") == "new_session":
                    self.assertNotIn("context_source", edge)

    def test_v2_project_fields_require_matching_native_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            _, baseline = canonical_v2_plan(fixture)
            invalid = (
                RETIREMENT_PROJECT_ID,
                "workspace-" + RETIREMENT_PROJECT_ID,
                "project-" + RETIREMENT_PROJECT_ID.upper(),
                "project-not-a-uuid",
                "project-" + RETIREMENT_PROJECT_ID + " ",
                "project-123e4567-e89b-12d3-a456-426614174099",
            )
            for field in ("project", "link"):
                for value in invalid:
                    with self.subTest(field=field, value=value):
                        document = json.loads(json.dumps(baseline))
                        item = document["workflows"][0]
                        target = item if field == "project" else item["links"][0]
                        target["project_id"] = value
                        raw = canonical_bytes(document)
                        plan = operations.LoadedPlan(
                            schema=operations.CANONICAL_V2_SCHEMA, value=document,
                            raw=raw, sha256=hashlib.sha256(raw).hexdigest(),
                        )
                        with self.assertRaises(PlanValidationError):
                            operations._validate_canonical_v2_plan(plan, verify_evidence=False)
            self.assertEqual(read_log(fixture), [])

    def test_v2_execution_target_selectors_are_validated_in_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            _, baseline = canonical_v2_plan(fixture, intent="graph-and-metadata")
            valid = ("none", "head", "default-branch", "ask-on-first-execution",
                     "ref:refs/heads/main", "ref:refs/tags/v1")
            invalid = ("ask_on_first_execution", "default_branch", "ref:",
                       "ref:refs/tags/v 1", "local")
            for target in (*valid, *invalid):
                with self.subTest(target=target):
                    document = json.loads(json.dumps(baseline))
                    document["workflows"][0]["target_metadata"]["execution_target"] = target
                    raw = canonical_bytes(document)
                    plan = operations.LoadedPlan(
                        schema=operations.CANONICAL_V2_SCHEMA, value=document,
                        raw=raw, sha256=hashlib.sha256(raw).hexdigest(),
                    )
                    if target in valid:
                        parsed = operations._validate_canonical_v2_plan(plan, verify_evidence=False)
                        self.assertEqual(parsed["workflows"][0]["target_metadata"]["execution_target"], target)
                    else:
                        with self.assertRaises(PlanValidationError):
                            operations._validate_canonical_v2_plan(plan, verify_evidence=False)

    def test_retired_v1_plan_is_rejected_before_journal_or_kent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = {
                "schema": "canonical-workflow-reconcile-plan-v1",
                "state_dir": str(root / "state"),
            }
            path = root / "v1.json"
            raw = canonical_bytes(document)
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            plan = load_plan(
                path, schema=document["schema"], expected_sha256=digest,
            )
            with mock.patch.object(operations, "_kent_json", side_effect=AssertionError("Kent read")):
                with self.assertRaisesRegex(PlanValidationError, "canonical v2"):
                    reconcile_canonical_workflows(plan, mode="prepare")
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1]
                                    / "scripts/reconcile-canonical-workflows"),
                 "prepare", "--plan", str(path), "--expect-plan-sha256", digest,
                 "--kent", str(root / "never-executed-kent")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("schema", result.stderr)
            self.assertFalse((root / "state").exists())

    def test_prepare_preflights_later_stage_before_persist_or_child(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
        v2_admission = {
            "schema": "delivery-source-admission-v2",
            "receipt_ref": admitted.value["receipt_ref"],
            "primary_workspace_id": admitted.value["primary_workspace_id"],
            "primary_root": admitted.value["primary_root"],
            "kit_root": admitted.value["kit_root"],
            "source_ref": admitted.value["source_ref"],
            "kit_ref": admitted.value["kit_ref"],
            "ci_helper": admitted.value["ci_helper"],
            "helper_bindings": admitted.value["helper_bindings"],
            "binding": {
                "kind": "r2-installed",
                "installation_binding_sha256": admitted.value[
                    "installation_binding_sha256"
                ],
            },
            "ingress": {"kind": "script-edge", "edge_id": "000d0e6b-58b4-435a-a9f9-cb32f7d420ab"},
        }
        with mock.patch.object(operations.Path, "home", return_value=fixture.home), \
                mock.patch.dict("os.environ", {"HOME": str(fixture.home)}):
            admitted_v2 = REAL_ADMIT_SOURCE_ADMISSION(
                v2_admission,
                project_id=PROJECT_ID,
                workflow_id=wid,
                project_root=fixture.project,
                kent_path=fixture.native_bin,
            )
        self.assertEqual(admitted_v2.value["binding"], v2_admission["binding"])
        self.assertEqual(admitted_v2.receipt_sha256, fixture.digest)

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

    def test_plan_publication_admission_does_not_require_r2_binding_or_memory_profile(self):
        wid = workflow_id(0)
        fixture, _ = create_verified_source_fixture(self, PROJECT_ID, wid)
        profile = fixture.project / ".kent/workflow-profile.toml"
        profile.write_text('[commands]\ncheckpoint = ".kent/scripts/workflow-checkpoint"\n')
        profile.chmod(0o644)
        fixture.repin_native_sources()
        fixture.receipt = {
            "schema": "delivery-source-publication-v1",
            "project_id": PROJECT_ID,
            "workflow_id": wid,
            "source_commit": fixture.commits[fixture.project],
            "kit_commit": fixture.commits[fixture.kit],
            "runtime_roots": [str(fixture.project), str(fixture.kit)],
            "files": [],
            "policy": "published_source_v1",
            "local_inputs": [],
        }
        fixture.refresh_receipt_files()
        fixture.pin()
        fixture.binding_path.unlink()
        # Source-only admission must work without any native Task evidence.
        fixture.native_bin.write_text(fixture.native_bin.read_text().replace(
            "args = sys.argv[1:]\n",
            "args = sys.argv[1:]\n"
            "if args and args[0] == 'task':\n"
            "    sys.exit(2)\n",
        ))
        admission = {
            "schema": "delivery-source-admission-v2",
            "receipt_ref": {
                "path": str(fixture.receipt_path),
                "sha256": fixture.digest,
            },
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
            "binding": {"kind": "plan-publication"},
            "ingress": {"kind": "script-edge", "edge_id": "40be9965-3a5f-4917-b466-7f0958f0fa89"},
        }
        with mock.patch.object(operations.Path, "home", return_value=fixture.home), \
                mock.patch.dict("os.environ", {"HOME": str(fixture.home)}):
            admitted = REAL_ADMIT_SOURCE_ADMISSION(
                admission,
                project_id=PROJECT_ID,
                workflow_id=wid,
                project_root=fixture.project,
                kent_path=fixture.native_bin,
            )
        self.assertEqual(admitted.value["binding"], {"kind": "plan-publication"})
        self.assertEqual(admitted.receipt_sha256, fixture.digest)
        self.assertEqual(admitted.inspection["status"], "admitted")
        self.assertFalse(fixture.binding_path.exists())

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
            "context_mode": "continue_session",
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

        frozen_edge = json.loads(branch["effective_edge_configuration_json"])
        frozen_edge["parameters"] = [
            {"key": "delivery_context", "purpose": "ordinary", "description": "v1"},
        ]
        branch["effective_edge_configuration_json"] = canonical_bytes(frozen_edge).decode("utf-8")
        graph_edge["parameters"] = [
            {"key": "delivery_context", "purpose": "ordinary", "description": "v1/v2"},
        ]
        affected: set[str] = set()
        envelope = operations._native_approval_payload(
            approval, [branch], nodes, workflow_id_value, workflow_graph, affected_decisions=affected,
        )
        self.assertEqual(affected, {approval["id"]})
        self.assertEqual(envelope, {"approval": approval, "branches": [branch]})
        baseline = digest(approval, branch)
        for column, source in (
            ("transition_snapshot_json", approval), ("materialized_values_json", approval),
            ("target_snapshot_json", branch), ("effective_edge_configuration_json", branch),
            ("context_source_resolution_json", branch),
        ):
            changed_approval, changed_branch = dict(approval), dict(branch)
            (changed_approval if source is approval else changed_branch)[column] = " " + source[column]
            self.assertNotEqual(digest(changed_approval, changed_branch), baseline, column)
        # A target's other semantic edits must not conceal its description edit.
        graph_edge["parameters"][0]["description"] = "v1"
        target_graph = json.loads(json.dumps(workflow_graph))
        target_graph["edges"][0]["parameters"][0].update(description="v1/v2", purpose="target_assignee")
        affected.clear()
        operations._native_approval_payload(
            approval, [branch], nodes, workflow_id_value, workflow_graph,
            affected_decisions=affected, target_graph=target_graph,
        )
        self.assertEqual(affected, {approval["id"]})

    def test_native_approval_context_dispatch_is_closed_and_raw_bound(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            _, context, _ = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_approvals=True,
            )
            connection = context["connection"]
            connection.row_factory = sqlite3.Row
            original_approval = dict(connection.execute(
                "SELECT * FROM task_pending_approvals ORDER BY id LIMIT 1",
            ).fetchone())
            original_branch = dict(connection.execute(
                "SELECT * FROM task_pending_approval_branches WHERE approval_id=?",
                (original_approval["id"],),
            ).fetchone())
            original_nodes = [dict(row) for row in connection.execute(
                "SELECT * FROM task_current_nodes WHERE task_id=?",
                (original_approval["source_task_id"],),
            )]
            original_graph = read_state(fixture)["workflows"][workflow_id(0)]["graph"]
            for mode in ("new_session", "continue_session", "compact_and_continue_session"):
                for session_id in (None, "44444444-4444-4444-8444-444444444444"):
                    with self.subTest(mode=mode, session_id=session_id):
                        approval = {**original_approval, "source_session_id": session_id}
                        nodes = [{**row, "session_id": session_id} for row in original_nodes]
                        graph = json.loads(json.dumps(original_graph))
                        edge = next(row for row in graph["edges"] if row["key"] == "backlog_plan")
                        edge["context_mode"] = mode
                        frozen_edge = {
                            **json.loads(original_branch["effective_edge_configuration_json"]),
                            "context_mode": mode,
                        }
                        reuse = {
                            "active_source": {"kind": "exact", "session_id": session_id},
                            "target_session": {"kind": "reuse", "session_id": session_id},
                        }
                        create = {
                            "active_source": {"kind": "deferred_self"},
                            "target_session": {"kind": "create"},
                        }
                        resolved = create if mode == "new_session" else reuse
                        branch = {
                            **original_branch,
                            "effective_edge_configuration_json": json.dumps(frozen_edge),
                            "context_source_resolution_json": json.dumps(resolved),
                        }

                        def payload(current_approval=approval, current_branch=branch,
                                    current_graph=graph, current_nodes=nodes):
                            return operations._native_approval_payload(
                                current_approval, [current_branch], current_nodes, workflow_id(0), current_graph,
                            )

                        envelope = payload()
                        self.assertEqual(envelope, {"approval": approval, "branches": [branch]})
                        baseline = canonical_sha256(envelope)
                        self.assertNotEqual(
                            canonical_sha256(payload({**approval, "workflow_version": approval["workflow_version"] + 1})),
                            baseline,
                        )
                        for column, source in (
                            ("transition_snapshot_json", approval), ("materialized_values_json", approval),
                            ("target_snapshot_json", branch), ("effective_edge_configuration_json", branch),
                            ("context_source_resolution_json", branch),
                        ):
                            changed_approval, changed_branch = dict(approval), dict(branch)
                            changed = changed_approval if source is approval else changed_branch
                            changed[column] = " " + source[column]
                            self.assertNotEqual(
                                canonical_sha256(payload(changed_approval, changed_branch)), baseline, column,
                            )
                            semantic = json.loads(source[column])
                            if column == "context_source_resolution_json":
                                # Every semantic context change is either another
                                # closed mode or invalid, never silently normalized.
                                semantic["active_source"]["unexpected"] = True
                            elif column == "effective_edge_configuration_json":
                                semantic["prompt_template"] = "Changed frozen prompt bytes."
                            else:
                                semantic["fixture_semantic_change"] = True
                            changed[column] = json.dumps(semantic)
                            if column == "context_source_resolution_json":
                                with self.assertRaises(EffectBlocked):
                                    payload(changed_approval, changed_branch)
                            else:
                                self.assertNotEqual(
                                    canonical_sha256(payload(changed_approval, changed_branch)), baseline, column,
                                )

                        invalid_contexts = [
                            reuse if mode == "new_session" else create,
                            {"active_source": reuse["active_source"], "target_session": create["target_session"]},
                            {"active_source": create["active_source"], "target_session": reuse["target_session"]},
                            {**resolved, "extra": None},
                            {"active_source": resolved["active_source"]},
                            {"target_session": resolved["target_session"]},
                        ]
                        for key in ("active_source", "target_session"):
                            invalid_contexts.extend([
                                {**resolved, key: {**resolved[key], "extra": None}},
                                {**resolved, key: {**resolved[key], "session_id": "foreign-session"}},
                                {**resolved, key: {"kind": "unknown"}},
                            ])
                            if mode == "new_session":
                                invalid_contexts.append({**resolved, key: {**resolved[key], "session_id": None}})
                        for invalid in invalid_contexts:
                            with self.subTest(invalid_context=invalid), self.assertRaises(EffectBlocked):
                                payload(current_branch={
                                    **branch, "context_source_resolution_json": json.dumps(invalid),
                                })
                        for bad_mode in (None, "", "reuse", "unknown", " new_session"):
                            bad_graph = json.loads(json.dumps(graph))
                            next(row for row in bad_graph["edges"] if row["id"] == edge["id"])[
                                "context_mode"] = bad_mode
                            with self.subTest(bad_mode=bad_mode), self.assertRaises(EffectBlocked):
                                payload(current_graph=bad_graph, current_branch={
                                    **branch, "effective_edge_configuration_json": json.dumps({
                                        **frozen_edge, "context_mode": bad_mode,
                                    }),
                                })
                        other_mode = "continue_session" if mode == "new_session" else "new_session"
                        with self.assertRaises(EffectBlocked):
                            payload(current_branch={
                                **branch, "effective_edge_configuration_json": json.dumps({
                                    **frozen_edge, "context_mode": other_mode,
                                }),
                            })
                        bad_graph = json.loads(json.dumps(graph))
                        next(row for row in bad_graph["edges"] if row["id"] == edge["id"])[
                            "context_mode"] = other_mode
                        with self.assertRaises(EffectBlocked):
                            payload(current_graph=bad_graph)
                        for column, key, value in (
                            ("effective_edge_configuration_json", "id", "foreign-edge"),
                            ("effective_edge_configuration_json", "workflow_id", workflow_id(1)),
                            ("effective_edge_configuration_json", "target_node_id", "foreign-node"),
                            ("effective_edge_configuration_json", "transition_group_id", "foreign-group"),
                            ("target_snapshot_json", "node_id", "foreign-node"),
                            ("target_snapshot_json", "entered_by_edge_id", "foreign-edge"),
                            ("target_snapshot_json", "node_kind", "script"),
                        ):
                            invalid = {**json.loads(branch[column]), key: value}
                            with self.subTest(column=column, key=key), self.assertRaises(EffectBlocked):
                                payload(current_branch={**branch, column: json.dumps(invalid)})
                        with self.assertRaises(EffectBlocked):
                            payload(current_branch={**branch, "transition_branch_key": "foreign-transition"})
                        with self.assertRaises(EffectBlocked):
                            payload(current_approval={**approval, "source_node_id": "foreign-source"})
                        with self.assertRaises(EffectBlocked):
                            payload(current_approval={**approval, "source_transition_branch_key": "foreign-branch"})
                        with self.assertRaises(EffectBlocked):
                            payload(current_approval={**approval, "source_session_id": "foreign-session"})
                        if mode == "new_session":
                            target = json.loads(branch["target_snapshot_json"])
                            bad_graph = json.loads(json.dumps(graph))
                            next(row for row in bad_graph["nodes"] if row["id"] == target["node_id"])[
                                "kind"] = "script"
                            with self.assertRaises(EffectBlocked):
                                payload(current_graph=bad_graph)
                            with self.assertRaises(EffectBlocked):
                                payload(current_graph=bad_graph, current_branch={
                                    **branch, "target_snapshot_json": json.dumps({**target, "node_kind": "script"}),
                                })

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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(fixture, self, synchronize_effects=True)
            value = plan.value
            item = value["workflows"][0]
            next(edge for edge in item["target_graph"]["edges"] if edge["key"] == "implement_replan")[
                "prompt_template"] = projected.document["graph"]["edges"][0]["prompt_template"]
            from workflowkit.graph import native_graph_postimage

            review = json.loads(context["review_path"].read_text())
            review["target_graph_sha256"] = canonical_sha256(native_graph_postimage(
                item["target_graph"], workflow_id=item["workflow_id"],
            ))
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
            self.assertEqual(actual["graph"], parsed["workflows"][0]["target_graph"])
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
                fixture = make_workflow_fixture(Path(temporary))
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
        cases = [
            (intent, mode)
            for intent in ("graph-only", "graph-and-metadata")
            for mode in ("new_session", "continue_session", "compact_and_continue_session")
        ]
        for intent, mode in cases:
            with self.subTest(intent=intent, mode=mode), tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
                fixture = make_workflow_fixture(Path(temporary))
                state = read_state(fixture)
                state["workflows"][workflow_id(0)]["version"] = 16
                write_state(fixture, state)
                plan, context, observed = native_canonical_v2_plan(
                    fixture, self, production_shape=True, with_approvals=True,
                    intent=intent, synchronize_effects=True,
                    approval_context_mode=mode, prefix_only=True,
                )
                connection = context["connection"]
                source_workflow = read_state(fixture)["workflows"][workflow_id(0)]
                source_graph = {"version": source_workflow["version"], **source_workflow["graph"]}
                # The only graph content change is the reviewed ingress prefix.
                target = json.loads(json.dumps(plan.value["workflows"][0]["target_graph"]))
                source_ingress = operations._v2_ingress_edge(source_graph, "fixture source")
                target_ingress = operations._v2_ingress_edge(target, "fixture target")
                self.assertTrue(target_ingress["prompt_template"].endswith(
                    "\n\n" + source_ingress["prompt_template"],
                ))
                target_ingress["prompt_template"] = source_ingress["prompt_template"]
                target["version"] = source_graph["version"]
                self.assertEqual(target, source_graph)
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
                    from workflowkit.graph import native_graph_postimage
                    item["source_graph_sha256"] = canonical_sha256(native_graph_postimage(
                        item["target_graph"], workflow_id=item["workflow_id"],
                    ))
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
                    from workflowkit.graph import native_graph_postimage

                    review["target_graph_sha256"] = canonical_sha256(native_graph_postimage(
                        item["target_graph"], workflow_id=item["workflow_id"],
                    ))
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

    def test_native_frozen_parameter_description_drift_is_readable_but_requires_decision_review(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            plan, context, observed = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_approvals=True, synchronize_effects=True,
                approval_descriptions=("workflow-delivery-context-v1", "workflow-delivery-context-v1/v2",
                                       "workflow-delivery-context-v1/v2"),
            )
            self.assertEqual(len(observed["compatibility_snapshot"]["native_components"]["approvals"]), 4)
            with native_observer_patches(context), mock.patch.object(operations, "_settle_or_run") as dispatch:
                with self.assertRaisesRegex(PlanValidationError, "omits a changed semantic path"):
                    reconcile_canonical_workflows(plan, mode="prepare")
                dispatch.assert_not_called()

    def test_native_parameter_description_exception_is_strict_and_exact_only_for_unknown_shapes(self) -> None:
        frozen = [
            {"key": "delivery_context", "purpose": "ordinary", "description": "v1", "extra": {"a": 1}},
            {"key": "other", "purpose": "ordinary", "description": "Unchanged"},
        ]
        changed = json.loads(json.dumps(frozen))
        changed[0]["description"] = "v1/v2"
        self.assertTrue(operations._native_parameters_description_compatible(frozen, changed))
        invalid = []
        for key, value in (("key", "renamed"), ("purpose", "target_assignee"), ("extra", {}),
                           ("extra", {"a": True})):
            row = json.loads(json.dumps(changed))
            row[0][key] = value
            invalid.append(row)
        for value in (None, 1, {}, []):
            row = json.loads(json.dumps(changed))
            row[0]["description"] = value
            invalid.append(row)
        missing = json.loads(json.dumps(changed))
        del missing[0]["description"]
        added = json.loads(json.dumps(changed))
        added[0]["unknown"] = None
        invalid.extend((missing, added, changed[::-1], changed[:1], changed + [changed[0]], {}, None))
        for row in invalid:
            with self.subTest(row=row):
                self.assertFalse(operations._native_parameters_description_compatible(frozen, row))
        for unknown in ({}, None, {"input": "v1"}, [{"key": "input"}]):
            self.assertTrue(operations._native_parameters_description_compatible(unknown, unknown))
        self.assertFalse(operations._native_parameters_description_compatible({"input": "v1"}, {"input": "v2"}))
        missing = json.loads(json.dumps(frozen))
        del missing[1]["description"]
        changed = json.loads(json.dumps(missing))
        changed[0]["description"] = "v1/v2"
        self.assertFalse(operations._native_parameters_description_compatible(missing, changed))
        for key in ("key", "purpose"):
            unknown = json.loads(json.dumps(frozen))
            del unknown[0][key]
            changed = json.loads(json.dumps(unknown))
            changed[0]["description"] = "v1/v2"
            self.assertFalse(operations._native_parameters_description_compatible(unknown, changed))

    def test_native_description_review_covers_frozen_current_target_without_changing_approvals(self) -> None:
        for descriptions in (("v1", "v1/v2", "v1/v2"), ("v1", "v1", "v1/v2"),
                             ("v1", "v1/v2", "v1"), ("v1", "v1", "v1")):
            with self.subTest(descriptions=descriptions), tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
                fixture = make_workflow_fixture(Path(temporary))
                plan, context, observed = native_canonical_v2_plan(
                    fixture, self, production_shape=True, with_approvals=True, synchronize_effects=True,
                    intent="graph-and-metadata", approval_descriptions=descriptions,
                )
                tables = ("task_pending_approvals", "task_pending_approval_branches", "task_current_nodes")
                before = [context["connection"].execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                          for table in tables]
                parsed = operations._validate_canonical_v2_plan(plan)
                affected: set[str] = set()
                with native_observer_patches(context):
                    live = operations._canonical_v2_read(parsed, parsed["workflows"][0], affected_decisions=affected)
                    self.assertEqual(live, observed)
                    if len(set(descriptions)) > 1:
                        self.assertEqual(len(affected), 4)
                        with self.assertRaisesRegex(PlanValidationError, "omits a changed semantic path"):
                            reconcile_canonical_workflows(plan, mode="prepare")
                        plan = review_native_description_decisions(fixture, plan, context)
                    else:
                        self.assertEqual(affected, set())
                    self.assertEqual(reconcile_canonical_workflows(plan, mode="prepare")["phase"], "prepared")
                    self.assertEqual(reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)["phase"],
                                     "complete")
                    current = operations._canonical_v2_read(parsed, parsed["workflows"][0])
                self.assertEqual(current["compatibility_snapshot"]["native_components"]["approvals"],
                                 observed["compatibility_snapshot"]["native_components"]["approvals"])
                for table, rows in zip(tables, before):
                    self.assertEqual(context["connection"].execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall(),
                                     rows)
                self.assertEqual(len(graph_apply_rows(fixture)), 1)

    def test_native_description_review_is_only_required_for_affected_decisions(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_approvals=True,
                approval_descriptions=("v1", "v1/v2", "v1/v2"),
            )
            connection = context["connection"]
            identity, raw = connection.execute(
                "SELECT approval_id,effective_edge_configuration_json FROM task_pending_approval_branches "
                "ORDER BY approval_id LIMIT 1"
            ).fetchone()
            # Three frozen decisions already describe the expanded contract.
            edge = json.loads(raw)
            edge["parameters"][0]["description"] = "v1/v2"
            connection.execute(
                "UPDATE task_pending_approval_branches SET effective_edge_configuration_json=? WHERE approval_id!=?",
                (canonical_bytes(edge).decode("utf-8"), identity),
            )
            connection.commit()
            parsed = operations._validate_canonical_v2_plan(plan)
            affected: set[str] = set()
            with native_observer_patches(context):
                observed = operations._canonical_v2_read(parsed, parsed["workflows"][0], affected_decisions=affected)
            self.assertEqual(affected, {identity})
            value = plan.value
            item = value["workflows"][0]
            for task in item["compatibility_snapshot"]["tasks"]:
                live_task = next(row for row in observed["compatibility_snapshot"]["tasks"] if row["id"] == task["id"])
                task["pending_decisions"] = live_task["pending_decisions"]
            item["compatibility_snapshot"]["native_components"] = observed["compatibility_snapshot"]["native_components"]
            review = json.loads(context["review_path"].read_bytes())
            review["compatibility_snapshot_sha256"] = canonical_sha256(item["compatibility_snapshot"])
            raw = canonical_bytes(review)
            context["review_path"].write_bytes(raw)
            item["compatibility_review_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
            plan = review_native_description_decisions(
                fixture, write_canonical_v2_plan(fixture, value), context, {identity},
            )
            with native_observer_patches(context):
                self.assertEqual(reconcile_canonical_workflows(plan, mode="prepare")["phase"], "prepared")

    def test_native_description_drift_after_prepare_and_during_admission_blocks_dispatch(self) -> None:
        for timing in ("after-prepare", "during-admission"):
            with self.subTest(timing=timing), tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
                fixture = make_workflow_fixture(Path(temporary))
                plan, context, _ = native_canonical_v2_plan(
                    fixture, self, production_shape=True, with_approvals=True, synchronize_effects=True,
                    approval_descriptions=("v1", "v1", "v1"),
                )
                def drift():
                    connection = context["connection"]
                    for identity, raw in connection.execute(
                        "SELECT approval_id,effective_edge_configuration_json FROM task_pending_approval_branches"
                    ).fetchall():
                        edge = json.loads(raw)
                        edge["parameters"][0]["description"] = "v1/v2"
                        connection.execute(
                            "UPDATE task_pending_approval_branches SET effective_edge_configuration_json=? "
                            "WHERE approval_id=?", (canonical_bytes(edge).decode("utf-8"), identity),
                        )
                    connection.commit()
                admit = operations._admit_v2_source
                def admit_then_drift(*args, **kwargs):
                    result = admit(*args, **kwargs)
                    drift()
                    return result
                with native_observer_patches(context):
                    reconcile_canonical_workflows(plan, mode="prepare")
                    if timing == "after-prepare":
                        drift()
                    with mock.patch.object(operations, "_admit_v2_source", side_effect=admit_then_drift):
                        with mock.patch.object(operations, "_settle_or_run") as dispatch:
                            with self.assertRaisesRegex(EffectBlocked, "inventory drifted"):
                                reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                            dispatch.assert_not_called()
                self.assertEqual(graph_apply_rows(fixture), [])

    def test_native_description_settlement_does_not_require_review_or_replay(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            plan, context, _ = native_canonical_v2_plan(
                fixture, self, production_shape=True, with_approvals=True, synchronize_effects=True,
                approval_descriptions=("v1", "v1/v2", "v1/v2"),
            )
            plan = review_native_description_decisions(fixture, plan, context)
            with native_observer_patches(context):
                reconcile_canonical_workflows(plan, mode="prepare")
                install_v2_unresolved_graph_postimage(fixture, plan)
                context["connection"].execute("UPDATE workflows SET version=2")
                context["connection"].commit()
                context["review_path"].unlink()
                with mock.patch.object(operations, "_canonical_v2_plan_check",
                                       side_effect=AssertionError("settlement must not require review")):
                    report = reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)
                self.assertEqual(report["settled"], "postimage")
                self.assertEqual(reconcile_canonical_workflows(plan, mode="apply", confirm=plan.sha256)["phase"],
                                 "complete")
            self.assertEqual(graph_apply_rows(fixture), [])

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
                fixture = make_workflow_fixture(Path(temporary))
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
                fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
                fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            self.assertEqual(task["current_nodes"], ["85142bca-99b5-4f0b-b364-f5296df95187"])
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
                "UPDATE task_current_nodes SET node_id='85142bca-99b5-4f0b-b364-f5296df95187' WHERE task_id='TASK-14'"
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
                fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
                "3805a1a5-d036-492b-8564-e772d71b8f5a": changed(lambda graph: graph["nodes"][0].update(prompt=expected)),
                "aab2d276-bae8-4a6c-ab53-20f4b1c4423f": changed(lambda graph: graph["transition_groups"][0].update(prompt=expected)),
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

    def test_source_gate_accepts_exact_native_frame_without_loosening_suffix_boundary(self):
        from workflowkit.delivery import SOURCE_ADMISSION_END, source_admission_gate
        from workflowkit.graph import native_graph_postimage

        for schema in ("delivery-source-admission-v1", "delivery-source-admission-v2"):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as temporary:
                fixture = make_workflow_fixture(Path(temporary))
                _, value = canonical_v2_plan(fixture)
                item = value["workflows"][0]
                source = {"version": item["expected_version"],
                          **read_state(fixture)["workflows"][workflow_id(0)]["graph"]}
                target = item["target_graph"]
                ingress = operations._v2_ingress_edge(target, "fixture")
                admission = item["source_admission"]
                if schema.endswith("-v2"):
                    admission["schema"] = schema
                    admission["binding"] = {
                        "kind": "r2-installed",
                        "installation_binding_sha256": admission.pop("installation_binding_sha256"),
                    }
                    admission["ingress"] = {"kind": "agent-edge", "edge_id": ingress["id"]}
                gate = source_admission_gate(
                    admission, project_id=PROJECT_ID, workflow_id=item["workflow_id"],
                    project_root=str(fixture["root"]),
                )
                for prompt in (gate + "\n\n", gate + "\n\nReviewed ingress body."):
                    ingress["prompt_template"] = prompt
                    encoded = native_graph_postimage(target, workflow_id=item["workflow_id"])
                    operations._v2_source_gate_check(
                        source, encoded, admission, project_id=PROJECT_ID,
                        workflow_id=item["workflow_id"], project_root=Path(fixture["root"]),
                    )
                for prompt in (
                    gate + "Unseparated suffix", gate + "\nSingle newline suffix",
                    gate.replace(SOURCE_ADMISSION_END, ""),
                    gate.replace(admission["receipt_ref"]["sha256"], "0" * 64, 1),
                    gate + "\n\n" + gate,
                ):
                    with self.subTest(prompt=prompt[-40:]):
                        ingress["prompt_template"] = prompt
                        with self.assertRaises(PlanValidationError):
                            operations._v2_source_gate_check(
                                source, native_graph_postimage(target, workflow_id=item["workflow_id"]),
                                admission, project_id=PROJECT_ID, workflow_id=item["workflow_id"],
                                project_root=Path(fixture["root"]),
                            )

    def test_v2_closed_admission_union_and_exact_script_ingress(self) -> None:
        from workflowkit.delivery import source_admission_gate

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            admission = {
                "schema": "delivery-source-admission-v2",
                "receipt_ref": {"path": str(root / "receipt.json"), "sha256": "a" * 64},
                "primary_workspace_id": "workspace-test",
                "primary_root": str(root),
                "kit_root": str(root / "kit"),
                "source_ref": "refs/remotes/origin/main",
                "kit_ref": "refs/remotes/origin/main",
                "ci_helper": str(root / "bin/kent-prepare-github-ci"),
                "helper_bindings": [],
                "binding": {"kind": "plan-publication"},
                "ingress": {"kind": "script-edge", "edge_id": "000d0e6b-58b4-435a-a9f9-cb32f7d420ab"},
            }
            parsed = operations._v2_source_admission(admission, "source_admission")
            self.assertEqual(parsed, admission)
            for invalid in (
                {**admission, "extra": True},
                {**admission, "binding": {"kind": "plan-publication", "extra": True}},
                {**admission, "ingress": {"kind": "script-edge", "edge_id": "000d0e6b-58b4-435a-a9f9-cb32f7d420ab",
                                          "extra": True}},
                {**admission, "ingress": {"kind": "start-edge", "edge_id": "000d0e6b-58b4-435a-a9f9-cb32f7d420ab"}},
            ):
                with self.subTest(invalid=invalid), self.assertRaises(PlanValidationError):
                    operations._v2_source_admission(invalid, "source_admission")
            r2 = {
                **admission,
                "binding": {
                    "kind": "r2-installed",
                    "installation_binding_sha256": "b" * 64,
                },
            }
            self.assertEqual(
                operations._v2_source_admission(r2, "source_admission")["binding"],
                r2["binding"],
            )

            source_graph = {
                "version": 3,
                "nodes": [
                    {"id": "6a736520-a86d-47e4-9312-01f09452d6f8", "key": "backlog", "kind": "agent"},
                    {"id": "fd12cf94-4cac-4f01-87f0-8f7739927d74", "key": "entry", "kind": "agent"},
                    {"id": "90e31940-4c84-466e-8e72-26f53c2f36f4", "key": "terminal", "kind": "terminal"},
                ],
                "node_groups": [],
                "transition_groups": [{
                    "id": "e6eae46e-32a1-4caa-a82f-07a21ff739e8", "source_node_id": "6a736520-a86d-47e4-9312-01f09452d6f8",
                    "transition_id": "ingress",
                }],
                "edges": [{
                    "id": "000d0e6b-58b4-435a-a9f9-cb32f7d420ab", "key": "ingress",
                    "transition_group_id": "e6eae46e-32a1-4caa-a82f-07a21ff739e8",
                    "target_node_id": "fd12cf94-4cac-4f01-87f0-8f7739927d74", "prompt_template": "Begin.",
                }],
            }
            target_graph = json.loads(json.dumps(source_graph))
            target_graph["nodes"].append({
                "id": "39fa54f4-f723-491e-a278-ba8547f7ba06", "key": "new-script", "kind": "script",
                "script_path": "scripts/new-script",
            })
            target_graph["edges"][0]["target_node_id"] = "39fa54f4-f723-491e-a278-ba8547f7ba06"
            target_graph["transition_groups"].append({
                "id": "b2c8811e-7901-4d0c-b3fd-fb901f31a089", "source_node_id": "39fa54f4-f723-491e-a278-ba8547f7ba06",
                "transition_id": "done",
            })
            target_graph["edges"].append({
                "id": "51bcad13-8afc-4be8-b3c6-57a244431c18", "key": "script-out",
                "transition_group_id": "b2c8811e-7901-4d0c-b3fd-fb901f31a089",
                "target_node_id": "fd12cf94-4cac-4f01-87f0-8f7739927d74",
            })
            target_graph["version"] = 4
            runtime_refs = [{
                "path": str(root / "scripts/new-script"),
                "relative_path": "scripts/new-script",
                "mode": 0o755,
            }]
            operations._v2_graph_compatibility(source_graph, target_graph, runtime_refs)

            gate = source_admission_gate(
                admission,
                project_id=PROJECT_ID,
                workflow_id=workflow_id(0),
                project_root=str(root),
            )
            target_graph["edges"][0]["prompt_template"] = gate + "\n\nBegin."
            operations._v2_source_gate_check(
                source_graph, target_graph, admission,
                project_id=PROJECT_ID, workflow_id=workflow_id(0), project_root=root,
            )
            bypass = json.loads(json.dumps(target_graph))
            bypass["transition_groups"].append({
                "id": "e64c0988-f90a-4f65-aa51-0de1f6176394", "source_node_id": "6a736520-a86d-47e4-9312-01f09452d6f8",
                "transition_id": "bypass",
            })
            bypass["edges"].append({
                "id": "33e7a1f0-cf9b-4746-8e02-a1bd9c8cd667", "key": "bypass",
                "transition_group_id": "e64c0988-f90a-4f65-aa51-0de1f6176394",
                "target_node_id": "fd12cf94-4cac-4f01-87f0-8f7739927d74",
            })
            with self.assertRaisesRegex(PlanValidationError, "bypasses"):
                operations._v2_source_gate_check(
                    source_graph, bypass, admission,
                    project_id=PROJECT_ID, workflow_id=workflow_id(0), project_root=root,
                )

            for invalid_graph in (
                {**target_graph, "nodes": [
                    {**row, "kind": "agent"} if row["id"] == "39fa54f4-f723-491e-a278-ba8547f7ba06" else row
                    for row in target_graph["nodes"]
                ]},
                {**target_graph, "nodes": [
                    {**row, "script_path": "scripts/missing"} if row["id"] == "39fa54f4-f723-491e-a278-ba8547f7ba06" else row
                    for row in target_graph["nodes"]
                ]},
                {
                    **target_graph,
                    "edges": [
                        row for row in target_graph["edges"]
                        if row["id"] != "51bcad13-8afc-4be8-b3c6-57a244431c18"
                    ],
                },
            ):
                with self.subTest(invalid_graph=invalid_graph), self.assertRaises(PlanValidationError):
                    operations._v2_graph_compatibility(
                        source_graph, invalid_graph, runtime_refs,
                    )
            with self.assertRaisesRegex(PlanValidationError, "removes existing edges"):
                operations._v2_graph_compatibility(
                    source_graph,
                    {
                        **target_graph,
                        "edges": [
                            row for row in target_graph["edges"]
                            if row["id"] != "000d0e6b-58b4-435a-a9f9-cb32f7d420ab"
                        ],
                    },
                    runtime_refs,
                )

            review_paths = operations._v2_required_review_paths(
                source_graph,
                target_graph,
                {"name": "before", "description": "", "execution_target": "head"},
                {"name": "after", "description": "", "execution_target": "head"},
                {"tasks": [{"id": "task-1", "pending_decisions": [{"id": "decision-1"}]}],
                 "sessions": []},
                admission,
            )
            self.assertTrue({
                "graph.added_node:39fa54f4-f723-491e-a278-ba8547f7ba06",
                "graph.added_edge:51bcad13-8afc-4be8-b3c6-57a244431c18",
                "graph.added_transition_group:b2c8811e-7901-4d0c-b3fd-fb901f31a089",
                "graph.edges:000d0e6b-58b4-435a-a9f9-cb32f7d420ab.prompt_template",
                "graph.edges:000d0e6b-58b4-435a-a9f9-cb32f7d420ab.target_node_id",
                "source_admission.ingress",
                "task:task-1",
            }.issubset(review_paths))
            approved_source = json.loads(json.dumps(source_graph))
            approved_target = json.loads(json.dumps(target_graph))
            approved_source["edges"][0]["requires_approval"] = True
            approved_target["edges"][0]["requires_approval"] = False
            authority_paths = operations._v2_required_review_paths(
                approved_source,
                approved_target,
                {"name": "before", "description": "", "execution_target": "head"},
                {"name": "after", "description": "", "execution_target": "head"},
                {"tasks": [{"id": "task-1", "pending_decisions": [
                    {"id": "decision-1"},
                ]}], "sessions": []},
            )
            self.assertTrue({
                "graph.edges:000d0e6b-58b4-435a-a9f9-cb32f7d420ab.requires_approval",
                "authority:graph.edges:000d0e6b-58b4-435a-a9f9-cb32f7d420ab.requires_approval",
                "decision:decision-1",
            }.issubset(authority_paths))

    def test_native_script_admission_reads_exact_frame_and_denies_without_effect(self) -> None:
        from workflowkit.delivery import (
            admit_script_source_ingress,
            source_admission_gate,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            wid = workflow_id(0)
            admission = {
                "schema": "delivery-source-admission-v2",
                "receipt_ref": {"path": str(root / "receipt.json"), "sha256": "a" * 64},
                "primary_workspace_id": "workspace-test",
                "primary_root": str(root),
                "kit_root": str(root / "kit"),
                "source_ref": "refs/remotes/origin/main",
                "kit_ref": "refs/remotes/origin/main",
                "ci_helper": str(root / "bin/kent-prepare-github-ci"),
                "helper_bindings": [],
                "binding": {"kind": "plan-publication"},
                "ingress": {"kind": "script-edge", "edge_id": "000d0e6b-58b4-435a-a9f9-cb32f7d420ab"},
            }
            gate = source_admission_gate(
                admission,
                project_id=PROJECT_ID,
                workflow_id=wid,
                project_root=str(root),
            )
            detail = {
                "summary": {
                    "id": "task-native",
                    "project_id": PROJECT_ID,
                    "workflow_id": wid,
                },
                "source_workspace": {
                    "root_path": str(root),
                    "is_primary": True,
                    "workspace_id": "workspace-test",
                },
                "project": {"default_workspace_id": "workspace-test"},
                "worktree_path": None,
                "current_scripts": [{
                    "current_node": {
                        "node_id": "39fa54f4-f723-491e-a278-ba8547f7ba06",
                        "transition_branch_key": "ingress",
                    },
                    "path": "scripts/entry.py",
                }],
            }
            frame_graph = {
                "workflow_id": wid,
                "graph": {
                    "nodes": [
                        {"id": "6a736520-a86d-47e4-9312-01f09452d6f8", "key": "backlog", "kind": "agent"},
                        {"id": "39fa54f4-f723-491e-a278-ba8547f7ba06", "key": "entry", "kind": "script",
                         "script_path": "scripts/entry.py"},
                        {"id": "7037a77d-c256-42d8-b649-563a192670d6", "key": "next", "kind": "agent"},
                    ],
                    "node_groups": [],
                    "transition_groups": [
                        {"id": "01222959-c666-41be-94e5-ad30d49f05fe", "source_node_id": "6a736520-a86d-47e4-9312-01f09452d6f8",
                         "transition_id": "ingress"},
                        {"id": "983af8f5-cafb-484d-9813-a93ed967d886", "source_node_id": "39fa54f4-f723-491e-a278-ba8547f7ba06",
                         "transition_id": "done"},
                    ],
                    "edges": [
                        {"id": "000d0e6b-58b4-435a-a9f9-cb32f7d420ab", "key": "ingress",
                         "transition_group_id": "01222959-c666-41be-94e5-ad30d49f05fe",
                         "target_node_id": "39fa54f4-f723-491e-a278-ba8547f7ba06",
                         "prompt_template": gate + "\n\nRun script."},
                        {"id": "910b91eb-688b-4890-835b-e897f16b82b0", "key": "done",
                         "transition_group_id": "983af8f5-cafb-484d-9813-a93ed967d886",
                         "target_node_id": "7037a77d-c256-42d8-b649-563a192670d6"},
                    ],
                },
            }
            kent = Path(sys.executable).resolve()
            context = {
                "task_id": "task-native",
                "node_id": "39fa54f4-f723-491e-a278-ba8547f7ba06",
                "transition_branch_key": "ingress",
            }
            project_read = mock.Mock(return_value=(0, (PROJECT_ID + "\n").encode(), b""))
            native_json = mock.Mock(side_effect=[detail, frame_graph])
            delegated = mock.Mock(return_value=object())
            provider = mock.Mock()
            with mock.patch.object(operations, "_run", project_read), \
                    mock.patch.object(operations, "_kent_json", native_json), \
                    mock.patch.object(operations, "admit_source_admission", delegated):
                result = admit_script_source_ingress(
                    root, context, kent_path=kent,
                )
                self.assertIs(result, delegated.return_value)
                self.assertEqual(delegated.call_count, 1)
                self.assertEqual(delegated.call_args.kwargs["project_id"], PROJECT_ID)
                self.assertEqual(delegated.call_args.kwargs["workflow_id"], wid)
                self.assertEqual(
                    delegated.call_args.kwargs["workspace_path"], str(root),
                )
                self.assertEqual(delegated.call_args.args[0], admission)
                provider.assert_not_called()

            document = root / "ingress.json"
            root.chmod(0o700)
            description = operations.create_script_ingress_reference(
                document, admission, project_id=PROJECT_ID, workflow_id=wid,
            )
            with self.assertRaises(FileExistsError):
                operations.create_script_ingress_reference(
                    document, admission, project_id=PROJECT_ID, workflow_id=wid,
                )
            compact_graph = json.loads(json.dumps(frame_graph))
            compact_graph["graph"]["edges"][0].pop("prompt_template")
            compact_graph["graph"]["transition_groups"][0]["description"] = description
            from workflowkit.graph import native_graph_postimage
            encoded = native_graph_postimage(compact_graph["graph"], workflow_id=wid)
            self.assertNotIn("prompt_template", encoded["edges"][0])
            self.assertEqual(encoded["transition_groups"][0]["description"], description)
            operations._v2_source_gate_check(
                frame_graph["graph"], encoded, admission,
                project_id=PROJECT_ID, workflow_id=wid, project_root=root,
            )
            with mock.patch.object(operations, "_run", project_read), \
                    mock.patch.object(operations, "_kent_json", side_effect=[detail, compact_graph]), \
                    mock.patch.object(operations, "admit_source_admission", delegated):
                self.assertIs(admit_script_source_ingress(root, context, kent_path=kent),
                              delegated.return_value)
            operations._v2_source_gate_check(
                frame_graph["graph"], compact_graph["graph"], admission,
                project_id=PROJECT_ID, workflow_id=wid, project_root=root,
            )
            description = compact_graph["graph"]["transition_groups"][0]["description"]
            with self.assertRaises(PlanValidationError):
                operations.read_script_ingress_reference(description + " ")
            document.chmod(0o644)
            with self.assertRaises(PlanValidationError):
                operations.read_script_ingress_reference(description)
            document.chmod(0o600)
            document.write_bytes(b"{}")
            with self.assertRaises(PlanValidationError):
                operations.read_script_ingress_reference(description)
            document.write_bytes(canonical_bytes({
                "project_id": PROJECT_ID, "workflow_id": wid,
                "source_admission": admission,
            }))
            alias = root / "linked-ingress.json"
            os.link(document, alias)
            with self.assertRaises(PlanValidationError):
                operations.read_script_ingress_reference(description)
            alias.unlink()
            alias.symlink_to(document)
            linked_reference = json.loads(description)
            linked_reference["path"] = str(alias)
            with self.assertRaises(PlanValidationError):
                operations.read_script_ingress_reference(canonical_bytes(linked_reference).decode())
            foreign = dict(admission, kit_root=str(root / "foreign-kit"))
            with self.assertRaises(PlanValidationError):
                operations._v2_source_gate_check(
                    frame_graph["graph"], compact_graph["graph"], foreign,
                    project_id=PROJECT_ID, workflow_id=wid, project_root=root,
                )
            dual = json.loads(json.dumps(compact_graph))
            dual["graph"]["edges"][0]["prompt_template"] = gate
            invalid_graphs = [dual]
            wrong_edge = json.loads(json.dumps(frame_graph))
            wrong_edge["graph"]["edges"][0]["id"] = "0f9e86ff-97fd-4c71-9264-82e4ded41473"
            invalid_graphs.append(wrong_edge)
            wrong_target = json.loads(json.dumps(frame_graph))
            wrong_target["graph"]["edges"][0]["target_node_id"] = "7037a77d-c256-42d8-b649-563a192670d6"
            invalid_graphs.append(wrong_target)
            later_script = json.loads(json.dumps(frame_graph))
            later_script["graph"]["nodes"].append({
                "id": "3c0b265c-c6b1-4404-96b5-7f3fb162b84f",
                "key": "later",
                "kind": "script",
                "script_path": "scripts/later.py",
            })
            later_script["graph"]["edges"][0]["target_node_id"] = "3c0b265c-c6b1-4404-96b5-7f3fb162b84f"
            invalid_graphs.append(later_script)
            bypass = json.loads(json.dumps(frame_graph))
            bypass["graph"]["transition_groups"].append({
                "id": "e64c0988-f90a-4f65-aa51-0de1f6176394", "source_node_id": "6a736520-a86d-47e4-9312-01f09452d6f8",
                "transition_id": "bypass",
            })
            bypass["graph"]["edges"].append({
                "id": "33e7a1f0-cf9b-4746-8e02-a1bd9c8cd667", "key": "bypass",
                "transition_group_id": "e64c0988-f90a-4f65-aa51-0de1f6176394",
                "target_node_id": "7037a77d-c256-42d8-b649-563a192670d6",
            })
            invalid_graphs.append(bypass)
            for invalid in invalid_graphs:
                with self.subTest(graph=invalid):
                    with mock.patch.object(operations, "_run", project_read), \
                            mock.patch.object(
                                operations, "_kent_json",
                                side_effect=[detail, invalid],
                            ), \
                            mock.patch.object(
                                operations, "admit_source_admission",
                                side_effect=AssertionError(
                                    "admission must not be delegated"
                                ),
                            ) as denied, \
                            self.assertRaises(PlanValidationError):
                        admit_script_source_ingress(
                            root, context, kent_path=kent,
                        )
                    denied.assert_not_called()
                    provider.assert_not_called()

    def test_v2_every_mutable_existing_graph_field_requires_review_disposition(self) -> None:
        from workflowkit.graph import native_graph_projection

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            plan, value = canonical_v2_plan(
                fixture, script_path_change=True, pending_approval=True,
            )
            item = value["workflows"][0]
            workflow = read_state(fixture)["workflows"][item["workflow_id"]]
            source_graph = {
                "version": item["expected_version"],
                **workflow["graph"],
            }
            next(
                row for row in source_graph["edges"]
                if row["id"] == "9c264349-da25-4402-adb6-53780bd0b7fd"
            )["requires_approval"] = True
            target_graph = item["target_graph"]
            script = next(row for row in target_graph["nodes"] if row["id"] == "39fa54f4-f723-491e-a278-ba8547f7ba06")
            script.update({
                "key": "janitor-reviewed",
                "name": "Reviewed Script",
                "description": "Reviewed Script description",
                "subagent_role": "not-used-by-script",
                "prompt_template": "Script prompt template",
                "prompt": "Script prompt",
                "context": {"reviewed": True},
                "parameters": [{"key": "mode", "type": "string"}],
            })
            edge = next(row for row in target_graph["edges"] if row["id"] == "9c264349-da25-4402-adb6-53780bd0b7fd")
            edge.update({
                "key": "fix_replan_reviewed",
                "target_node_id": "8bda2875-9d3e-40e4-886c-25bb48f5f3aa",
                "transition_group_id": "c6a3f68e-7629-404a-8153-2d5f0e32ff0d",
                "requires_approval": False,
                "parameters": [{"key": "mode", "type": "string"}],
                "context_mode": "continue_session",
                "context_source": {"kind": "selected_node", "node_key": "plan"},
                "assignee_selection": "previous_node",
                "thinking_selection": "high",
                "prompt_template": "Reviewed prompt template",
                "prompt": "Reviewed prompt",
                "description": "Reviewed edge description",
            })
            group = next(
                row for row in target_graph["transition_groups"]
                if row["id"] == "c60a77d2-3672-49fc-8aea-fe5c37f0110a"
            )
            group.update({
                "transition_id": "fix_replan_reviewed",
                "source_node_id": "7d280478-d98b-4462-9ae6-f58bf0d5a8d0",
                "display_name": "Reviewed transition",
                "description": "Reviewed transition description",
            })

            source_metadata = {
                key: workflow["metadata"][key]
                for key in ("name", "description", "execution_target")
            }
            projected_source = native_graph_projection(
                source_graph, workflow_id=item["workflow_id"],
            )
            projected_target = native_graph_projection(
                target_graph, workflow_id=item["workflow_id"],
            )
            required = operations._v2_required_review_paths(
                projected_source,
                projected_target,
                source_metadata,
                source_metadata,
                item["compatibility_snapshot"],
                item["source_admission"],
            )
            expected_fields = {
                "nodes": (
                    "key", "name", "description", "script_path", "subagent_role",
                    "prompt_template", "prompt", "context", "parameters",
                ),
                "edges": (
                    "key", "target_node_id", "transition_group_id", "requires_approval",
                    "parameters", "context_mode", "context_source", "assignee_selection",
                    "thinking_selection", "prompt_template", "prompt", "description",
                ),
                "transition_groups": (
                    "transition_id", "source_node_id", "display_name", "description",
                ),
            }
            field_paths = {
                f"graph.{collection}:{identity}.{field}"
                for collection, identity, fields in (
                    ("nodes", "39fa54f4-f723-491e-a278-ba8547f7ba06", expected_fields["nodes"]),
                    ("edges", "9c264349-da25-4402-adb6-53780bd0b7fd", expected_fields["edges"]),
                    ("transition_groups", "c60a77d2-3672-49fc-8aea-fe5c37f0110a",
                     expected_fields["transition_groups"]),
                )
                for field in fields
            }
            pending_decision = item["compatibility_snapshot"]["tasks"][0][
                "pending_decisions"
            ][0]["id"]
            authority_paths = {
                "authority:graph.edges:9c264349-da25-4402-adb6-53780bd0b7fd.requires_approval",
                f"decision:{pending_decision}",
            }
            self.assertTrue(authority_paths.issubset(required))
            self.assertEqual(field_paths - required, set())

            review_path = Path(item["compatibility_review_ref"]["path"])
            review = json.loads(review_path.read_text())
            review["source_graph_sha256"] = canonical_sha256(source_graph)
            from workflowkit.graph import native_graph_postimage
            review["target_graph_sha256"] = canonical_sha256(native_graph_postimage(
                target_graph, workflow_id=item["workflow_id"],
            ))
            evidence = review["dispositions"][0]["evidence"]
            review["dispositions"] = [
                {"path": path, "disposition": "compatible", "evidence": evidence}
                for path in sorted(required)
            ]
            item["source_graph_sha256"] = canonical_sha256(source_graph)
            raw_review = canonical_bytes(review)
            review_path.write_bytes(raw_review)
            item["compatibility_review_ref"]["sha256"] = hashlib.sha256(
                raw_review
            ).hexdigest()
            plan = write_canonical_v2_plan(fixture, value)
            parsed = operations._validate_canonical_v2_plan(plan)
            source = {
                "workflow_id": item["workflow_id"],
                "project_id": item["project_id"],
                "version": item["expected_version"],
                "graph": source_graph,
                "metadata": source_metadata,
                "links": item["links"],
                "default": item["default"],
            }
            target_item = parsed["workflows"][0]
            operations._validate_canonical_v2_target(
                parsed, target_item, source, None, None,
            )
            for path in sorted(field_paths | authority_paths):
                with self.subTest(path=path):
                    accepted = review["dispositions"]
                    review["dispositions"] = [
                        row for row in accepted if row["path"] != path
                    ]
                    raw_review = canonical_bytes(review)
                    review_path.write_bytes(raw_review)
                    target_item["compatibility_review_ref"]["sha256"] = hashlib.sha256(
                        raw_review
                    ).hexdigest()
                    with self.assertRaisesRegex(
                        PlanValidationError,
                        "omits a changed semantic path",
                    ):
                        operations._validate_canonical_v2_target(
                            parsed, target_item, source, None, None,
                        )
                    review["dispositions"] = accepted

    def test_composed_builder_parameters_and_prompt_reviews_are_exact(self) -> None:
        from workflowkit.graph import native_graph_postimage, native_graph_projection, plan_workflow_graph
        from workflowkit.delivery import source_admission_gate

        root = Path(__file__).resolve().parents[1]
        builder = runpy.run_path(str(root / ".kent/workflows/kit_development.py"))
        profile = builder["profile_at"](root, check_files=False)
        for complexity in (False, True):
            with self.subTest(complexity=complexity), tempfile.TemporaryDirectory() as temporary:
                current_profile = replace(
                    profile, policies={**profile.policies, "coder_selection": "complexity" if complexity else "disabled"},
                )
                spec = builder["build_current_workflow"](current_profile)
                empty = {"workflow_id": workflow_id(0), "expected_version": 16,
                         "graph": {key: [] for key in ("nodes", "edges", "node_groups", "transition_groups")}}
                source_graph = plan_workflow_graph(spec, empty, metadata_changed=False).document["graph"]
                # Model the one admitted migration from the current public graph,
                # without importing a retired historical builder.
                replan = next(edge for edge in source_graph["edges"] if edge["key"] == "implement_replan")
                source_graph["edges"] = [
                    edge for edge in source_graph["edges"] if edge["id"] != replan["id"]
                ]
                source_graph["transition_groups"] = [
                    group for group in source_graph["transition_groups"]
                    if group["id"] != replan["transition_group_id"]
                ]
                ingress_id = operations._v2_ingress_edge(source_graph, "previous graph")["id"]
                previous_prompt = next(
                    edge for edge in source_graph["edges"]
                    if edge.get("prompt_template") and edge["id"] != ingress_id
                )
                previous_prompt["prompt_template"] = "Previously reviewed transition instructions."
                target = plan_workflow_graph(
                    spec, {**empty, "graph": source_graph}, metadata_changed=False).document["graph"]
                source_graph["version"] = 16
                target["version"] = 17
                fixture = make_workflow_fixture(Path(temporary))
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
                replan_edge_id = next(
                    edge["id"] for edge in target["edges"]
                    if edge["key"] == "implement_replan"
                )
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
                    operations._v2_graph_compatibility(source_graph, bad, item["runtime_refs"])
                    required = operations._v2_required_review_paths(
                        source_graph, bad, {}, {}, {"tasks": [], "sessions":[]},
                    )
                    self.assertIn(f"graph.added_edge:{replan_edge_id}", required)
                typed_source = json.loads(json.dumps(source_graph))
                typed_target = json.loads(json.dumps(target))
                for graph in (typed_source, typed_target):
                    next(edge for edge in graph["edges"] if edge["key"] == "fix_replan")[
                        "parameters"][0]["default"] = True
                next(edge for edge in typed_target["edges"] if edge["key"] == "implement_replan")[
                    "parameters"][0]["default"] = 1
                operations._v2_graph_compatibility(typed_source, typed_target, item["runtime_refs"])
                required = operations._v2_required_review_paths(
                    typed_source, typed_target, {}, {}, {"tasks": [], "sessions":[]},
                )
                self.assertIn(f"graph.added_edge:{replan_edge_id}", required)
                for duplicate in (False, True):
                    bad_source = json.loads(json.dumps(source_graph))
                    fix = next(edge for edge in bad_source["edges"] if edge["key"] == "fix_replan")
                    if duplicate:
                        bad_source["edges"].append({**fix, "id": "f7b7744f-bc88-443b-bfde-f43c9c401bab"})
                        bad_target = json.loads(json.dumps(target))
                        bad_target["edges"].append({**fix, "id": "f7b7744f-bc88-443b-bfde-f43c9c401bab"})
                    else:
                        bad_source["edges"].remove(fix)
                        bad_target = json.loads(json.dumps(target))
                        bad_target["edges"] = [edge for edge in bad_target["edges"] if edge["id"] != fix["id"]]
                    if duplicate:
                        with self.assertRaisesRegex(PlanValidationError, "duplicate identities"):
                            operations._v2_graph_compatibility(
                                bad_source, bad_target, item["runtime_refs"],
                            )
                    else:
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
                              target_graph_sha256=canonical_sha256(native_graph_postimage(
                                  target, workflow_id=item["workflow_id"],
                              )),
                              target_metadata_sha256=canonical_sha256(source["metadata"]))
                required = operations._v2_required_review_paths(
                    native_graph_projection(source_graph, workflow_id=item["workflow_id"]),
                    native_graph_projection(target, workflow_id=item["workflow_id"]),
                    source["metadata"], source["metadata"], item["compatibility_snapshot"],
                )
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
            fixture = make_workflow_fixture(Path(temporary))
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
            self.assertIn("graph.nodes:39fa54f4-f723-491e-a278-ba8547f7ba06.script_path", required)

            unqualified = json.loads(json.dumps(target_graph))
            next(node for node in unqualified["nodes"] if node["id"] == "39fa54f4-f723-491e-a278-ba8547f7ba06")[
                "script_path"
            ] = str(Path(fixture["root"]) / "unqualified-helper")
            with self.assertRaises(PlanValidationError):
                operations._v2_graph_compatibility(
                    source_graph,
                    unqualified,
                    item["runtime_refs"],
                )
            changed_semantics = json.loads(json.dumps(target_graph))
            next(node for node in changed_semantics["nodes"] if node["id"] == "39fa54f4-f723-491e-a278-ba8547f7ba06")[
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
                next(node for node in live["nodes"] if node["id"] == "39fa54f4-f723-491e-a278-ba8547f7ba06")[
                    "script_path"
                ],
                target_graph["nodes"][-1]["script_path"],
            )

    def test_v2_review_must_bind_source_admission_and_metadata_only_null_is_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
            allowed, _ = canonical_v2_plan(
                fixture,
                intent="metadata-only",
                source_admission_enabled=False,
            )
            report = reconcile_canonical_workflows(allowed, mode="prepare")
            self.assertEqual(report["phase"], "prepared")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
            plan, _ = canonical_v2_plan(
                fixture,
                intent="graph-only",
                source_admission_enabled=False,
            )
            with self.assertRaises(PlanValidationError):
                reconcile_canonical_workflows(plan, mode="prepare")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
                if row["path"] != "graph.nodes:39fa54f4-f723-491e-a278-ba8547f7ba06.script_path"
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
                fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            detail["current_nodes"] = [{"node_id": "7d280478-d98b-4462-9ae6-f58bf0d5a8d0"}]
            detail.pop("pending_approvals")
            write_state(fixture, state)

            with self.assertRaisesRegex(EffectBlocked, "approval inventory is unavailable"):
                operations._canonical_v2_read(parsed, item)

    def test_v2_current_scripts_are_required_and_active_scripts_block(self) -> None:
        for script_state in ("missing", "malformed", "active"):
            with self.subTest(script_state=script_state), tempfile.TemporaryDirectory() as temporary:
                fixture = make_workflow_fixture(Path(temporary))
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
                        "current_node": {"node_id": "7d280478-d98b-4462-9ae6-f58bf0d5a8d0"},
                    }]
                write_state(fixture, state)

                with self.assertRaisesRegex(
                    EffectBlocked,
                    "current Script|active native Script",
                ):
                    operations._canonical_v2_read(parsed, item)

    def test_v2_empty_current_scripts_prove_idle_without_live_executions_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
                fixture = make_workflow_fixture(Path(temporary))
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
                        "current_nodes": ["85142bca-99b5-4f0b-b364-f5296df95187"],
                    })
                    detail.update({
                        "status": active,
                        "current_nodes": [{"node_id": "85142bca-99b5-4f0b-b364-f5296df95187"}],
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
            plan, value = canonical_v2_plan(fixture)
            value["workflows"][0]["target_graph"]["edges"][0][
                "target_node_id"
            ] = "90e31940-4c84-466e-8e72-26f53c2f36f4"
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
            fixture = make_workflow_fixture(Path(temporary))
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
                '        detail["current_nodes"] = [{"node_id": "7d280478-d98b-4462-9ae6-f58bf0d5a8d0"}]\n'
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
            fixture = make_workflow_fixture(Path(temporary))
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
            detail["current_nodes"] = [{"node_id": "7d280478-d98b-4462-9ae6-f58bf0d5a8d0"}]
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
                fixture = make_workflow_fixture(Path(temporary))
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
            fixture = make_workflow_fixture(Path(temporary))
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
                {"node_id": "7d280478-d98b-4462-9ae6-f58bf0d5a8d0"}
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

if __name__ == "__main__":
    unittest.main()
