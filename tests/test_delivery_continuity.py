from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from workflowkit.delivery import (
    build_canary_workflow, build_delivery_workflow, build_smoke_lab_workflow,
    cleanup_prompt, published_cleanup_prompt, qualify_transition_keys,
)
from workflowkit.delivery import (
    capture_producer, capture_script_producer, observe_script_identity,
    pack_delivery, producer_reference_closure, reconstruct_producer, transition_contract_reference,
)
from workflowkit.profile import ProjectProfile
from workflowkit.model import EdgeSpec, ParameterSpec
from workflowkit.graph import graph_matches_spec, plan_workflow_graph


REPO_ROOT = Path(__file__).resolve().parents[1]

class GeneratedStageContractsTests(unittest.TestCase):
    def test_writer_and_owner_recover_original_context_before_escalation(self):
        from workflowkit.delivery import implement_prompt, owner_replan_instruction

        current = ProjectProfile.from_toml(
            REPO_ROOT, (REPO_ROOT / ".kent/workflow-profile.toml").read_text(),
            check_files=False,
        )
        for profile in (self.profile(), runtime_v2_profile(ci=False), current):
            for selected in (False, True):
                candidate = replace(profile, policies={
                    **profile.policies, "writer_sessions": "continuous",
                    "coder_selection": "complexity" if selected else "disabled",
                })
                for fresh in (False, True):
                    with self.subTest(memory=profile.project_memory,
                                      selected=selected, fresh=fresh):
                        writer = implement_prompt(
                            candidate, fresh_session=fresh, coder_selection=selected)
                        self.assertNotIn("as the complete handoff", writer)
                        if fresh:
                            self.assertIn("discovery entry points", writer)
                            self.assertIn("before editing or repeating a question", writer)
                            self.assertIn("do not replace original authority", writer)
                        owner = owner_replan_instruction(candidate)
                        self.assertIn("recover accessible bindings read-only", owner)
                        self.assertIn("Keep production edits blocked", owner)
                        self.assertIn("Never invent a transition", owner)
                        self.assertIn("precise unresolved gap and responsible owner", owner)
                        self.assertLess(owner.index("recover accessible bindings"),
                                        owner.index("If this node can retain blocked"))
                        self.assertIn("snapshot plan_path and work_kind", owner)
                        if candidate.delivery_reliability_policy() == "v1":
                            if candidate.project_memory is not None:
                                self.assertIn("external immutable accepted snapshot", owner)
                                self.assertIn("`check-plan`", owner)
                                self.assertIn("`resolve-plan`", owner)
                                self.assertIn("do not require or recreate it", owner)
                                self.assertIn("never fall back to legacy", owner)
                                self.assertIn(
                                    "Only for a Task confirmed as inventoried legacy",
                                    owner,
                                )
                            else:
                                self.assertIn("do not invoke an absent helper", owner)
                                self.assertIn("block", owner)
                                self.assertIn("do not assume legacy mode or create a fallback", owner)
                        if selected:
                            self.assertIn("accepted_plan_sha256", writer)
                            self.assertIn("accepted_plan_sha256", owner)

    def profile(self):
        return ProjectProfile.from_toml(
            REPO_ROOT, (REPO_ROOT / "contracts/project-profile.example.toml").read_text(),
            check_files=False,
        )

    def test_completion_fields_come_from_actual_graph_across_capabilities(self):
        for context in (False, True):
            for ci in (False, True):
                profile = runtime_v2_profile(ci=ci, source_ci=ci) if context else replace(
                    self.profile(),
                    capabilities={**self.profile().capabilities, "ci_monitoring": ci})
                spec = build_delivery_workflow(profile, 5)
                for incoming in spec.edges:
                    if not incoming.prompt:
                        continue
                    outgoing = [edge for edge in spec.edges
                                if edge.source == incoming.target]
                    for edge in outgoing:
                        fields = ", ".join(f"`{p.key}`" for p in edge.parameters) or "(none)"
                        self.assertIn(
                            f"`{edge.transition}`: {fields}.", incoming.prompt,
                            (context, ci, incoming.key, edge.key))

    def test_completion_contract_unions_fanout_fields_once(self):
        edges = qualify_transition_keys([
            EdgeSpec(key="entry", source="start", transition="begin", target="writer",
                     prompt="Choose `done`."),
            EdgeSpec(key="left", source="writer", transition="done", target="left",
                     parameters=(ParameterSpec("first", "First"),)),
            EdgeSpec(key="right", source="writer", transition="done", target="right",
                     parameters=(ParameterSpec("second", "Second"),
                                 ParameterSpec("first", "First"))),
        ])
        self.assertEqual(
            edges[0].prompt.count("`writer_done`: `first`, `second`."), 1)
        self.assertIn("Choose `writer_done`.", edges[0].prompt)

    def test_cleanup_preserves_helper_and_legitimate_nohelper_paths(self):
        for profile in (self.profile(), runtime_v2_profile(ci=False)):
            without = replace(profile, commands={
                **profile.commands, "prepare_cleanup": ""})
            prompt = cleanup_prompt(without, merged=True)
            self.assertIn(
                f'`{profile.command("evidence")} append --task', prompt)
            self.assertEqual(
                f'`{profile.command("evidence")} seal --task' in prompt,
                profile.runtime_contracts_v2())
            with_helper = replace(profile, commands={
                **profile.commands, "prepare_cleanup": ".kent/scripts/prepare-cleanup"})
            prompt = cleanup_prompt(with_helper, merged=True)
            self.assertIn("`.kent/scripts/prepare-cleanup`", prompt)
            self.assertNotIn(
                f'`{profile.command("evidence")} append --task', prompt)
            self.assertTrue(published_cleanup_prompt(with_helper).startswith(prompt))
            self.assertIn("{{.Params.publication_report}}",
                          published_cleanup_prompt(with_helper))

    def test_prepare_pr_fix_is_task_scoped_without_redundant_approval(self):
        spec = build_delivery_workflow(self.profile(), 5)
        edges = {edge.key: edge for edge in spec.edges}
        self.assertFalse(edges["prepare_pr_fix"].requires_approval)
        self.assertTrue(edges["prepare_pr_no_pr"].requires_approval)
        self.assertTrue(edges["prepare_pr_needs_user_action"].requires_approval)

    def test_lab_routing_does_not_load_product_plan_or_implement(self):
        profile = self.profile()
        for complexity in ("disabled", "complexity"):
            selected = replace(profile, policies={
                **profile.policies, "coder_selection": complexity})
            for spec in (build_canary_workflow(selected, 5), build_smoke_lab_workflow(selected)):
                prompts = "\n".join(edge.prompt or "" for edge in spec.edges)
                for kind in profile.work_kinds.values():
                    self.assertNotIn(kind.plan, prompts)
                    self.assertNotIn(kind.implement, prompts)
                self.assertNotIn("Implement mapping from", prompts)

    def test_reviews_keep_stage_obligations_without_relabeling_old_statuses(self):
        for compliance in (False, True):
            for pr in (False, True):
                profile = self.profile()
                profile = replace(profile, capabilities={
                    **profile.capabilities, "compliance_review": compliance,
                    "pull_requests": pr, "ci_monitoring": pr, "spec_review": True})
                spec = build_delivery_workflow(profile, 5)
                for target in ("spec_review", "verification_gate"):
                    prompt = next(edge.prompt for edge in spec.edges
                                  if edge.target == target and edge.prompt)
                    self.assertIn("current authorized stage", prompt)
                    self.assertIn("owner and due milestone", prompt)
                gate = next(edge.prompt for edge in spec.edges
                            if edge.target == "verification_gate" and edge.prompt)
                self.assertIn("fresh review", gate)
                self.assertIn("project-owned", gate)
                self.assertNotIn("Keep full reports in `review_context`", gate)

