"""Offline contracts for the fixed ready package and create-only install boundary."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workflowkit import lite_install as lite
from workflowkit.graph import graph_matches_spec
from workflowkit.model import ParameterSpec, SpecError
from workflowkit.kent import KentCommandError


class Client:
    def __init__(self, root):
        self.workspace = self.project_workspace = root
        self.binary = "/fixture/kent"
        self.writes = []
        self.graph = {"nodes": [
            {"id": "22222222-2222-4222-8222-222222222222", "key": "backlog", "kind": "start",
             "display_name": "Backlog", "group_id": None},
            {"id": "33333333-3333-4333-8333-333333333333", "key": "done", "kind": "terminal",
             "display_name": "Done", "group_id": None},
        ], "edges": [], "node_groups": [], "transition_groups": []}
        self.links = []
        self.name = ""
        self.workflow_id = "11111111-1111-4111-8111-111111111111"
        self.fail_graph = False

    def require_version(self, *args):
        pass

    def run(self, args):
        if args[0] != "project":
            raise AssertionError(args)
        return SimpleNamespace(stdout="project-fixture\n")

    def run_json(self, args):
        if args[:2] != ["workflow", "create"]:
            raise AssertionError(args)
        self.writes.append(("create", args))
        self.name = args[-1]
        return {"id": self.workflow_id, "name": self.name, "version": 1}

    def require_inspect(self, selector):
        return {"workflow": {"id": self.workflow_id, "name": self.name}}

    def inspect_graph(self, selector):
        return {"workflow_id": self.workflow_id, "expected_version": 1, "graph": self.graph}

    def apply_graph_document(self, document, *, confirm_destructive):
        if self.fail_graph:
            raise RuntimeError("unknown graph RPC outcome")
        assert confirm_destructive is False
        self.writes.append(("graph", deepcopy(document)))
        self.graph = document["graph"]

    def ensure_workflow_metadata(self, selector, spec, definition):
        self.writes.append(("metadata", selector))

    def assert_exact_graph_document(self, spec, document):
        if not graph_matches_spec(spec, document):
            raise SpecError("graph mismatch")

    def validate(self, selector):
        self.writes.append(("validate", selector))

    def link(self, selector, *, set_default):
        assert set_default is False
        self.writes.append(("link", selector))
        self.links = [{"id": selector, "project_link": {"default": False}}]

    def workflow_records(self, *, project_id=None):
        if project_id is None and self.name:
            return ({"id": self.workflow_id, "name": self.name},)
        return tuple(self.links)

    def workflow_has_tasks(self, definition):
        return False

    def workflow_is_linked(self, definition):
        return False


class LiteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/fixture/project")
        self.client = Client(self.root)
        self.state = {"project_id": "project-fixture", "workflow_inventory": [],
                      "linked_workflows": [], "project_files": {}, "kit_files": {}}
        self.observe = patch.object(lite, "observe", return_value=self.state)
        self.observe.start()
        self.addCleanup(self.observe.stop)

    def plan(self):
        return lite.preview(self.root, self.client)

    def apply(self, plan):
        raw = lite.encode(plan)
        with patch.object(Path, "resolve", return_value=self.root):
            return lite.apply(plan, raw, lite.digest(raw), self.client)

    def test_fixed_eleven_nodes_no_scripts_or_join(self):
        spec = lite.load_spec()
        self.assertEqual(11, len(spec.nodes))
        self.assertEqual(8, sum(n.kind == "agent" for n in spec.nodes))
        self.assertFalse(any(n.kind in {"script", "join"} for n in spec.nodes))
        self.assertEqual({"default", "spec-reviewer", "researcher"},
                         {n.agent for n in spec.nodes if n.agent})
        self.assertEqual("default", next(n.agent for n in spec.nodes if n.key == "cleanup"))

    def test_complete_graph_matches_spec(self):
        plan = self.plan()
        self.assertTrue(graph_matches_spec(lite.project_spec(self.root, self.client), {
            "workflow_id": "00000000-0000-4000-8000-000000000000",
            "expected_version": 0, "graph": plan["graph"]}))

    def test_preview_has_no_mutations(self):
        self.plan()
        self.assertEqual([], self.client.writes)

    def test_existing_name_rejected_before_effect(self):
        self.state["workflow_inventory"] = [{"name": lite.project_spec(self.root, self.client).name}]
        with self.assertRaisesRegex(SpecError, "already exists"):
            self.plan()
        self.assertEqual([], self.client.writes)

    def test_create_exact_graph_nondefault_only(self):
        plan = self.plan()
        created = {"id": self.client.workflow_id, "name": plan["name"], "version": 1}
        expected = lite.bind_fresh_seed(plan["graph"], self.client.inspect_graph(created["id"]), created)
        self.apply(plan)
        operations = [name for name, _ in self.client.writes]
        self.assertEqual(["create", "graph", "metadata", "validate", "link", "validate"], operations)
        self.assertEqual(expected, self.client.graph)
        endpoints = {node["key"]: node["id"] for node in self.client.graph["nodes"]
                     if node["key"] in {"backlog", "done"}}
        self.assertEqual({"backlog": "22222222-2222-4222-8222-222222222222",
                          "done": "33333333-3333-4333-8333-333333333333"}, endpoints)
        original = {row["key"]: row["id"] for row in plan["graph"]["nodes"]}
        for node in self.client.graph["nodes"]:
            if node["key"] not in endpoints:
                self.assertEqual(original[node["key"]], node["id"])
        for collection in ("edges", "transition_groups"):
            self.assertEqual([row["id"] for row in plan["graph"][collection]],
                             [row["id"] for row in self.client.graph[collection]])

    def test_wrong_digest_rejected(self):
        plan = self.plan()
        with self.assertRaisesRegex(SpecError, "SHA-256"):
            lite.apply(plan, lite.encode(plan), "wrong", self.client)
        self.assertEqual([], self.client.writes)

    def test_noncanonical_bytes_rejected(self):
        plan = self.plan()
        raw = json.dumps(plan).encode()
        with self.assertRaisesRegex(SpecError, "canonical"):
            lite.apply(plan, raw, lite.digest(raw), self.client)

    def test_source_native_drift_rejected(self):
        plan = deepcopy(self.plan())
        self.state["project_files"] = {"changed": "sha"}
        with self.assertRaisesRegex(SpecError, "drift"):
            self.apply(plan)
        self.assertEqual([], self.client.writes)

    def test_metadata_drift_rejected_before_effect(self):
        plan = self.plan()
        plan["name"] = "foreign"
        with self.assertRaisesRegex(SpecError, "metadata"):
            self.apply(plan)
        self.assertEqual([], self.client.writes)

    def test_graph_drift_rejected_before_effect(self):
        plan = self.plan()
        plan["graph"]["nodes"][0]["display_name"] = "wrong"
        with self.assertRaisesRegex(SpecError, "graph differs"):
            self.apply(plan)
        self.assertEqual([], self.client.writes)

    def test_partial_apply_reports_real_identity_without_retry(self):
        self.client.fail_graph = True
        with self.assertRaisesRegex(KentCommandError, self.client.workflow_id):
            self.apply(self.plan())
        self.assertEqual(["create"], [name for name, _ in self.client.writes])

    def test_backedges_reuse_writer_and_replan_owner(self):
        for edge in lite.load_spec().edges:
            if edge.target == "implement" and edge.source != "plan_review":
                self.assertEqual(("continue_session", "node:implement"),
                                 (edge.context, edge.context_source))
            if edge.target == "plan" and edge.source != "backlog":
                self.assertEqual(("continue_session", "node:plan"),
                                 (edge.context, edge.context_source))

    def test_inputs_declared_and_identity_carried_on_every_edge(self):
        for edge in lite.load_spec().edges:
            parameters = {p.key for p in edge.parameters}
            if edge.source == "backlog":
                self.assertEqual(set(), parameters)
                self.assertNotIn("Incoming values:", edge.prompt)
                self.assertNotIn("{{.Params.", edge.prompt)
                self.assertIn("{{.TaskBody}}", edge.prompt)
                continue
            self.assertTrue({"plan_ref", "decision_refs", "plan_review_result",
                             "candidate_ref", "verification_result",
                             "candidate_review_result", "delivery_ref",
                             "disposition", "cleanup_result", "feedback"} <= parameters)
            if edge.prompt:
                for key in parameters:
                    self.assertIn("{{.Params." + key + "}}", edge.prompt)

    def test_failure_and_review_backedges(self):
        edges = {e.transition: e.target for e in lite.load_spec().edges}
        self.assertEqual("plan", edges["plan_revise"])
        for key in ("verify_fix", "review_fix", "publish_fix", "observe_fix"):
            self.assertEqual("implement", edges[key])
        self.assertEqual("cleanup", edges["observe_delivered"])
        self.assertEqual("done", edges["cleanup_delivered"])
        self.assertEqual("not_delivered", edges["cleanup_not_delivered"])
        for edge in lite.load_spec().edges:
            if edge.target in {"done", "not_delivered"}:
                self.assertEqual("cleanup", edge.source)

    def test_prompt_guards_are_explicit_not_runtime_proof(self):
        prompts = "\n".join(e.prompt or "" for e in lite.load_spec().edges)
        for clause in ("exact immutable", "genuine", "read-only", "Unknown",
                       "actual authorized merge", "never replay blindly",
                       "Agents never call accept-plan"):
            self.assertIn(clause, prompts)
        self.assertNotIn("/gradlew", prompts)

    def test_no_storage_installer_or_legacy_dispatch(self):
        source = Path(lite.__file__).read_text()
        for forbidden in ("project_install", "initialize-task", "set_default=True",
                          '["task", "start"', "compatibility_plan="):
            self.assertNotIn(forbidden, source)

    def test_cli_help_runs_without_native_or_storage(self):
        with self.assertRaises(SystemExit) as caught:
            lite.main(["--help"])
        self.assertEqual(0, caught.exception.code)

    def test_actual_installer_dispatch_in_isolated_fixture(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("LITE_TEST_ROOT")) as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "workflowkit").mkdir()
            # Read-only module links preserve real package initialization without
            # copying the Kit or installing any runtime/helper.
            import workflowkit.graph
            dependency_root = Path(workflowkit.graph.__file__).parent
            for source in dependency_root.glob("*.py"):
                if source.name != "lite_install.py":
                    (root / "workflowkit" / source.name).symlink_to(source)
            shutil.copyfile(lite.__file__, root / "workflowkit/lite_install.py")
            shutil.copyfile(lite.KIT / "scripts/install", root / "scripts/install")
            result = subprocess.run(["bash", str(root / "scripts/install"), "workflow", "--help"],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("preview,apply", result.stdout)
            result = subprocess.run(["bash", str(root / "scripts/install"), "workflow", "preview",
                                     "--project", str(root), "--template", "unknown"],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(2, result.returncode)
            self.assertIn("invalid choice", result.stderr)

    def test_ambiguous_creation_never_touches_graph(self):
        plan = self.plan()
        self.client.workflow_records = lambda **kwargs: (
            {"id": self.client.workflow_id, "name": "foreign"},)
        with self.assertRaisesRegex(SpecError, "ambiguous"):
            self.apply(plan)
        self.assertEqual(["create"], [name for name, _ in self.client.writes])

    def test_observe_is_read_only_and_binds_source_and_binary(self):
        # Stub bytes, not native effects. Real hashing and request shape stay under test.
        self.observe.stop()
        self.client.run_json = lambda args: {
            "project_id": "project-fixture", "offset": 0, "workspaces": [{
                "workspace_id": "workspace-fixture", "root_path": str(self.root), "is_default": True
            }], "next_offset": None}
        with patch.object(lite, "file_hashes", return_value={"file": "sha"}), \
                patch.object(lite, "qualified_identity",
                             return_value=("project-fixture", "workspace-fixture")), \
                patch.object(Path, "read_bytes", return_value=b"kent executable"):
            state = lite.observe(self.root, self.client)
        self.assertEqual("project-fixture", state["project_id"])
        self.assertEqual(lite.digest(b"kent executable"), state["kent_sha256"])
        self.assertEqual({"file": "sha"}, state["kit_files"])
        self.assertEqual({"file": "sha"}, state["execution_dependencies"])
        self.assertEqual([], self.client.writes)

    def test_qualified_identity_reuses_positive_native_helper(self):
        with patch.object(lite.op, "_source_canonical_root", return_value=self.root), \
                patch.object(lite.op, "_git", side_effect=[
                    str(self.root), f"worktree {self.root}\nHEAD abc\n"]), \
                patch.object(lite.op, "_source_native_identity", return_value="workspace-fixture") as native:
            self.assertEqual(("project-fixture", "workspace-fixture"),
                             lite.qualified_identity(self.root, self.client))
        native.assert_called_once_with(
            project_id="project-fixture", project_root=self.root, primary_root=self.root,
            kent_path=Path(self.client.binary), source_only=True,
        )
        self.assertEqual([], self.client.writes)

    def test_git_failure_cannot_use_cached_primary_fallback(self):
        plan = self.plan()
        self.observe.stop()
        with patch.object(lite.op, "_source_canonical_root", return_value=self.root), \
                patch.object(lite.op, "_git", side_effect=lite.op.OperationError("Git failed")):
            with self.assertRaisesRegex(lite.op.OperationError, "Git failed"):
                self.apply(plan)
        self.assertEqual([], self.client.writes)

    def test_secondary_git_checkout_is_rejected_before_create(self):
        plan = self.plan()
        self.observe.stop()
        with patch.object(lite.op, "_source_canonical_root", return_value=self.root), \
                patch.object(lite.op, "_git", side_effect=[
                    str(self.root), "worktree /different/primary\nHEAD abc\n"]):
            with self.assertRaisesRegex(SpecError, "Git primary checkout"):
                self.apply(plan)
        self.assertEqual([], self.client.writes)

    def test_native_project_or_primary_mismatch_rejected_before_create(self):
        plan = self.plan()
        self.observe.stop()
        for message in ("native project identity/root differs",
                        "native default workspace root differs",
                        "native primary workspace identity is unavailable or ambiguous"):
            with self.subTest(message=message), \
                    patch.object(lite.op, "_source_canonical_root", return_value=self.root), \
                    patch.object(lite.op, "_git", side_effect=[
                        str(self.root), f"worktree {self.root}\nHEAD abc\n"]), \
                    patch.object(lite.op, "_source_native_identity",
                                 side_effect=lite.op.PlanValidationError(message)):
                with self.assertRaisesRegex(lite.op.PlanValidationError, message):
                    self.apply(plan)
                self.assertEqual([], self.client.writes)

    def test_workspace_catalog_mismatch_rejected_before_create(self):
        plan = self.plan()
        self.observe.stop()
        with patch.object(lite, "qualified_identity",
                          return_value=("project-fixture", "workspace-fixture")):
            for value in (
                {"project_id": "foreign", "offset": 0, "workspaces": []},
                {"project_id": "project-fixture", "offset": 0, "workspaces": []},
                {"project_id": "project-fixture", "offset": 0, "workspaces": [
                    {"workspace_id": "workspace-fixture", "is_default": True, "root_path": "/foreign"}]},
            ):
                with self.subTest(catalog=value):
                    self.client.run_json = lambda args: value
                    with self.assertRaisesRegex(SpecError, "workspace"):
                        self.apply(plan)
                    self.assertEqual([], self.client.writes)

    def test_each_executed_dependency_drift_rejected_before_create(self):
        import workflowkit.graph
        baseline_modules = Path(workflowkit.graph.__file__).parent
        self.observe.stop()
        with tempfile.TemporaryDirectory(dir=os.environ.get("LITE_TEST_ROOT")) as temporary:
            directory = Path(temporary).resolve()
            kit, root = directory / "kit", directory / "project"
            root.mkdir()
            for name in lite.KIT_FILES:
                target = kit / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(lite.KIT / name, target)
            for name in lite.DEPENDENCY_FILES:
                shutil.copyfile(baseline_modules / Path(name).name, kit / name)
            for name in lite.PROJECT_FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("Fixture project instruction.\n")
            binary = directory / "kent"
            binary.write_bytes(b"Fixture CLI executable bytes.\n")
            client = Client(root)
            client.binary = str(binary)
            original_run_json = client.run_json

            def run_json(args):
                if args[:2] == ["project", "workspaces"]:
                    return {"project_id": "project-fixture", "offset": 0, "workspaces": [{
                        "workspace_id": "workspace-fixture", "root_path": str(root), "is_default": True
                    }]}
                return original_run_json(args)

            client.run_json = run_json
            with patch.object(lite, "KIT", kit), \
                    patch.object(lite, "qualified_identity",
                                 return_value=("project-fixture", "workspace-fixture")):
                plan = lite.preview(root, client)
                for name in lite.DEPENDENCY_FILES:
                    with self.subTest(dependency=name):
                        filename = kit / name
                        before = filename.read_bytes()
                        filename.write_bytes(before + b"\n# Drift after preview.\n")
                        raw = lite.encode(plan)
                        with self.assertRaisesRegex(SpecError, "drift"):
                            lite.apply(plan, raw, lite.digest(raw), client)
                        self.assertEqual([], client.writes)
                        filename.write_bytes(before)

    def test_real_native_identity_helper_rejects_catalog_drift_before_create(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("LITE_TEST_ROOT")) as temporary:
            root = Path(temporary).resolve()
            client = Client(root)
            plan = lite.preview(root, client)
            self.observe.stop()
            # Execute the production identity validator, stubbing only native transport.
            with patch.object(lite.op, "_git", side_effect=[
                    str(root), f"worktree {root}\nHEAD abc\n"]), \
                    patch.object(lite.op, "_project_rows", return_value=[
                        {"id": "project-fixture", "path": str(root)}]), \
                    patch.object(lite.op, "_run", return_value=(0, b"project-fixture\n", b"")), \
                    patch.object(lite.op, "_kent_pages", return_value=[{
                        "project_id": "project-fixture", "offset": 0, "workspaces": [{
                            "workspace_id": "workspace-fixture", "root_path": str(root.parent),
                            "is_default": True,
                        }],
                    }]):
                raw = lite.encode(plan)
                with self.assertRaisesRegex(lite.op.PlanValidationError, "default workspace root differs"):
                    lite.apply(plan, raw, lite.digest(raw), client)
            self.assertEqual([], client.writes)

    def test_start_parameter_rejected_before_native_create(self):
        spec = lite.load_spec()
        start = next(edge for edge in spec.edges if edge.source == "backlog")
        invalid = replace(spec, edges=tuple(
            replace(edge, parameters=(ParameterSpec("plan_ref", "Unavailable at start."),))
            if edge == start else edge for edge in spec.edges))
        with self.assertRaisesRegex(SpecError, "start transitions cannot declare parameters"):
            lite.validate_native_inputs(invalid)
        self.assertEqual([], self.client.writes)

    def test_root_parameter_aliases_and_absent_carriers_are_rejected(self):
        spec = lite.load_spec()
        edge = next(edge for edge in spec.edges if edge.source == "plan")
        for expression in ("{{.plan_ref}}", "{{.Params.missing}}", "{{.Inputs.plan_ref}}",
                           "{{.Params.prior.plan_ref}}"):
            with self.subTest(expression=expression):
                invalid = replace(spec, edges=tuple(
                    replace(item, prompt=item.prompt + "\n" + expression) if item == edge else item
                    for item in spec.edges))
                with self.assertRaises(SpecError):
                    lite.validate_native_inputs(invalid)
        self.assertEqual([], self.client.writes)

    def test_start_cannot_reference_missing_incoming_value_or_source_session(self):
        spec = lite.load_spec()
        start = next(edge for edge in spec.edges if edge.source == "backlog")
        for expression in ("{{.Params.plan_ref}}", "{{.SessionId}}"):
            with self.subTest(expression=expression):
                invalid = replace(spec, edges=tuple(
                    replace(edge, prompt=edge.prompt + "\n" + expression) if edge == start else edge
                    for edge in spec.edges))
                with self.assertRaises(SpecError):
                    lite.validate_native_inputs(invalid)

    def test_load_spec_rejects_baseline_native_invariants_before_preview(self):
        value = json.loads((lite.KIT / lite.PACKAGE).read_bytes())
        start = next(edge for edge in value["edges"] if edge["source"] == "backlog")
        start["parameters"] = [{"key": "unavailable", "description": "No Agent producer."}]
        with patch.object(Path, "read_bytes", return_value=lite.encode(value)):
            with self.assertRaisesRegex(SpecError, "start transitions cannot declare parameters"):
                lite.preview(self.root, self.client)
        self.assertEqual([], self.client.writes)

    def test_empty_or_foreign_seed_rows_are_never_replaced(self):
        plan = self.plan()
        for mutation in ("empty", "extra_node", "edge", "node_group", "group", "unknown_field"):
            with self.subTest(mutation=mutation):
                client = Client(self.root)
                if mutation == "empty":
                    client.graph["nodes"] = []
                elif mutation == "extra_node":
                    client.graph["nodes"].append({
                        "id": "44444444-4444-4444-8444-444444444444", "key": "foreign", "kind": "agent"})
                elif mutation == "unknown_field":
                    client.graph["nodes"][0]["foreign"] = True
                else:
                    collection = {"edge": "edges", "node_group": "node_groups",
                                  "group": "transition_groups"}[mutation]
                    client.graph[collection].append({"id": "foreign"})
                raw = lite.encode(plan)
                with patch.object(Path, "resolve", return_value=self.root):
                    with self.assertRaisesRegex(KentCommandError, "seed"):
                        lite.apply(plan, raw, lite.digest(raw), client)
                self.assertEqual(["create"], [name for name, _ in client.writes])

    def test_new_seed_with_tasks_or_links_is_not_mutated(self):
        plan = self.plan()
        for method in ("workflow_has_tasks", "workflow_is_linked"):
            with self.subTest(method=method):
                client = Client(self.root)
                setattr(client, method, lambda definition: True)
                raw = lite.encode(plan)
                with patch.object(Path, "resolve", return_value=self.root):
                    with self.assertRaisesRegex(KentCommandError, "Tasks/links"):
                        lite.apply(plan, raw, lite.digest(raw), client)
                self.assertEqual(["create"], [name for name, _ in client.writes])

    def test_changed_seed_version_or_identity_is_blocked(self):
        plan = self.plan()
        created = {"id": self.client.workflow_id, "version": 1}
        for changes in ({"expected_version": 2}, {"workflow_id": "foreign"}):
            with self.subTest(changes=changes):
                inspected = dict(self.client.inspect_graph(created["id"]), **changes)
                with self.assertRaisesRegex(SpecError, "identity/version"):
                    lite.bind_fresh_seed(plan["graph"], inspected, created)


if __name__ == "__main__":
    unittest.main()
