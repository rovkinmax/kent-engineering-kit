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

from workflowkit.delivery import build_delivery_workflow
from workflowkit.delivery import (
    capture_producer, capture_script_producer, observe_script_identity,
    pack_delivery, producer_reference_closure, reconstruct_producer, transition_contract_reference,
)
from workflowkit.profile import ProjectProfile


REPO_ROOT = Path(__file__).resolve().parents[1]

REVALIDATION_EDGE_CONTRACTS = {
    "plan_review_revalidate": (
        "plan_review",
        "plan_review_needs_changes",
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
REVALIDATION_PROMPT_REQUIREMENTS = (
    (
        "grill timing",
        "Consult a bounded read-only grill leaf before freeze and formal review",
    ),
    (
        "material-change triggers",
        "only when materially revising requirements, API/UX, architecture, "
        "authority, safety/effects, cost, or evidence strategy",
    ),
    (
        "execution-disproof trigger",
        "or when execution disproves the chosen approach",
    ),
    (
        "grill non-triggers",
        "Hash drift, checkbox progress, typos, operational waiting, and "
        "repeated identical failures alone do not require another grill call.",
    ),
    (
        "unavailable eligibility blocker",
        "Report unavailable effective leaf eligibility as a concrete blocker "
        "rather than claiming critique occurred.",
    ),
    (
        "linked proposal and human acceptance",
        "Keep the exact proposal plus human acceptance when that pair grants authority.",
    ),
    (
        "discoverable durable authority",
        "Store durable references in the authoritative plan or discoverable "
        "project-permitted report-only evidence, not only `review_context`;",
    ),
    (
        "no agent-summary consent substitution",
        "An agent summary or truncated historical source cannot fill missing consent.",
    ),
)


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
    def test_native_interface_exact_bytes_historical_failure_and_no_completion(self):
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
            "run_id": "22222222-2222-4222-8222-222222222222",
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
                    "parameters": [{"key": "delivery_context"}, {"key": "ci_report"}],
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
            root, identity=identity, producer_kind="ci-watch", inputs={},
            result={"transition": "failed", "delivery_context": outer, "ci_report": report},
        )
        request = {
            "schema": "workflow-pack-delivery-v1", "transition": "failed",
            "values": {}, "producer_refs": [locator], "delivery_mode": "preserve",
            "transition_contract_ref": transition_contract_reference(identity, detail, graph, "failed"),
        }
        environment = {
            **os.environ, "HOME": str(home), "PATH": str(bin_path) + os.pathsep + os.environ["PATH"],
            "TEST_NATIVE_CALLS": str(command_log), "TEST_NATIVE_DATA": str(native),
            **{"KENT_" + key.upper(): identity[key] for key in ("session_id", "run_id", "step_id")},
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
            self.assertEqual(output["transition"], "failed")
            self.assertNotIn("values", output)
        calls = [json.loads(line) for line in command_log.read_text().splitlines()]
        self.assertTrue(calls)
        self.assertFalse(any("complete" in args for args in calls))
        denied = subprocess.run(
            command, cwd=root, env=environment,
            input=json.dumps({**request, "values": {"force": "true"}}),
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(denied.stdout, "")


class ScriptProducerIdentityTests(unittest.TestCase):
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
        agent.update(session_id="later-session", run_id="later-run", step_id="later-step")
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
        agent.update(session_id="current-agent", run_id="run", step_id="step")
        detail = {**self.detail, "current_nodes": [{
            "node_id": "agent-node", "session_id": "current-agent",
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
        identity = self.observe()
        original, _ = capture_producer(
            self.root, identity=identity, producer_kind="original-script",
            inputs={}, result={"transition": "failed", "report": "exact historical failure"},
        )
        packed, _ = capture_producer(
            self.root, identity=identity, producer_kind="pack-delivery",
            inputs={}, result={"transition": "failed", "report": "exact historical failure"},
            binding={"producer_refs": json.dumps({"refs": [original]})},
        )
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
    def test_revalidation_grill_contract_in_runtime_v2_profiles(self) -> None:
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
                    for label, required_text in REVALIDATION_PROMPT_REQUIREMENTS:
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

                marker = (
                    "Complete `prepare_pr_monitor_ci`"
                    if name == "source-ci"
                    else "Flat prepare_pr_monitor_ci:"
                )
                start = normalized_prompt.index(marker)
                end = normalized_prompt.index("If no PR is genuinely", start)
                handoff = normalized_prompt[start:end]
                parameter_tokens = [
                    "task_short_id={{.TaskShortId}}"
                    if field == "task_short_id"
                    else f"`{field}`" if name == "source-ci"
                    else field
                    for field in fields
                ]
                positions = [handoff.index(token) for token in parameter_tokens]
                self.assertEqual(positions, sorted(positions))
                if name in {"kit-lite-no-ci", "runtime-v2-no-ci"}:
                    self.assertNotIn("delivery_context", handoff)
                elif name == "source-ci":
                    self.assertIn(
                        "PR identity and CI packets belong only in that packet",
                        handoff,
                    )
                elif name in {"kit-lite-flat-ci", "legacy-standard-ci", "legacy-standard-no-ci"}:
                    self.assertIn("Flat prepare_pr_monitor_ci:", handoff)

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

    def test_generated_smoke_prompt_preserves_mobile_lease_contract(self) -> None:
        profile = runtime_v2_profile(ci=False)
        prompt = edge_map(profile)["gate_smoke_required"].prompt or ""
        for required in (
            "kent task show {{.TaskShortId}} --json",
            "`summary.id`",
            "`summary.short_id`",
            "`kent session-id`",
            "`KENT_TASK_ID`",
            "`KENT_SESSION_ID`",
            "`task_native_id`",
            "`task_short_id`",
            "`lease_owner_id`",
            "`lock_resource`",
            "`lock_token`",
            "`acquire` emits one bare token",
            "`acquire-any` emits exactly `resource=...` then `token=...`",
            "Reject missing, empty, duplicate, malformed, extra, or ineligible",
            "`resume` on that exact pair",
            "`resume-owned`",
            "only project-eligible resources",
            "multiple matches",
            "sole occupancy",
            "`age <= TTL` stays busy",
            "`age > TTL`",
            "resume-first refreshes",
            "replacement-first invalidates",
            "require `unlocked`",
        ):
            with self.subTest(required=required):
                self.assertIn(required, prompt)

        contract = (REPO_ROOT / "contracts" / "mobile-smoke-contract.md").read_text()
        for required in (
            "`summary.id`",
            "`summary.short_id`",
            "`lease_owner_id`",
            "`lock_resource`",
            "`lock_token`",
            "multiple same-Task matches are ambiguous and block",
            "`age <= TTL`",
            "`age > TTL`",
            "require `unlocked`",
        ):
            with self.subTest(contract=required):
                self.assertIn(required, contract)

        adapter_readme = (REPO_ROOT / "adapters" / "README.md").read_text()
        self.assertIn("### Mobile resource-lock interface", adapter_readme)
        self.assertIn("`acquire-any` emits exactly", adapter_readme)
        self.assertIn("`contracts/mobile-smoke-contract.md`", adapter_readme)

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