REVALIDATION_EDGE_CONTRACTS = {
    "plan_review_revalidate": (
        "plan_review",
        "plan_review_needs_changes",
    ),
    "plan_contract_revalidate": (
        "plan_contract",
        "plan_contract_revalidate",
    ),
    "plan_contract_continue_revalidate": (
        "plan_contract_continue",
        "plan_contract_continue_changed",
    ),
    "plan_contract_verify_revalidate": (
        "plan_contract_verify",
        "plan_contract_verify_changed",
    ),
}

def runtime_v2_profile(*, ci: bool, source_ci: bool = False) -> ProjectProfile:
    contents = (REPO_ROOT / "contracts" / "project-profile.example.toml").read_text()
    contents = contents.replace(
        "schema_version = 3\n",
        'schema_version = 4\nkit_managed_commands = ["dispatch"]\n',
    ).replace('release_topology = "none"\n', "")
    contents += (
        "\n[command_versions]\n"
        'dispatch = "1.0.0"\n'
        "\n[release]\n"
        'topology_kind = "appsome-release-publication"\n'
        'adoption_mode = "managed-in-place"\n'
        'spec_path = ".kent/release/spec.toml"\n'
        'builder_path = ".kent/release/build.sh"\n'
        'snapshot_path = ".kent/release/snapshot.toml"\n'
    )
    contents = contents.replace(
        'kit_managed_commands = ["dispatch"]',
        (
            'kit_managed_commands = ["runtime_contracts", "verify", "evidence", '
            '"janitor", "wait_pr", "github_observation"'
            + (', "wait_ci"' if ci else "")
            + "]"
        ),
    ).replace(
        'dispatch = ".kent/scripts/workflow-verification-dispatch"\n',
        (
            'dispatch = ".kent/scripts/workflow-verification-dispatch"\n'
            'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n'
        ),
    ).replace(
        '[command_versions]\n'
        'dispatch = "1.0.0"\n',
        (
            "[command_versions]\n"
            'runtime_contracts = "2.0.0"\n'
            'verify = "2.0.0"\n'
            'evidence = "2.0.0"\n'
            'janitor = "2.0.0"\n'
            'wait_pr = "3.0.0"\n'
            'github_observation = "1.0.0"\n'
            + ('wait_ci = "3.0.0"\n' if ci else "")
        ),
    ).replace(
        "ci_monitoring = true",
        f"ci_monitoring = {'true' if ci else 'false'}",
    )
    if source_ci:
        contents = (
            contents.replace(
                '"github_observation"',
                '"github_observation", "prepare_ci"',
            )
            .replace(
                'github_observation = "1.0.0"\n',
                'github_observation = "1.0.0"\nprepare_ci = "2.0.0"\n',
            )
            .replace(
                'wait_ci = ".kent/scripts/workflow-wait-github-ci"\n',
                'wait_ci = ".kent/scripts/workflow-wait-github-ci"\n'
                'prepare_ci = ".kent/scripts/workflow-prepare-github-ci"\n',
            )
        )
    return ProjectProfile.from_toml(REPO_ROOT, contents, check_files=False)


def edge_map(profile: ProjectProfile):
    return {
        edge.key: edge
        for edge in build_delivery_workflow(profile, 1).edges
    }


def create_repository(testcase: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Kent Test"),
        ("config", "user.email", "kent@example.invalid"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    (root / ".kent").mkdir()
    (root / ".kent" / "workflow-profile.toml").write_text(
        "[policies]\n"
        'writer_sessions = "continuous"\n'
        'coder_selection = "disabled"\n'
    )
    (root / ".gitignore").write_text("/.kent/runtime/\n")
    (root / "tracked.txt").write_text("ready\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "."],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "Initial"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return root.resolve()


def materialize_templates(root: Path, *names: str) -> Path:
    scripts = root / ".kent" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = REPO_ROOT / "templates" / "project" / name
        target = scripts / name
        shutil.copyfile(source, target)
        target.chmod(0o755)
    shutil.copyfile(
        REPO_ROOT / "workflowkit" / "runtime.py",
        scripts / "workflow_runtime_contracts.py",
    )
    return scripts


def run_json_command(
    command: list[str],
    *,
    root: Path,
    payload: dict[str, object],
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=root,
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **(environment or {})},
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {result.stderr}"
        )
    output = json.loads(result.stdout)
    if not isinstance(output, dict):
        raise AssertionError("command did not emit a JSON object")
    return output


def assert_edge_projection(
    testcase: unittest.TestCase,
    *,
    spec,
    source: str,
    output: dict[str, object],
) -> object:
    edge = next(
        (
            candidate
            for candidate in spec.edges
            if candidate.source == source
            and candidate.transition == output.get("transition")
        ),
        None,
    )
    testcase.assertIsNotNone(edge, (source, output.get("transition")))
    required = {parameter.key for parameter in edge.parameters}
    testcase.assertTrue(required <= output.keys(), (edge.key, required, output))
    return edge


def direct_parameter_references(prompt: str) -> set[str]:
    return set(
        re.findall(
            r"\{\{\s*\.Params\.([A-Za-z_][A-Za-z0-9_]*)",
            prompt,
        )
    )


class DeliveryPackingCommandTests(unittest.TestCase):
    def test_two_cycle_gate_retry_packs_new_context_and_projects_fresh_fanout(self):
        """Exercise the source graph/packer contract, not Kent's Join scheduler."""
        root = create_repository(self)
        profile = runtime_v2_profile(ci=True, source_ci=True)
        spec = build_delivery_workflow(profile, 1)
        workflow_id = "44444444-4444-4444-8444-444444444444"
        inspected = {
            "workflow_id": workflow_id,
            "expected_version": 1,
            "graph": {
                key: []
                for key in ("node_groups", "nodes", "transition_groups", "edges")
            },
        }
        projected = plan_workflow_graph(
            spec, inspected, metadata_changed=False,
        ).document
        self.assertTrue(graph_matches_spec(spec, projected))

        native_graph = projected["graph"]
        nodes = {node["key"]: node for node in native_graph["nodes"]}
        node_keys_by_id = {node["id"]: node["key"] for node in native_graph["nodes"]}
        native_edges = {edge["key"]: edge for edge in native_graph["edges"]}
        groups = {
            group["id"]: group
            for group in native_graph["transition_groups"]
        }

        retry_transition = "verification_gate_retry_verification"
        retry_edge = native_edges["gate_retry_verification"]
        retry_group = groups[retry_edge["transition_group_id"]]
        self.assertEqual(
            (node_keys_by_id[retry_group["source_node_id"]],
             retry_group["transition_id"]),
            ("verification_gate", retry_transition),
        )
        self.assertEqual(
            node_keys_by_id[retry_edge["target_node_id"]],
            "verification_dispatch",
        )
        self.assertFalse(retry_edge["requires_approval"])
        self.assertEqual(retry_edge["context_mode"], "new_session")
        self.assertEqual(retry_edge["context_source"], {"kind": "immediate_source"})
        self.assertEqual(
            [parameter["key"] for parameter in retry_edge["parameters"]],
            ["workspace_path", "review_context", "delivery_context"],
        )

        session_id = "11111111-1111-4111-8111-111111111111"
        identity = {
            "task_id": "native-task",
            "task_short_id": "KIT-1",
            "project_id": "native-project",
            "workspace_path": str(root),
            "session_id": session_id,
            "step_id": "33333333-3333-4333-8333-333333333333",
        }
        detail = {
            "summary": {
                "id": identity["task_id"],
                "short_id": identity["task_short_id"],
                "project_id": identity["project_id"],
                "workflow_id": workflow_id,
            },
            "current_nodes": [{
                "node_id": nodes["verification_gate"]["id"],
                "session_id": session_id,
            }],
            "live_sessions": [],
        }

        cycle_a_context = (
            '{"cycle":"A","joined_report_ref":"cycle-a-joined-report"}'
        )
        cycle_a_delivery = (
            ' { "schema":"workflow-delivery-context-v1", "phase":"pre_pr" }\n'
        )
        cycle_a_sources = [
            capture_producer(
                root,
                identity=identity,
                producer_kind="cycle-a-deterministic",
                inputs={"review_context": cycle_a_context},
                result={
                    "transition": "reported",
                    "verification_status": "passed",
                    "verification_report": "cycle-a-deterministic-pass",
                },
            )[0],
            capture_producer(
                root,
                identity=identity,
                producer_kind="cycle-a-standards",
                inputs={},
                result={
                    "transition": "reported",
                    "standards_status": "blocked",
                    "standards_report": "cycle-a-standards-blocked",
                },
            )[0],
            capture_producer(
                root,
                identity=identity,
                producer_kind="cycle-a-specification",
                inputs={},
                result={
                    "transition": "reported",
                    "spec_status": "passed",
                    "review_report": "cycle-a-specification-pass",
                    "delivery_context": cycle_a_delivery,
                },
            )[0],
        ]
        cycle_b_context = (
            '{"cycle":"B","resolution_ref":"owner-readback-cycle-b"}'
        )
        locator, emitted = pack_delivery(
            root,
            {
                "schema": "workflow-pack-delivery-v1",
                "transition": retry_transition,
                "values": {
                    "workspace_path": str(root),
                    "review_context": cycle_b_context,
                    "delivery_context": cycle_a_delivery,
                },
                "transition_contract_ref": transition_contract_reference(
                    identity, detail, projected, retry_transition,
                ),
                "delivery_mode": "preserve",
                "producer_refs": cycle_a_sources,
            },
            identity=identity,
            task_detail=detail,
            graph=projected,
        )
        completion = json.loads(emitted)
        declared = {
            parameter["key"] for parameter in retry_edge["parameters"]
        }
        self.assertEqual(
            set(completion) - {"transition", "commentary"},
            declared,
        )
        self.assertEqual(completion["transition"], retry_transition)
        self.assertEqual(completion["workspace_path"], str(root))
        self.assertEqual(completion["review_context"], cycle_b_context)
        self.assertNotEqual(completion["review_context"], cycle_a_context)
        self.assertEqual(completion["delivery_context"], cycle_a_delivery)
        for historical_value in (
            "cycle-a-deterministic-pass",
            "cycle-a-standards-blocked",
            "cycle-a-specification-pass",
        ):
            self.assertNotIn(historical_value, emitted.decode("utf-8"))

        packed = reconstruct_producer(root, locator, identity=identity)
        retained = json.loads(packed["binding"]["producer_refs"])["refs"]
        self.assertEqual(retained, cycle_a_sources)
        for source, status_field, expected_status in zip(
            cycle_a_sources,
            ("verification_status", "standards_status", "spec_status"),
            ("passed", "blocked", "passed"),
        ):
            history = reconstruct_producer(root, source, identity=identity)
            self.assertEqual(history["result"][status_field], expected_status)
        self.assertEqual(
            reconstruct_producer(root, cycle_a_sources[0], identity=identity)[
                "inputs"
            ]["review_context"],
            cycle_a_context,
        )

        fanout_group = next(
            group
            for group in native_graph["transition_groups"]
            if group["source_node_id"] == nodes["verification_dispatch"]["id"]
            and group["transition_id"] == "verification_dispatch_fanout_verify"
        )
        fanout_edges = [
            edge
            for edge in native_graph["edges"]
            if edge["transition_group_id"] == fanout_group["id"]
        ]
        self.assertEqual(
            {edge["key"] for edge in fanout_edges},
            {
                "dispatch_deterministic_verify",
                "dispatch_standards_review",
                "dispatch_spec_review",
            },
        )
        cycle_a_only_values = (
            cycle_a_context,
            "cycle-a-deterministic-pass",
            "cycle-a-standards-blocked",
            "cycle-a-specification-pass",
        )
        for edge in fanout_edges:
            self.assertEqual(
                node_keys_by_id[edge["target_node_id"]],
                {
                    "dispatch_deterministic_verify": "deterministic_verify",
                    "dispatch_standards_review": "standards_review",
                    "dispatch_spec_review": "spec_review",
                }[edge["key"]],
            )
            self.assertEqual(edge["context_mode"], "new_session")
            self.assertEqual(
                [parameter["key"] for parameter in edge["parameters"]],
                ["workspace_path", "review_context", "delivery_context"],
            )
            branch_inputs = {
                parameter["key"]: completion[parameter["key"]]
                for parameter in edge["parameters"]
            }
            self.assertEqual(
                branch_inputs,
                {
                    "workspace_path": str(root),
                    "review_context": cycle_b_context,
                    # The retry preserves the exact incoming packet bytes.
                    "delivery_context": cycle_a_delivery,
                },
            )
            for stale_value in cycle_a_only_values:
                self.assertNotIn(stale_value, json.dumps(branch_inputs))

        join_key_by_edge = {
            "deterministic_report_join": (
                "deterministic_verify",
                "verification_status",
                "verification_report",
            ),
            "standards_report_join": (
                "standards_review",
                "standards_status",
                "standards_report",
            ),
            "spec_report_join": (
                "spec_review",
                "spec_status",
                "review_report",
            ),
        }
        for edge_key, (source_key, status_key, report_key) in join_key_by_edge.items():
            edge = native_edges[edge_key]
            group = groups[edge["transition_group_id"]]
            self.assertEqual(node_keys_by_id[group["source_node_id"]], source_key)
            self.assertEqual(group["transition_id"], f"{source_key}_reported")
            self.assertEqual(node_keys_by_id[edge["target_node_id"]], "verification_join")
            self.assertEqual(
                [parameter["key"] for parameter in edge["parameters"]],
                [status_key, report_key],
            )
        gate_edge = native_edges["verification_join_gate"]
        gate_group = groups[gate_edge["transition_group_id"]]
        self.assertEqual(node_keys_by_id[gate_group["source_node_id"]], "verification_join")
        self.assertEqual(gate_group["transition_id"], "verification_join_evaluate")
        self.assertEqual(node_keys_by_id[gate_edge["target_node_id"]], "verification_gate")
        gate_prompt = " ".join(gate_edge["prompt_template"].split())
        self.assertIn("identifies the original technical blocker", gate_prompt)
        self.assertIn(
            "If owner resolution, changed prerequisites, evidence, or settlement "
            "is absent, do not retry",
            gate_prompt,
        )
        self.assertIn(
            "Never relabel an old `blocked` report or reuse an old PASS as current evidence",
            gate_prompt,
        )

    def test_native_packing_derives_review_preserves_failed_packets_and_never_completes(self):
        root = create_repository(self)
        scripts = materialize_templates(root, "workflow-checkpoint", "workflow-evidence-ledger")
        home = root / "home"
        bin_path = home / ".kent" / "bin"
        bin_path.mkdir(parents=True)
        (bin_path / "kent-prepare-github-ci").symlink_to(REPO_ROOT / "scripts" / "prepare-github-ci")
        session = "11111111-1111-4111-8111-111111111111"
        identity = {
            "task_id": "native-task", "task_short_id": "KIT-1", "project_id": "native-project",
            "workspace_path": str(root), "session_id": session,
            "step_id": "33333333-3333-4333-8333-333333333333",
        }
        workflow_id = "44444444-4444-4444-8444-444444444444"
        detail = {
            "summary": {
                "id": "native-task", "short_id": "KIT-1", "project_id": "native-project",
                "workflow_id": workflow_id,
            },
            "source_workspace": {
                "is_primary": True, "workspace_id": "workspace", "root_path": str(root),
            },
            "current_nodes": [{"node_id": "source", "session_id": session}],
        }
        graph = {
            "workflow_id": workflow_id, "expected_version": 1,
            "graph": {
                "nodes": [{"id": "source", "key": "gate", "kind": "agent"}],
                "transition_groups": [{
                    "id": "group", "source_node_id": "source", "transition_id": "failed",
                }],
                "edges": [{
                    "id": "edge", "transition_group_id": "group", "target_node_id": "fix",
                    "parameters": [{"key": "delivery_context"}, {"key": "ci_report"},
                                   {"key": "review_context"}],
                }],
            },
        }
        native = root / "native.json"
        native.write_text(json.dumps({"detail": detail, "graph": graph}))
        command_log = root / "native-calls.jsonl"
        fake_kent = bin_path / "kent"
        fake_kent.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['TEST_NATIVE_CALLS'], 'a') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "data = json.loads(Path(os.environ['TEST_NATIVE_DATA']).read_text())\n"
            "if args[0] == 'project': print('native-project')\n"
            "elif args[:2] == ['task', 'show']: print(json.dumps(data['detail']))\n"
            "elif args[:3] == ['workflow', 'graph', 'inspect']: print(json.dumps(data['graph']))\n"
            "else: sys.exit(18)\n"
        )
        fake_kent.chmod(0o700)
        outer = ' { "schema":"workflow-delivery-context-v1", "phase":"pre_pr" }\n'
        report = ' { "status": "failed", "head": "historical-before-repair" }\n'
        locator, _ = capture_producer(
            root, identity=identity, producer_kind="ci-watch",
            inputs={"review_context": "previous dispatch input"},
            result={"transition": "failed", "delivery_context": outer, "ci_report": report,
                    "review_context": "previous dispatch result"},
        )
        second, _ = capture_producer(
            root, identity=identity, producer_kind="review", inputs={},
            result={"review_context": "different producer context"},
        )
        review = '{"project_carrier":{"stage":"review-ready","external":"UNVERIFIED"}}'
        request = {
            "schema": "workflow-pack-delivery-v1", "transition": "failed",
            "values": {"review_context": review},
            "producer_refs": [locator, second], "delivery_mode": "preserve",
            "transition_contract_ref": transition_contract_reference(identity, detail, graph, "failed"),
        }
        environment = {
            **os.environ, "HOME": str(home), "PATH": str(bin_path) + os.pathsep + os.environ["PATH"],
            "TEST_NATIVE_CALLS": str(command_log), "TEST_NATIVE_DATA": str(native),
            **{"KENT_" + key.upper(): identity[key] for key in ("session_id", "step_id")},
        }
        command = [
            sys.executable, str(scripts / "workflow-checkpoint"), "pack-delivery",
            "--task", "KIT-1", "--workspace", str(root),
        ]
        for attempt in range(2):
            if attempt:
                # A new native Run and graph version do not corrupt an original
                # failed report or silently make it fresh/passing.
                environment["KENT_RUN_ID"] = "55555555-5555-4555-8555-555555555555"
                graph["expected_version"] = 2
                native.write_text(json.dumps({"detail": detail, "graph": graph}))
            result = subprocess.run(
                command, cwd=root, env=environment, input=json.dumps(request),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["delivery_context"], outer)
            self.assertEqual(output["ci_report"], report)
            self.assertEqual(output["review_context"], review)
            self.assertEqual(output["transition"], "failed")
            self.assertNotIn("values", output)
        calls = [json.loads(line) for line in command_log.read_text().splitlines()]
        self.assertTrue(calls)
        self.assertFalse(any("complete" in args for args in calls))
        for rewrite in ({"force": "true"}, {"ci_report": "rewritten"},
                        {"delivery_context": "rewritten"}):
            with self.subTest(rewrite=rewrite):
                denied = subprocess.run(
                    command, cwd=root, env=environment,
                    input=json.dumps({**request, "values": {**request["values"], **rewrite}}),
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(denied.stdout, "")


class ScriptProducerIdentityTests(unittest.TestCase):
    def test_versioned_agent_archives_keep_legacy_bytes_and_reject_mixed_owners(self):
        import hashlib
        from workflowkit import delivery

        native = self.observe()
        common = {key: native[key] for key in
                  ("task_id", "task_short_id", "project_id", "workspace_path")}
        current = {
            **common, "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "22222222-2222-4222-8222-222222222222",
        }
        for version in (1, 2):
            with self.subTest(version=version):
                stored_owner = ({**common, "session_id": "historical-session",
                                 "run_id": "historical-run", "step_id": "historical-step"}
                                if version == 1 else current)
                base = {
                    "schema": f"workflow-producer-source-v{version}", "identity": stored_owner,
                    "producer_kind": "historical-fixture", "inputs": {},
                    "result": {"transition": "failed", "commentary": "original bytes"}, "binding": {},
                }
                raw = delivery.packet_json(base)
                locator = f"build/kent-workflow/KIT-1/packets/base-{hashlib.sha256(raw).hexdigest()}.json"
                emitted = delivery._packet_emitted(base, locator)
                emitted_path = str(Path(locator).with_name(
                    "emitted-" + hashlib.sha256(emitted).hexdigest() + ".json"))
                archive = {locator: raw, emitted_path: emitted}
                result = reconstruct_producer(
                    self.root, locator, identity=current, read_relative=archive.__getitem__)
                self.assertEqual(result["identity"], stored_owner)
                self.assertEqual(result["emitted"], emitted)
                self.assertEqual(producer_reference_closure(
                    self.root, locator, identity=current, read_relative=archive.__getitem__),
                    sorted(archive))
                for schema in ("workflow-producer-source-v3",
                               f"workflow-producer-source-v{3 - version}"):
                    wrong = {**base, "schema": schema}
                    wrong_raw = delivery.packet_json(wrong)
                    wrong_locator = f"build/kent-workflow/KIT-1/packets/base-{hashlib.sha256(wrong_raw).hexdigest()}.json"
                    with self.assertRaisesRegex(ValueError, "schema|version"):
                        reconstruct_producer(
                            self.root, wrong_locator, identity=current,
                            read_relative=lambda _: wrong_raw)
        with self.assertRaises(ValueError):
            capture_producer(self.root, identity={**current, "run_id": "stale"},
                             producer_kind="agent", inputs={}, result={})
        locator, _ = capture_producer(self.root, identity=current,
                                      producer_kind="agent", inputs={}, result={})
        self.assertEqual(json.loads((self.root / locator).read_bytes())["schema"],
                         "workflow-producer-source-v2")

    def setUp(self):
        self.root = create_repository(self)
        self.context = {"task_id": "native-task", "node_id": "script-node"}
        self.detail = {
            "summary": {
                "id": "native-task", "short_id": "KIT-1",
                "project_id": "native-project",
                "workflow_id": "44444444-4444-4444-8444-444444444444",
            },
            "source_workspace": {
                "is_primary": True, "workspace_id": "primary", "root_path": str(self.root),
            },
            "project": {"default_workspace_id": "primary"},
            "current_scripts": [{
                "current_node": {"node_id": "script-node"},
                "path": ".kent/scripts/workflow-plan-contract",
            }],
        }
        self.graph = {
            "workflow_id": self.detail["summary"]["workflow_id"], "expected_version": 3,
            "graph": {"nodes": [{
                "id": "script-node", "kind": "script",
                "script_path": ".kent/scripts/workflow-plan-contract",
            }]},
        }
        self.calls = []

    def read(self, root, args, *, structured=True):
        self.assertEqual(root, self.root)
        self.calls.append(args)
        if args[0] == "project":
            self.assertFalse(structured)
            return "native-project"
        if args[:2] == ["task", "show"]:
            return self.detail
        if args[:3] == ["workflow", "graph", "inspect"]:
            return self.graph
        self.fail(f"unexpected native effect: {args}")

    def observe(self):
        return observe_script_identity(
            self.root, "KIT-1", self.context, native_read=self.read,
        )

    def test_script_without_agent_environment_captures_and_agent_reads_historically(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            identity = self.observe()
            locator, emitted = capture_script_producer(
                self.root, task="KIT-1", native_context=self.context,
                producer_kind="plan-contract", inputs={"plan_path": "original"},
                result={"transition": "failed", "review_context": " exact\nbytes "},
                native_read=self.read,
            )
        self.assertEqual(identity, {
            "task_id": "native-task", "task_short_id": "KIT-1", "project_id": "native-project",
            "workspace_path": str(self.root), "invocation_kind": "script",
            "node_id": "script-node", "transition_branch_key": None,
        })
        agent = {key: identity[key] for key in
                 ("task_id", "task_short_id", "project_id", "workspace_path")}
        agent.update(session_id="11111111-1111-4111-8111-111111111111",
                     step_id="22222222-2222-4222-8222-222222222222")
        self.assertEqual(reconstruct_producer(
            self.root, locator, identity=agent,
        )["emitted"], emitted)
        self.assertEqual(reconstruct_producer(
            self.root, locator, identity=identity,
        )["result"]["review_context"], " exact\nbytes ")
        self.assertFalse(any("list" in args or "sessions" in args for args in self.calls))

    def test_script_branch_proof_and_unrelated_agent_environment(self):
        from unittest.mock import patch
        self.context["transition_branch_key"] = "branch"
        self.detail["current_scripts"][0]["current_node"]["transition_branch_key"] = "branch"
        self.detail["current_scripts"][0]["path"] = str(
            self.root / ".kent/scripts/workflow-plan-contract",
        )
        with patch.dict(os.environ, {"KENT_SESSION_ID": "unrelated-parent-session"}):
            identity = self.observe()
        self.assertEqual(identity["transition_branch_key"], "branch")
        self.assertNotIn("session_id", identity)
        with self.assertRaises(ValueError):
            pack_delivery(self.root, {}, identity=identity, task_detail=self.detail, graph=self.graph)
        with self.assertRaises(ValueError):
            capture_producer(
                self.root, identity={**identity, "session_id": "forged"},
                producer_kind="test", inputs={}, result={"transition": "failed"},
            )

    def test_script_observer_uses_production_bounded_native_reader_without_agent_ids(self):
        import importlib.machinery
        import importlib.util
        from unittest.mock import patch

        fake_bin = self.root / "native-bin"
        fake_bin.mkdir()
        fake_kent = fake_bin / "kent"
        fake_kent.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"detail = {self.detail!r}\n"
            f"graph = {self.graph!r}\n"
            "args = sys.argv[1:]\n"
            "if args[0] == 'project': print('native-project')\n"
            "elif args[:2] == ['task', 'show']: print(json.dumps(detail))\n"
            "elif args[:3] == ['workflow', 'graph', 'inspect']: print(json.dumps(graph))\n"
            "else: sys.exit(19)\n"
        )
        fake_kent.chmod(0o700)
        loader = importlib.machinery.SourceFileLoader(
            "checkpoint_script_test", str(REPO_ROOT / "templates/project/workflow-checkpoint"),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        with patch.dict(os.environ, {"PATH": str(fake_bin)}, clear=True):
            locator, emitted = capture_script_producer(
                self.root, task="KIT-1", native_context=self.context,
                producer_kind="script-fixture", inputs={}, result={"transition": "failed"},
                native_read=module.delivery_native_read,
            )
        self.assertEqual(reconstruct_producer(
            self.root, locator, identity=self.observe(),
        )["emitted"], emitted)

    def test_missing_short_id_is_derived_from_exact_native_script_task(self):
        identity = observe_script_identity(
            self.root, None, self.context, native_read=self.read,
        )
        self.assertEqual(identity["task_short_id"], "KIT-1")
        self.assertIn(["task", "show", "native-task", "--project", "native-project", "--json"], self.calls)

    def test_shared_emitter_legacy_bytes_and_opted_in_capture_without_short_parameter(self):
        import importlib.machinery
        import importlib.util
        from unittest.mock import patch

        loader = importlib.machinery.SourceFileLoader(
            "checkpoint_emitter_test", str(REPO_ROOT / "templates/project/workflow-checkpoint"),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        raw = b' { "transition": "failed", "review_context": " exact\\nbytes " } \n'
        payload = {
            "_kent": self.context, "workspace_path": str(self.root),
            "review_context": "incoming exact\nbytes", "structured_aux": {"not": "a string"},
        }
        with patch.object(module, "delivery_module", side_effect=AssertionError("legacy resolved Kit")):
            self.assertEqual(module.emit_producer_result(self.root, payload, raw, "dispatch"), raw)
        profile = self.root / ".kent/workflow-profile.toml"
        profile.write_text(profile.read_text() + 'delivery_reliability = "v1"\n')
        import workflowkit.delivery as delivery
        with patch.object(module, "delivery_module", return_value=delivery), \
                patch.object(module, "delivery_native_read", side_effect=self.read):
            emitted = module.emit_producer_result(self.root, payload, raw, "dispatch")
        output = json.loads(emitted)
        locator = delivery.producer_locator(output["commentary"])
        stored = reconstruct_producer(self.root, locator, identity=self.observe())
        self.assertEqual(stored["result"]["review_context"], " exact\nbytes ")
        self.assertEqual(stored["inputs"]["review_context"], payload["review_context"])
        self.assertNotIn("_kent", stored["inputs"])
        self.assertNotIn("structured_aux", stored["inputs"])
        self.assertEqual(stored["identity"]["task_short_id"], "KIT-1")
        self.assertNotIn("task_short_id", payload)

    def test_invalid_workspace_input_is_evidence_not_owner_identity_or_packed_result(self):
        identity = self.observe()
        foreign = str(self.root / "foreign-root")
        locator, _ = capture_producer(
            self.root, identity=identity, producer_kind="dispatch",
            inputs={"workspace_path": foreign},
            result={"transition": "invalid_workspace", "reported_workspace_path": foreign},
        )
        stored = reconstruct_producer(self.root, locator, identity=identity)
        self.assertEqual(stored["inputs"]["workspace_path"], foreign)
        self.assertEqual(stored["identity"]["workspace_path"], str(self.root))
        self.assertNotIn("workspace_path", stored["result"])
        with self.assertRaises(ValueError):
            capture_producer(
                self.root, identity=identity, producer_kind="dispatch", inputs={},
                result={"transition": "passed", "workspace_path": foreign},
            )
        agent = {key: identity[key] for key in
                 ("task_id", "task_short_id", "project_id", "workspace_path")}
        agent.update(session_id="11111111-1111-4111-8111-111111111111",
                     step_id="22222222-2222-4222-8222-222222222222")
        detail = {**self.detail, "current_nodes": [{
            "node_id": "agent-node", "session_id": agent["session_id"],
        }]}
        graph = {
            "workflow_id": self.graph["workflow_id"], "expected_version": 3,
            "graph": {
                "nodes": [{"id": "agent-node", "kind": "agent"}],
                "transition_groups": [{
                    "id": "retry", "source_node_id": "agent-node", "transition_id": "retry",
                }],
                "edges": [{
                    "id": "retry-edge", "transition_group_id": "retry",
                    "parameters": [{"key": "workspace_path"}],
                }],
            },
        }
        request = {
            "schema": "workflow-pack-delivery-v1", "transition": "retry", "values": {},
            "producer_refs": [locator], "delivery_mode": "preserve",
            "transition_contract_ref": transition_contract_reference(agent, detail, graph, "retry"),
        }
        with self.assertRaises(ValueError):
            pack_delivery(self.root, request, identity=agent, task_detail=detail, graph=graph)

    def test_foreign_task_node_branch_script_path_and_agent_impersonation_fail(self):
        import copy
        changes = [
            lambda: self.context.update(task_id="foreign"),
            lambda: self.context.update(node_id="foreign"),
            lambda: self.context.update(transition_branch_key="other"),
            lambda: self.detail["summary"].update(project_id="foreign"),
            lambda: self.detail.update(worktree_path=str(self.root / "foreign")),
            lambda: self.detail["current_scripts"][0]["current_node"].update(session_id="agent"),
            lambda: self.detail["current_scripts"][0].update(path=".kent/scripts/other"),
            lambda: self.graph["graph"]["nodes"][0].update(kind="agent"),
            lambda: self.context.update(session_id="forged"),
            lambda: self.detail["current_scripts"].append(
                copy.deepcopy(self.detail["current_scripts"][0])),
        ]
        context, detail, graph = (
            copy.deepcopy(self.context), copy.deepcopy(self.detail), copy.deepcopy(self.graph),
        )
        for change in changes:
            self.context, self.detail, self.graph = (
                copy.deepcopy(context), copy.deepcopy(detail), copy.deepcopy(graph),
            )
            change()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.observe()
        self.assertFalse((self.root / "build").exists())

    def test_retired_source_reconstruction_from_bounded_archive_bytes(self):
        identity = self.observe()
        locator, _ = capture_producer(
            self.root, identity=identity, producer_kind="script",
            inputs={}, result={"transition": "failed", "report": " original\nbytes "},
        )
        expected = reconstruct_producer(self.root, locator, identity=identity)
        paths = producer_reference_closure(self.root, locator, identity=identity)
        archive = {path: (self.root / path).read_bytes() for path in paths}
        for path in paths:
            (self.root / path).unlink()
        reads = []

        def read(relative):
            reads.append(relative)
            return archive[relative]

        self.assertEqual(reconstruct_producer(
            self.root, locator, identity=identity, read_relative=read,
        ), expected)
        self.assertEqual(producer_reference_closure(
            self.root, locator, identity=identity, read_relative=read,
        ), paths)
        self.assertTrue(set(reads) <= set(paths))
        with self.assertRaises(ValueError):
            reconstruct_producer(self.root, locator, identity=identity)
        for path in paths:
            original = archive[path]
            archive[path] = b"{}"
            with self.subTest(path=path), self.assertRaises(ValueError):
                reconstruct_producer(self.root, locator, identity=identity, read_relative=read)
            archive[path] = original
        with self.assertRaises(ValueError):
            reconstruct_producer(self.root, "../" + locator, identity=identity, read_relative=read)
        self.assertTrue(set(reads) <= set(paths))

    def test_nested_producer_closure_reconstructs_after_original_files_retire(self):
        script_identity = self.observe()
        identity = {
            **{key: script_identity[key] for key in
               ("task_id", "task_short_id", "project_id", "workspace_path")},
            "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "22222222-2222-4222-8222-222222222222",
        }
        original, _ = capture_producer(
            self.root, identity=script_identity, producer_kind="original-script",
            inputs={}, result={"transition": "failed", "report": "exact historical failure"},
        )
        detail = {**self.detail, "current_nodes": [{
            "node_id": "agent-node", "session_id": identity["session_id"],
        }]}
        graph = {
            "workflow_id": self.graph["workflow_id"], "expected_version": 3,
            "graph": {
                "nodes": [{"id": "agent-node", "kind": "agent"}],
                "transition_groups": [{
                    "id": "failed", "source_node_id": "agent-node", "transition_id": "failed",
                }],
                "edges": [{"id": "edge", "transition_group_id": "failed",
                           "parameters": [{"key": "report"}]}],
            },
        }
        packed, emitted = pack_delivery(self.root, {
            "schema": "workflow-pack-delivery-v1", "transition": "failed", "values": {},
            "producer_refs": [original], "delivery_mode": "preserve",
            "transition_contract_ref": transition_contract_reference(identity, detail, graph, "failed"),
        }, identity=identity, task_detail=detail, graph=graph)
        self.assertEqual(json.loads(emitted)["report"], "exact historical failure")
        self.assertEqual(json.loads((self.root / original).read_bytes())["schema"],
                         "workflow-producer-source-v1")
        self.assertEqual(json.loads((self.root / packed).read_bytes())["schema"],
                         "workflow-producer-source-v2")
        paths = producer_reference_closure(self.root, packed, identity=identity)
        self.assertEqual(len(paths), 4)
        archive = {path: (self.root / path).read_bytes() for path in paths}
        for path in paths:
            (self.root / path).unlink()
        self.assertEqual(producer_reference_closure(
            self.root, packed, identity=identity, read_relative=archive.__getitem__,
        ), paths)
        for path in paths:
            original = archive[path]
            archive[path] = b"{}"
            with self.subTest(path=path), self.assertRaises(ValueError):
                producer_reference_closure(
                    self.root, packed, identity=identity, read_relative=archive.__getitem__,
                )
            archive[path] = original


class DeliveryContinuityTest(unittest.TestCase):
    def test_revalidation_preserves_retained_owner_and_authority(self) -> None:
        profiles = {
            "runtime-v2-with-ci": runtime_v2_profile(ci=True, source_ci=True),
            "runtime-v2-without-ci": runtime_v2_profile(ci=False),
        }
        for profile_name, profile in profiles.items():
            edges = edge_map(profile)
            for key, (source, transition) in REVALIDATION_EDGE_CONTRACTS.items():
                with self.subTest(profile=profile_name, edge=key):
                    edge = edges.get(key)
                    self.assertIsNotNone(edge, key)
                    self.assertEqual(edge.source, source, key)
                    self.assertEqual(edge.transition, transition, key)
                    self.assertEqual(edge.target, "plan_revalidation", key)
                    self.assertEqual(edge.context, "continue_session", key)
                    self.assertEqual(edge.context_source, "node:plan", key)
                    self.assertIsInstance(edge.prompt, str, key)
                    normalized = " ".join(edge.prompt.split())
                    for label, required_text in (
                        ("optional critique", "Optional bounded read-only critique"),
                        ("review gates", "mandatory project review gates"),
                        ("durable authority", "not only `review_context`"),
                        ("original consent", "An agent summary or truncated historical source cannot fill missing consent"),
                    ):
                        with self.subTest(
                            profile=profile_name,
                            edge=key,
                            requirement=label,
                        ):
                            self.assertIn(
                                " ".join(required_text.split()),
                                normalized,
                                f"{key}: missing {label} clause",
                            )

    def test_shared_lifecycle_edges_carry_delivery_context(self) -> None:
        profiles = {
            "runtime-v2-with-ci": runtime_v2_profile(ci=True, source_ci=True),
            "runtime-v2-without-ci": runtime_v2_profile(ci=False),
            "kit-lite-cursor": ProjectProfile.from_toml(
                REPO_ROOT,
                (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
                check_files=False,
            ),
        }
        shared_edges = {
            "plan_review",
            "plan_review_accept",
            "plan_review_revalidate",
            "plan_revalidation_review",
            "plan_contract_continue_revalidate",
            "plan_contract_verify_revalidate",
            "plan_contract_branch_identity",
            "branch_identity_implement",
            "branch_identity_resolution",
            "branch_identity_retry",
            "plan_contract_implement",
            "plan_contract_continue_implement",
            "plan_contract_verify",
            "plan_contract_checked_continue",
            "plan_contract_checked_verify",
            "implement_continue",
            "implement_verify",
            "fix_verify",
            "dispatch_deterministic_verify",
            "dispatch_invalid_workspace",
            "dispatch_standards_review",
            "dispatch_spec_review",
            "gate_fix",
            "gate_reverify_after_user_action",
            "gate_retry_verification",
            "gate_smoke_required",
            "gate_delivery_ready",
            "smoke_fix",
            "compliance_prepare_pr",
            "compliance_fix",
            "compliance_evidence_repair",
            "compliance_needs_user_action",
            "evidence_repair_compliance",
            "evidence_repair_fix",
            "prepare_pr_fix",
            "ci_monitor_fix",
            "waiting_pr_fix",
            "ci_monitor_watch",
            "fix_needs_user_action",
        }
        for name, profile in profiles.items():
            with self.subTest(profile=name):
                edges = edge_map(profile)
                for key in sorted(shared_edges):
                    if key not in edges:
                        continue
                    self.assertIn(
                        "delivery_context",
                        {parameter.key for parameter in edges[key].parameters},
                        key,
                    )

    def test_plan_initializes_pre_pr_and_gate_uses_latest_scoped_dispatch(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        spec = build_delivery_workflow(profile, 1)
        edges = {edge.key: edge for edge in spec.edges}

        self.assertIn(
            "delivery_context",
            {parameter.key for parameter in edges["plan_review"].parameters},
        )
        self.assertIn("pre_pr", edges["start_plan"].prompt)
        self.assertIn(
            "{{.Params.verification_dispatch_fanout_verify.delivery_context}}",
            edges["verification_join_gate"].prompt or "",
        )
        self.assertNotIn(
            "delivery_context",
            {
                parameter.key
                for parameter in edges["deterministic_report_join"].parameters
            },
        )

    def test_implement_prompt_displays_the_packet_once(self) -> None:
        profiles = (
            runtime_v2_profile(ci=False),
            ProjectProfile.from_toml(
                REPO_ROOT,
                (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
                check_files=False,
            ),
        )
        for profile in profiles:
            with self.subTest(schema=profile.schema_version):
                prompt = edge_map(profile)["plan_contract_implement"].prompt or ""
                self.assertEqual(
                    prompt.count("{{.Params.delivery_context}}"),
                    1,
                )

    def test_gate_reads_latest_scoped_dispatch_without_fanout(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        profile = replace(
            profile,
            capabilities={
                **profile.capabilities,
                "standards_review": False,
                "spec_review": False,
            }
        )
        edges = edge_map(profile)
        gate = edges["deterministic_report_gate"]

        self.assertIn(
            "{{.Params.verification_dispatch_fanout_verify.delivery_context}}",
            gate.prompt or "",
        )
        self.assertEqual(
            tuple(parameter.key for parameter in gate.parameters),
            ("verification_status", "verification_report"),
        )

    def test_prepare_pr_passes_real_post_pr_context_to_ci_producer(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        edges = edge_map(profile)
        keys = tuple(
            parameter.key
            for parameter in edges["prepare_pr_ci_prepare"].parameters
        )

        self.assertEqual(
            keys,
            ("workspace_path", "task_short_id", "delivery_context"),
        )
        self.assertIn(
            "post_pr",
            edges["prepare_pr_ci_prepare"].parameters[-1].description,
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in edges["ci_monitor_watch"].parameters
            ),
            ("workspace_path", "task_short_id", "delivery_context"),
        )

    def test_prepare_pr_handoff_matches_flat_profile_edges_and_authority(self) -> None:
        kit_lite = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
            check_files=False,
        )
        kit_lite_ci = replace(
            kit_lite,
            capabilities={**kit_lite.capabilities, "ci_monitoring": True},
            commands={
                **kit_lite.commands,
                "wait_ci": ".kent/scripts/workflow-wait-github-ci",
            },
        )
        legacy_standard = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / "contracts" / "project-profile.example.toml").read_text(),
            check_files=False,
        )
        legacy_standard_no_ci = replace(
            legacy_standard,
            capabilities={**legacy_standard.capabilities, "ci_monitoring": False},
        )
        profiles = {
            "kit-lite-no-ci": (
                kit_lite,
                (
                    "workspace_path",
                    "pr_url",
                    "branch_name",
                    "merge_strategy",
                    "pr_feedback_cursor",
                ),
            ),
            "kit-lite-flat-ci": (
                kit_lite_ci,
                (
                    "workspace_path",
                    "pr_url",
                    "branch_name",
                    "merge_strategy",
                    "pr_feedback_cursor",
                    "ci_contract",
                ),
            ),
            "runtime-v2-no-ci": (
                runtime_v2_profile(ci=False),
                (
                    "workspace_path",
                    "pr_url",
                    "branch_name",
                    "merge_strategy",
                    "pr_feedback_cursor",
                ),
            ),
            "source-ci": (
                runtime_v2_profile(ci=True, source_ci=True),
                ("workspace_path", "task_short_id", "delivery_context"),
            ),
            "legacy-standard-ci": (
                legacy_standard,
                (
                    "workspace_path",
                    "pr_url",
                    "branch_name",
                    "merge_strategy",
                    "ci_contract",
                ),
            ),
            "legacy-standard-no-ci": (
                legacy_standard_no_ci,
                (
                    "workspace_path",
                    "pr_url",
                    "branch_name",
                    "merge_strategy",
                ),
            ),
        }
        non_entry_edges = {
            "prepare_pr_no_pr",
            "prepare_pr_fix",
            "prepare_pr_needs_user_action",
        }
        for name, (profile, expected_fields) in profiles.items():
            with self.subTest(profile=name):
                spec = build_delivery_workflow(profile, 5)
                entry = next(
                    edge
                    for edge in spec.edges
                    if edge.source == "prepare_pr"
                    and edge.key not in non_entry_edges
                )
                fields = tuple(parameter.key for parameter in entry.parameters)
                self.assertEqual(fields, expected_fields)

                prompt = next(
                    edge.prompt
                    for edge in spec.edges
                    if edge.target == "prepare_pr"
                    and edge.prompt
                    and edge.prompt.startswith("Prepare delivery for")
                )
                normalized_prompt = " ".join(prompt.split())
                self.assertNotIn(
                    "This workflow explicitly authorizes committing the task changes",
                    normalized_prompt,
                )
                self.assertIn(
                    "Git actions require exact current human approval",
                    normalized_prompt,
                )
                self.assertIn("Plan acceptance", normalized_prompt)
                self.assertIn("`delivery_context`", normalized_prompt)
                self.assertIn("PR/CI/merge data are evidence only", normalized_prompt)
                self.assertIn("No consumer-source writes or merge", normalized_prompt)
                self.assertIn("package publication needs separate approval", normalized_prompt)

                contract = (
                    f"`{entry.transition}`: "
                    + ", ".join(f"`{field}`" for field in fields) + "."
                )
                self.assertIn(contract, normalized_prompt)
                if not profile.capability("ci_monitoring"):
                    narrative = normalized_prompt.split("Completion fields", 1)[0]
                    self.assertNotIn("prepare_pr_monitor_ci", narrative)
                    self.assertNotIn("ci_contract", fields)
                if name == "source-ci":
                    self.assertIn("PR identity and CI packets belong only in", normalized_prompt)
                else:
                    self.assertNotIn("For source-contract CI handoff", normalized_prompt)

    def test_agent_prompt_parameters_exist_on_the_incoming_edge(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        edges = edge_map(profile)
        edge_keys = (
            "ci_prepare_failed",
            "ci_watch_diagnose",
            "ci_watch_waiting_pr",
            "ci_watch_state_changed",
            "waiting_pr_needs_user_action",
            "prepare_pr_fix",
            "waiting_pr_fix",
        )
        flat_observation_edges = {
            "ci_prepare_failed",
            "ci_watch_diagnose",
            "ci_watch_waiting_pr",
            "ci_watch_state_changed",
            "waiting_pr_needs_user_action",
        }
        scoped_prior_key = "verification_dispatch_fanout_verify"
        dispatch = edges["dispatch_deterministic_verify"]
        self.assertIn(
            "delivery_context",
            {parameter.key for parameter in dispatch.parameters},
        )
        for key in edge_keys:
            edge = edges[key]
            references = direct_parameter_references(edge.prompt or "")
            references.discard("TaskShortId")
            self.assertNotIn("verification_dispatch_fanout_verify", references, key)
            self.assertLessEqual(
                references,
                {parameter.key for parameter in edge.parameters},
                key,
            )
            if key in flat_observation_edges:
                self.assertNotIn(
                    "{{.Params.delivery_context}}",
                    edge.prompt or "",
                    key,
                )
        gate = edges["verification_join_gate"]
        self.assertIn(
            f"{{{{.Params.{scoped_prior_key}.delivery_context}}}}",
            gate.prompt or "",
        )

    def test_observer_recovery_loops_keep_flat_delivery_contracts(self) -> None:
        source_profile = runtime_v2_profile(ci=True, source_ci=True)
        source_edges = edge_map(source_profile)
        ci_prompt = source_edges["ci_watch_diagnose"].prompt or ""
        ci_prompt_compact = " ".join(ci_prompt.split())
        waiting_prompt = (
            source_edges["waiting_pr_needs_user_action"].prompt or ""
        )
        waiting_prompt_compact = " ".join(waiting_prompt.split())

        self.assertIn("For `ci_monitor_needs_changes`, pack", ci_prompt)
        self.assertIn(
            "Keep `ci_monitor_needs_user_action` on its existing flat "
            "self-recovery contract.",
            ci_prompt_compact,
        )
        self.assertIn(
            "For source-contract `waiting_pr_ci_required`",
            waiting_prompt,
        )
        self.assertIn(
            "Keep `waiting_pr_needs_user_action` on its existing flat "
            "self-recovery contract.",
            waiting_prompt_compact,
        )
        for key in (
            "ci_monitor_needs_user_action",
            "waiting_pr_needs_user_action",
        ):
            self.assertNotIn(
                "delivery_context",
                {parameter.key for parameter in source_edges[key].parameters},
                key,
            )

        non_source_profile = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
            check_files=False,
        )
        non_source_profile = replace(
            non_source_profile,
            capabilities={
                **non_source_profile.capabilities,
                "ci_monitoring": True,
            },
            commands={
                **non_source_profile.commands,
                "wait_ci": ".kent/scripts/workflow-wait-github-ci",
            },
        )
        non_source_prompt = (
            edge_map(non_source_profile)["waiting_pr_needs_user_action"].prompt
            or ""
        )
        self.assertIn(
            "non-source-contract `waiting_pr_ci_required`",
            non_source_prompt,
        )
        self.assertNotIn(
            "For source-contract `waiting_pr_ci_required`",
            non_source_prompt,
        )

    def test_legacy_standard_profile_has_no_delivery_context_contract(self) -> None:
        profile = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / "contracts" / "project-profile.example.toml").read_text(),
            check_files=False,
        )
        self.assertEqual(profile.schema_version, 3)
        self.assertEqual(profile.delivery_profile, "standard")
        spec = build_delivery_workflow(profile, 1)

        self.assertFalse(
            any(
                parameter.key == "delivery_context"
                for edge in spec.edges
                for parameter in edge.parameters
            )
        )
        self.assertFalse(
            any(
                "{{.Params.delivery_context}}" in (edge.prompt or "")
                for edge in spec.edges
            )
        )

    def test_generated_smoke_delegates_lease_protocol_to_project_adapter(self) -> None:
        profile = runtime_v2_profile(ci=False)
        prompt = edge_map(profile)["gate_smoke_required"].prompt or ""
        for required in (
            "Smoke procedure and resource adapter",
            "native identity binding",
            "resource/owner/token state privately in `stage_data`",
            "never infer ownership",
            "release readback",
            "Never expose tokens",
        ):
            with self.subTest(required=required):
                self.assertIn(required, prompt)
        self.assertNotIn("obtain the current Session with `kent session-id`", prompt)
        self.assertNotIn("kent task show {{.TaskShortId}} --json", prompt)

        self.assertNotIn("`acquire-any` emits", prompt)
        self.assertNotIn("age <= TTL", prompt)

    def test_real_wrapper_stdout_projects_to_selected_edge_specs(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        spec = build_delivery_workflow(profile, 1)
        root = create_repository(self)
        scripts = materialize_templates(
            root,
            "workflow-plan-contract",
            "workflow-plan-contract-accept",
            "workflow-plan-contract-continue",
            "workflow-plan-contract-verify",
            "workflow-plan-contract-fix-continue",
            "workflow-verification-dispatch",
            "workflow-verify-report",
        )
        pre_pr = ' { "schema" : "workflow-delivery-context-v1", "phase" : "pre_pr" } '
        post_pr = json.dumps(
            {
                "schema": "workflow-delivery-context-v1",
                "phase": "post_pr",
                "pr_url": "https://github.com/acme/widget/pull/42",
                "branch_name": "task/continuity",
                "merge_strategy": "squash",
                "pr_feedback_cursor": "uninitialized",
                "ci_contract": '{ "schema" : "github-ci-contract-v1" }',
                "ci_report": '{ "schema" : "github-ci-report-v1" }',
            },
            ensure_ascii=False,
        )
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement continuity\n")

        accepted = run_json_command(
            [str(scripts / "workflow-plan-contract-accept")],
            root=root,
            payload={
                "workspace_path": str(root),
                "plan_path": ".todo/task/plan.md",
                "work_kind": "feature",
                "plan_route": "start",
                "plan_route_context": "not-applicable",
                "review_context": "accepted plan",
                "task_short_id": "TASK-CONTINUITY",
                "delivery_context": pre_pr,
            },
        )
        accept_edge = assert_edge_projection(
            self,
            spec=spec,
            source="plan_contract",
            output=accepted,
        )
        self.assertEqual(accept_edge.key, "plan_contract_implement")
        self.assertEqual(accepted["delivery_context"], pre_pr)
        snapshot = json.loads(
            (
                root
                / ".kent"
                / "runtime"
                / "TASK-CONTINUITY"
                / "plan-contract.json"
            ).read_text()
        )
        self.assertNotIn("delivery_context", snapshot)

        verified = run_json_command(
            [str(scripts / "workflow-plan-contract-verify")],
            root=root,
            payload={
                "workspace_path": str(root),
                "review_context": "all writer steps complete",
                "task_short_id": "TASK-CONTINUITY",
                "delivery_context": post_pr,
            },
        )
        verify_edge = assert_edge_projection(
            self,
            spec=spec,
            source="plan_contract_verify",
            output=verified,
        )
        self.assertEqual(verify_edge.key, "plan_contract_checked_verify")
        self.assertEqual(verified["delivery_context"], post_pr)

        dispatched = run_json_command(
            [str(scripts / "workflow-verification-dispatch")],
            root=root,
            payload={
                "workspace_path": str(root),
                "review_context": "verification evidence",
                "delivery_context": post_pr,
            },
        )
        dispatch_edge = assert_edge_projection(
            self,
            spec=spec,
            source="verification_dispatch",
            output=dispatched,
        )
        self.assertEqual(dispatch_edge.key, "dispatch_deterministic_verify")
        self.assertEqual(dispatched["delivery_context"], post_pr)

        verifier = scripts / "workflow-compile-verify"
        verifier.write_text(
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"failed\"}'\n"
        )
        verifier.chmod(0o755)
        with (root / ".gitignore").open("a") as ignored:
            ignored.write("/build/kent-workflow/\n")
        failed_report = run_json_command(
            [sys.executable, str(scripts / "workflow-verify-report")],
            root=root,
            payload={"workspace_path": str(root)},
        )
        report_edge = assert_edge_projection(
            self,
            spec=spec,
            source="deterministic_verify",
            output=failed_report,
        )
        self.assertEqual(report_edge.key, "deterministic_report_join")
        self.assertEqual(
            {parameter.key for parameter in report_edge.parameters},
            {"verification_status", "verification_report"},
        )
        self.assertEqual(failed_report["verification_status"], "needs_changes")
        self.assertNotIn("delivery_context", failed_report)


if __name__ == "__main__":
    unittest.main()
