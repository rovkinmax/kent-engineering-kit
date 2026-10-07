from __future__ import annotations

from dataclasses import replace
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from workflowkit.delivery import build_delivery_workflow
from workflowkit.kent import spec_as_json
from workflowkit.model import SpecError


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / ".kent/workflows/kit_development.py"
CHILD = ROOT / ".kent/scripts/workflow-compile-verify"
IDENTITY = ROOT / ".kent/scripts/kit-verification-identity.py"
FIXED_POINT = "4d0e514aebae5295c803693d72f6640ab2c7846d"
TASK_SOURCE = "787f683499f60bedd7b1acede729507b575bafc1"
QUALIFIED_TARGET = "38b40892dd688d181138890c9ae55d23aa715c95"
TARGET_BUILDER_SHA256 = "6e06e0a7c97825e21bd93e99d04141305cc3cfbc25d44759f5a69554e3700773"
TARGET_DELIVERY_SHA256 = "8e5432816b58aae7a6c4d06d64be6db25ca6150e46ef66e9d1ddb2122eddae77"
module_spec = importlib.util.spec_from_file_location("kit_development", BUILDER)
assert module_spec is not None and module_spec.loader is not None
kit = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(kit)

# Exact-history acceptance is a separate, explicitly executed qualification.
# Mandatory discovery retains portable production-main comparison controls.
# Run both pinned cases with KEN22_RUN_LOCAL_QUALIFICATION=1; missing objects
# are an error, never a skip or a substitute target.
LOCAL_QUALIFICATION_TESTS = {
    "test_fixed_point_v5_check_positive_and_inconsistent_controls_are_read_only",
    "test_target_candidate_and_mode_checks_are_read_only",
}


def load_tests(loader, tests, pattern):
    def included(suite):
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                yield from included(test)
            elif (
                not isinstance(test, KitDevelopmentWorkflowTest)
                or test._testMethodName not in LOCAL_QUALIFICATION_TESTS
                or os.environ.get("KEN22_RUN_LOCAL_QUALIFICATION") == "1"
            ):
                yield test

    return unittest.TestSuite(included(tests))


class KitDevelopmentWorkflowTest(unittest.TestCase):
    def fixture_git_environment(self) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        )
        return environment

    def source_archive(self, revision: str) -> Path:
        prerequisite = subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=ROOT, env=self.fixture_git_environment(),
            capture_output=True, check=False,
        )
        self.assertEqual(
            prerequisite.returncode, 0,
            f"explicit KEN-22 qualification requires exact commit {revision}: "
            + prerequisite.stderr.decode(),
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        archive = subprocess.run(
            ["git", "archive", "--format=tar", revision],
            cwd=ROOT, env=self.fixture_git_environment(),
            capture_output=True, check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(root)
        # Contained TMPDIR fixtures must not inherit the task's Git boundary:
        # git apply can otherwise report success while skipping every path.
        subprocess.run(
            ["git", "init", "-q", str(root)],
            env=self.fixture_git_environment(), capture_output=True, check=True,
        )
        discovered = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root, env=self.fixture_git_environment(),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(Path(discovered).resolve(), root)
        return root

    def tree_snapshot(self, root: Path) -> dict[str, tuple[str, int, str]]:
        import hashlib

        observed = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                observed[relative] = ("symlink", mode, os.readlink(path))
            elif path.is_file():
                observed[relative] = (
                    "file", mode, hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            elif path.is_dir():
                observed[relative] = ("directory", mode, "")
        return observed

    def run_builder(
        self, root: Path, *arguments: str, builder_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [
                sys.executable, "-B",
                str(builder_path or root / ".kent/workflows/kit_development.py"),
                *arguments,
            ],
            cwd=root, env=environment, capture_output=True, text=True, check=False,
        )

    def run_task_candidate_builder(
        self, root: Path, task_builder: Path, *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        probe = """
import importlib.util
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
task_builder = Path(sys.argv[2]).resolve()
arguments = sys.argv[3:]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

qualified = load(
    "qualified_kit",
    root / ".kent/workflows/kit_development.py",
)
task = load("task_kit", task_builder)
task.rendered_candidate_spec = qualified.rendered_candidate_spec
sys.argv = [str(task_builder), *arguments]
try:
    result = task.main()
except (OSError, ValueError) as error:
    print(f"Kit development workflow: {error}", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(result)
"""
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [
                sys.executable, "-B", "-c", probe,
                str(root), str(task_builder), *arguments,
            ],
            cwd=root, env=environment, capture_output=True, text=True, check=False,
        )

    def run_target_candidate_profile_probe(
        self, root: Path,
    ) -> subprocess.CompletedProcess[str]:
        probe = """
import importlib.util
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
path = root / ".kent/workflows/kit_development.py"
spec = importlib.util.spec_from_file_location("qualified_kit", path)
assert spec is not None and spec.loader is not None
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)
profile = kit.profile_at(root)
policies = dict(profile.policies)
assert policies.get("coder_selection") != "complexity"
default_before = json.loads(kit.rendered_spec(root))
candidate_workflow = kit.build_complexity_candidate_workflow(profile)
assert profile.policies == policies
assert candidate_workflow.nodes
default_after = json.loads(kit.rendered_spec(root))
candidate = json.loads(kit.rendered_candidate_spec(root))
nodes = {node["key"]: node for node in candidate["nodes"]}
edges = {edge["key"]: edge for edge in candidate["edges"]}
assert default_before == default_after
assert default_before["name"] == "Kit Engineering Delivery v5"
assert {node["key"]: node for node in default_before["nodes"]}["plan"]["agent"] != "complexity-planner"
assert nodes["plan"]["agent"] == "complexity-planner"
assert nodes["plan_revalidation"]["agent"] == "complexity-planner"
assert edges["plan_review_accept"]["requires_approval"] is True
"""
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [sys.executable, "-B", "-c", probe, str(root)],
            cwd=root, env=environment, capture_output=True, text=True, check=False,
        )

    def apply_task_source_delta(
        self, root: Path, path: str, expected_target_sha256: str,
    ) -> None:
        import hashlib

        target = root / path
        preimage = target.read_bytes()
        self.assertEqual(
            hashlib.sha256(preimage).hexdigest(),
            expected_target_sha256,
        )
        # The integrated checkout already contains KEN-18. Its complete
        # baseline delta cannot be applied over that same incoming target.
        # Pin the approved pre-integration Task-only overlay instead, retaining
        # the immutable Task baseline separately from the incoming target.
        patch = subprocess.run(
            ["git", "diff", "--binary", FIXED_POINT, TASK_SOURCE, "--", path],
            cwd=ROOT, env=self.fixture_git_environment(),
            capture_output=True, check=True,
        ).stdout
        self.assertTrue(patch, f"expected task delta for {path}")
        checked = subprocess.run(
            ["git", "apply", "--check", "--binary", "-"],
            cwd=root, input=patch, env=self.fixture_git_environment(),
            capture_output=True, check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr.decode())
        applied = subprocess.run(
            ["git", "apply", "--binary", "-"],
            cwd=root, input=patch, env=self.fixture_git_environment(),
            capture_output=True, check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr.decode())
        self.assertNotEqual(
            target.read_bytes(), preimage, "git apply must change the target",
        )
        self.assertEqual(
            target.read_bytes(), (ROOT / path).read_bytes(),
            "qualified target plus Task-only overlay must match integrated source",
        )
        reversed_check = subprocess.run(
            ["git", "apply", "--reverse", "--check", "--binary", "-"],
            cwd=root, input=patch, env=self.fixture_git_environment(),
            capture_output=True, check=False,
        )
        self.assertEqual(
            reversed_check.returncode, 0, reversed_check.stderr.decode(),
        )

    def install_task_builder_fixture(self, root: Path) -> Path:
        import hashlib

        target_builder = root / ".kent/workflows/kit_development.py"
        self.assertEqual(
            hashlib.sha256(target_builder.read_bytes()).hexdigest(),
            TARGET_BUILDER_SHA256,
        )
        task_builder = root / ".kent/workflows/kit_development_task.py"
        shutil.copyfile(BUILDER, task_builder)
        return task_builder

    def test_execution_repairs_do_not_enable_ci_monitoring(self) -> None:
        profile = kit.profile_at(ROOT)
        self.assertFalse(profile.capability("ci_monitoring"))
        role = (ROOT / "agents/implementation-worker.md").read_text()
        self.assertIn("full approved file boundary", role)
        self.assertIn("baseline debt, foreign changes", role)

    def test_planning_procedure_orders_grill_before_formal_reviews(self) -> None:
        procedure = (ROOT / ".kent/commands/plan.md").read_text()
        self.assertLess(procedure.index("grill critique"),
                        procedure.index("Then freeze"))
        self.assertLess(procedure.index("Then freeze"),
                        procedure.index("one independent read-only preview review"))
        self.assertIn("both independent reviews PASS", procedure)
        self.assertIn("not either independent PASS receipt", procedure)

    def test_evidence_custody_preserves_post_approval_sources(self) -> None:
        # Instruction-contract regression, not a simulation of agent behavior.
        plan = " ".join((ROOT / ".kent/commands/plan.md").read_text().split())
        implement = " ".join((ROOT / ".kent/commands/implement.md").read_text().split())
        for text in (
            "first post-approval Implement entry",
            "original preview ScopeHash",
            "Never record an expected or pending approval as accepted",
        ):
            with self.subTest(owner="plan", requirement=text):
                self.assertIn(text, plan)
        for text in (
            "first accepted entry and after material plan revalidation",
            "original human decision",
            "both same-hash PASS receipts",
            "read back",
            "not only transient `review_context`",
            "Do not repeat this capture on every writer slice",
        ):
            with self.subTest(owner="implement", requirement=text):
                self.assertIn(text, implement)

    def test_evidence_custody_cleanup_assembles_available_records(self) -> None:
        cleanup = " ".join((ROOT / ".kent/commands/cleanup-task.md").read_text().split())
        for text in (
            "Cleanup owns assembling and reading back `retention_receipt`",
            "missing preassembled receipt alone is not an external blocker",
            "historical Plan comment",
            "original decision and review sources",
            "outside the future-deleted root",
            "original ScopeHash",
        ):
            with self.subTest(requirement=text):
                self.assertIn(text, cleanup)
        self.assertLess(cleanup.index("Cleanup owns assembling"),
                        cleanup.index("Invoke the profile's `prepare_cleanup`"))

    def test_evidence_custody_does_not_invent_authority_or_seal(self) -> None:
        cleanup = " ".join((ROOT / ".kent/commands/cleanup-task.md").read_text().split())
        for text in (
            "pending approval, current node or downstream progression",
            "does not prove original human consent",
            "inaccessible original decision",
            "conflicting scope/hash",
            "ambiguous ownership",
            "name the exact missing fact",
            "Cleanup also owns the redaction and operation-report proofs",
            "real current unmodified `KENT_SESSION_ID`",
            "Do not issue any standalone ledger append",
            "never accept its seal as valid",
        ):
            with self.subTest(requirement=text):
                self.assertIn(text, cleanup)

    def test_evidence_custody_role_recovers_bookkeeping_not_consent(self) -> None:
        role = " ".join((ROOT / "agents/delivery-operator.md").read_text().split())
        for text in (
            "Missing agent bookkeeping is not missing human authority",
            "bounded recovery from accessible original sources",
            "Never invent consent",
            "an older pending decision as the current outcome",
            "exact unavailable fact or required external action",
        ):
            with self.subTest(requirement=text):
                self.assertIn(text, role)

    def fixture(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        shutil.copytree(ROOT / ".kent/context", root / ".kent/context")
        shutil.copytree(ROOT / ".kent/commands", root / ".kent/commands")
        shutil.copyfile(
            ROOT / ".kent/workflow-profile.toml",
            root / ".kent/workflow-profile.toml",
        )
        (root / ".kent/scripts").mkdir()
        shutil.copy2(CHILD, root / ".kent/scripts/workflow-compile-verify")
        shutil.copy2(ROOT / ".kent/scripts/workflow-prepare-cleanup",
                     root / ".kent/scripts/workflow-prepare-cleanup")
        return root

    def test_profile_and_full_command_closure(self) -> None:
        profile = kit.profile_at(ROOT)
        self.assertEqual(profile.schema_version, 3)
        self.assertEqual(profile.minimum_kent_version, "2.7.2")
        self.assertEqual(profile.execution_default, "default-branch")
        self.assertEqual(profile.delivery_profile, "lite")
        self.assertEqual(profile.writer_session_policy(), "continuous")
        self.assertEqual(profile.branch_identity_policy(), "task")
        self.assertEqual(profile.pr_merge_strategy(), "rebase")
        self.assertEqual(profile.smoke_policy(), "disabled")
        self.assertNotEqual(profile.policies.get("coder_selection"), "complexity")
        self.assertEqual(profile.release_topology, "none")
        self.assertFalse(profile.runtime_contracts_v2())
        self.assertFalse(profile.adapters)
        self.assertEqual(set(profile.work_kinds), {
            "feature", "bugfix", "refactor", "migration", "dependency", "test",
        })
        self.assertEqual(set(profile.commands), {
            *kit.SCHEMA3_COPIES, *kit.PROJECT_COPIES, "compile_verify", "prepare_cleanup",
        })
        self.assertEqual(set(profile.context_manifests), {
            "plan", "implement", "review", "delivery", "smoke",
        })
        for kind in profile.work_kinds.values():
            self.assertEqual(kind.plan, ".kent/commands/plan.md")
            self.assertEqual(kind.implement, ".kent/commands/implement.md")
        for path in profile.procedures.values():
            self.assertTrue((ROOT / path).is_file())
        kit.verify_command_closure()

    def test_exact_graph_and_approval_delta(self) -> None:
        profile = kit.profile_at(ROOT)
        base = build_delivery_workflow(profile, 5)
        spec = kit.build_workflow(profile)
        spec.validate()  # Includes context source and parameter topology.
        self.assertEqual(spec.name, "Kit Engineering Delivery v5")
        self.assertEqual(spec.nodes, base.nodes)
        self.assertEqual(len(spec.nodes), 21)
        self.assertEqual(len(spec.edges), 53)
        self.assertEqual(len({
            (edge.source, edge.transition) for edge in spec.edges
        }), 52)
        self.assertEqual({node.key for node in spec.nodes}, {
            "backlog", "plan", "plan_review", "plan_contract",
            "plan_contract_continue", "plan_contract_verify",
            "plan_revalidation", "implement", "verification_dispatch",
            "deterministic_verify", "standards_review", "verification_join",
            "verification_gate", "fix", "prepare_pr", "waiting_pr",
            "merge_watch", "cleanup", "task_janitor", "done", "wont_do",
        })
        changed_approvals = []
        for original, actual in zip(base.edges, spec.edges, strict=True):
            expected = original
            if actual.key == "plan_review_accept":
                expected = replace(expected, requires_approval=True)
                changed_approvals.append(actual.key)
            if actual.target == "plan_review":
                expected = replace(
                    expected,
                    prompt=(expected.prompt or "") + kit.PLAN_REVIEW_GUIDANCE,
                )
                self.assertIn("different reviewer Session", actual.prompt)
                self.assertIn("exact preview SHA-256", actual.prompt)
            self.assertEqual(actual, expected)
        self.assertEqual(changed_approvals, ["plan_review_accept"])
        instruction_prompt_targets = {
            "prepare_pr_no_pr",
            "fix_pr_merged_cleanup",
            "waiting_pr_cleanup",
            "merge_watch_cleanup",
            "waiting_pr_close_without_merge",
            "task_janitor_blocked",
        }
        observed_instruction_targets = {
            edge.key
            for edge in spec.edges
            if edge.prompt
            and "`git -C <workspace_path> branch --show-current`" in edge.prompt
        }
        self.assertEqual(observed_instruction_targets, instruction_prompt_targets)
        for edge in spec.edges:
            if edge.key in instruction_prompt_targets:
                self.assertIn("workspace_path", edge.prompt)
                self.assertIn("branch_name", edge.prompt)
        for node in spec.nodes:
            if node.kind == "agent":
                self.assertEqual(node.completion_mode, "shell_command")

    def test_every_cleanup_entrance_delegates_single_terminal_owner(self) -> None:
        spec = kit.build_workflow(kit.profile_at(ROOT))
        entrances = [edge for edge in spec.edges if edge.target == "cleanup"]
        self.assertEqual({edge.key for edge in entrances}, {
            "prepare_pr_no_pr", "fix_pr_merged_cleanup", "waiting_pr_cleanup",
            "merge_watch_cleanup", "waiting_pr_close_without_merge",
            "task_janitor_blocked", "cleanup_needs_user_action",
        })
        for edge in entrances:
            with self.subTest(edge=edge.key):
                self.assertIn(".kent/scripts/workflow-prepare-cleanup", edge.prompt)
                self.assertNotIn("workflow-evidence-ledger append", edge.prompt)
                self.assertIn("unmodified", edge.prompt)
                self.assertIn("KENT_RUN_ID", edge.prompt)

    def test_v5_preserves_historical_v1_through_v5_snapshots(self) -> None:
        import hashlib

        historical_v1 = ROOT / ".kent/workflows/kit-engineering-delivery-v1.spec.json"
        raw = historical_v1.read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "3f586796ee574682251716f28eb38eb5abf475cb8bc0c2f04003bb0d9d41decc",
        )
        historical_v2 = ROOT / ".kent/workflows/kit-engineering-delivery-v2.spec.json"
        raw_v2 = historical_v2.read_bytes()
        self.assertEqual(
            hashlib.sha256(raw_v2).hexdigest(),
            "6fbbf4187bd8d705678b080f2f8a1f66d44b907d8de8063f50d4aaacf0a99ea7",
        )
        self.assertEqual(json.loads(raw)["name"], "Kit Engineering Delivery v1")
        self.assertEqual(json.loads(raw_v2)["name"], "Kit Engineering Delivery v2")
        raw_v3 = (ROOT / ".kent/workflows/kit-engineering-delivery-v3.spec.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(raw_v3).hexdigest(),
            "9650b3a597a8999cd1504fb4c67f9fc676b26dd567351603bf49c07887bebba7",
        )
        raw_v4 = (ROOT / ".kent/workflows/kit-engineering-delivery-v4.spec.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(raw_v4).hexdigest(),
            "fead032959ae10b569a99c68dd2d524b8374c1882d821a049000113bba674924",
        )
        historical_v5 = ROOT / ".kent/workflows/kit-engineering-delivery-v5.spec.json"
        raw_v5 = historical_v5.read_bytes()
        self.assertEqual(
            hashlib.sha256(raw_v5).hexdigest(),
            "b4ad79a3499d1a65ab749b8f3a98f2837a4b519eb8f4a0629d240dd6e9efcd55",
        )
        self.assertEqual(json.loads(raw_v5)["name"], "Kit Engineering Delivery v5")
        current = json.loads(kit.rendered_spec())
        self.assertEqual(current["name"], "Kit Engineering Delivery v5")
        self.assertNotEqual(current, json.loads(raw_v2))

    def test_v5_snapshot_is_exact_and_g1_g2_delta_is_bounded(self) -> None:
        import hashlib

        snapshot = ROOT / kit.SPEC_PATH
        before = snapshot.read_bytes()
        self.assertEqual(
            hashlib.sha256(before).hexdigest(),
            "b4ad79a3499d1a65ab749b8f3a98f2837a4b519eb8f4a0629d240dd6e9efcd55",
        )
        historical = json.loads(before)
        rendered = json.loads(json.dumps(spec_as_json(
            kit.build_workflow(kit.profile_at(ROOT)),
        )))
        self.assertEqual(rendered, json.loads(kit.rendered_spec()))
        self.assertEqual(historical.keys(), rendered.keys())
        self.assertEqual(historical["nodes"], rendered["nodes"])
        for field in historical.keys() - {"edges"}:
            self.assertEqual(historical[field], rendered[field], field)
        historical_edges = {edge["key"]: edge for edge in historical["edges"]}
        rendered_edges = {edge["key"]: edge for edge in rendered["edges"]}
        self.assertEqual(historical_edges.keys(), rendered_edges.keys())
        changed_fields = {}
        for key in sorted(historical_edges):
            before_edge = historical_edges[key]
            after_edge = rendered_edges[key]
            fields = {
                field
                for field in before_edge.keys() | after_edge.keys()
                if before_edge.get(field) != after_edge.get(field)
            }
            if fields:
                changed_fields[key] = fields
        self.assertEqual(changed_fields, {
            "gate_delivery_ready": {"prompt"},
            "merge_watch_cleanup": {"parameters"},
        })
        self.assertEqual(
            [param["key"] for param in rendered_edges["merge_watch_cleanup"]["parameters"]],
            [
                "workspace_path", "pr_url", "branch_name", "merge_strategy",
                "merge_report", "pr_feedback_cursor",
            ],
        )
        self.assertIn(
            "Git actions require exact current human approval",
            rendered_edges["gate_delivery_ready"]["prompt"],
        )

        self.assertEqual(before, snapshot.read_bytes())

    def test_fixed_point_v5_check_positive_and_inconsistent_controls_are_read_only(self) -> None:
        root = self.source_archive(FIXED_POINT)
        snapshot = root / ".kent/workflows/kit-engineering-delivery-v5.spec.json"
        original_v5 = snapshot.read_bytes()
        before = self.tree_snapshot(root)

        default = self.run_builder(root)
        self.assertEqual(default.returncode, 0, default.stderr)
        default_spec = json.loads(default.stdout)
        self.assertEqual(default_spec["name"], "Kit Engineering Delivery v5")
        default_nodes = {node["key"]: node for node in default_spec["nodes"]}
        self.assertNotEqual(default_nodes["plan"]["agent"], "complexity-planner")
        self.assertEqual(before, self.tree_snapshot(root))

        write_spec = self.run_builder(root, "--write-spec")
        self.assertEqual(write_spec.returncode, 0, write_spec.stderr)
        self.assertEqual(snapshot.read_bytes(), original_v5)
        self.assertFalse(
            (root / ".kent/workflows/kit-engineering-delivery-v6.spec.json").exists()
        )

        result = self.run_builder(root, "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.tree_snapshot(root))

        snapshot.write_bytes(snapshot.read_bytes() + b" ")
        inconsistent = self.tree_snapshot(root)
        result = self.run_builder(root, "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("semantic workflow spec is stale", result.stderr)
        self.assertEqual(inconsistent, self.tree_snapshot(root))

    def test_candidate_check_fails_closed_without_qualified_target_renderer(self) -> None:
        # Actual integration now supplies the renderer. Test the missing
        # prerequisite in a genuinely unqualified disposable main() fixture.
        sync = kit.load_synchronizer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            before = self.tree_snapshot(root)
            with (
                mock.patch.object(kit, "ROOT", root),
                mock.patch.object(kit, "load_synchronizer", return_value=sync),
                mock.patch.dict(kit.__dict__, rendered_candidate_spec=None),
                mock.patch.object(sys, "argv", [str(BUILDER), "--check-candidate"]),
            ):
                with self.assertRaisesRegex(
                    SpecError, "qualified target fixture required",
                ):
                    kit.main()
            self.assertEqual(before, self.tree_snapshot(root))

    def test_checkout_local_legacy_comparison_controls_are_read_only(self) -> None:
        # Portable comparison/closure coverage, not fixed-point parity
        # acceptance. The exact fixed-point qualification remains mandatory
        # for this repair's handoff and is executed separately.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            snapshot = root / kit.SPEC_PATH
            snapshot.parent.mkdir(parents=True)
            # Preserve real command-closure validation: main's temporary
            # source root contains the checkout-local authoritative templates.
            for relative in (kit.SCHEMA3_COPIES | kit.PROJECT_COPIES).values():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            expected = json.dumps(
                spec_as_json(kit.build_workflow(kit.profile_at(ROOT))),
                indent=2, ensure_ascii=False,
            ) + "\n"
            snapshot.write_text(expected)
            sync = kit.load_synchronizer()
            with (
                mock.patch.object(kit, "ROOT", root),
                mock.patch.object(kit, "load_synchronizer", return_value=sync),
                mock.patch.object(sys, "argv", [str(BUILDER), "--check"]),
            ):
                before = self.tree_snapshot(root)
                self.assertEqual(kit.main(), 0)
                self.assertEqual(before, self.tree_snapshot(root))
                snapshot.write_text(expected + " ")
                altered = self.tree_snapshot(root)
                with self.assertRaisesRegex(SpecError, "semantic workflow spec is stale"):
                    kit.main()
                self.assertEqual(altered, self.tree_snapshot(root))

    def test_checkout_local_candidate_comparison_and_mode_controls_are_read_only(self) -> None:
        # An independent synthetic renderer isolates the production main()
        # comparison. It does not qualify the real v6 artifact; the pinned
        # target-plus-task test below is the separate acceptance proof.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate = root / ".kent/workflows/kit-engineering-delivery-v6.spec.json"
            candidate.parent.mkdir(parents=True)
            expected = '{"fixture": "portable comparison control"}\n'
            candidate.write_text(expected)
            sync = kit.load_synchronizer()
            renderer = mock.Mock(return_value=expected)
            with (
                mock.patch.object(kit, "ROOT", root),
                mock.patch.object(kit, "load_synchronizer", return_value=sync),
                mock.patch.dict(kit.__dict__, rendered_candidate_spec=renderer),
                mock.patch.object(sys, "argv", [str(BUILDER), "--check-candidate"]),
            ):
                before = self.tree_snapshot(root)
                self.assertEqual(kit.main(), 0)
                renderer.assert_called_once_with()
                self.assertEqual(before, self.tree_snapshot(root))
                candidate.write_text(expected + " ")
                altered = self.tree_snapshot(root)
                with self.assertRaisesRegex(SpecError, "complexity candidate spec is stale"):
                    kit.main()
                self.assertEqual(altered, self.tree_snapshot(root))
                candidate.write_text(expected)
                for flag in ("--bootstrap", "--write-spec", "--write-candidate-spec"):
                    with (
                        self.subTest(flag=flag),
                        mock.patch.object(
                            sys, "argv", [str(BUILDER), "--check-candidate", flag],
                        ),
                        mock.patch.object(kit, "bootstrap") as bootstrap,
                        mock.patch.object(kit, "rendered_spec") as legacy_renderer,
                        mock.patch("sys.stderr", new_callable=io.StringIO),
                    ):
                        renderer.reset_mock()
                        before = self.tree_snapshot(root)
                        with self.assertRaises((SpecError, SystemExit)):
                            kit.main()
                        renderer.assert_not_called()
                        legacy_renderer.assert_not_called()
                        bootstrap.assert_not_called()
                        self.assertEqual(before, self.tree_snapshot(root))

            comparison = 'if target.read_bytes() != candidate_renderer().encode("utf-8"):\n'
            source = BUILDER.read_text()
            self.assertEqual(source.count(comparison), 1)
            mutated_path = root / ".kent/workflows/kit_development_mutated.py"
            mutated_path.write_text(source.replace(
                comparison,
                'if target.read_bytes() == candidate_renderer().encode("utf-8"):\n',
                1,
            ))
            spec = importlib.util.spec_from_file_location("mutated_kit", mutated_path)
            assert spec is not None and spec.loader is not None
            mutated = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mutated)
            with (
                mock.patch.object(mutated, "load_synchronizer", return_value=sync),
                mock.patch.dict(mutated.__dict__, rendered_candidate_spec=renderer),
                mock.patch.object(sys, "argv", [str(mutated_path), "--check-candidate"]),
            ):
                before = self.tree_snapshot(root)
                with self.assertRaisesRegex(SpecError, "complexity candidate spec is stale"):
                    mutated.main()
                self.assertEqual(before, self.tree_snapshot(root))

    def test_integrated_candidate_renderer_and_main_controls_are_read_only(self) -> None:
        # This is the actual combined checkout renderer, not the synthetic
        # comparison seam or the separately pinned historical acceptance.
        artifact = ROOT / kit.CANDIDATE_SPEC_PATH
        source_roots = (
            ROOT / "workflowkit",
            ROOT / ".kent/workflows",
            ROOT / ".kent/scripts",
        )
        before_source = {path: self.tree_snapshot(path) for path in source_roots}
        profile_before = (ROOT / ".kent/workflow-profile.toml").read_bytes()
        self.assertEqual(artifact.read_bytes(), kit.rendered_candidate_spec().encode())
        result = self.run_builder(ROOT, "--check-candidate")
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = self.run_target_candidate_profile_probe(ROOT)
        self.assertEqual(probe.returncode, 0, probe.stderr)

        sync = kit.load_synchronizer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate = root / kit.CANDIDATE_SPEC_PATH
            candidate.parent.mkdir(parents=True)
            shutil.copyfile(artifact, candidate)
            # Only the artifact lookup is redirected. The real renderer's
            # default root remains the actual integrated source/profile.
            with (
                mock.patch.object(kit, "ROOT", root),
                mock.patch.object(kit, "load_synchronizer", return_value=sync),
                mock.patch.object(sys, "argv", [str(BUILDER), "--check-candidate"]),
            ):
                before = self.tree_snapshot(root)
                self.assertEqual(kit.main(), 0)
                self.assertEqual(before, self.tree_snapshot(root))
                candidate.write_bytes(candidate.read_bytes() + b" ")
                altered = self.tree_snapshot(root)
                with self.assertRaisesRegex(SpecError, "complexity candidate spec is stale"):
                    kit.main()
                self.assertEqual(altered, self.tree_snapshot(root))
                for flag in ("--bootstrap", "--write-spec", "--write-candidate-spec"):
                    with (
                        self.subTest(flag=flag),
                        mock.patch.object(
                            sys, "argv", [str(BUILDER), "--check-candidate", flag],
                        ),
                        mock.patch.object(kit, "bootstrap") as bootstrap,
                        mock.patch("sys.stderr", new_callable=io.StringIO),
                    ):
                        rejected = self.tree_snapshot(root)
                        with self.assertRaises((SpecError, SystemExit)):
                            kit.main()
                        bootstrap.assert_not_called()
                        self.assertEqual(rejected, self.tree_snapshot(root))
                candidate.unlink()
                candidate.symlink_to(artifact)
                linked = self.tree_snapshot(root)
                with self.assertRaises((OSError, ValueError)):
                    kit.main()
                self.assertEqual(linked, self.tree_snapshot(root))

        self.assertEqual(
            before_source, {path: self.tree_snapshot(path) for path in source_roots},
        )
        self.assertEqual(profile_before, (ROOT / ".kent/workflow-profile.toml").read_bytes())

    def test_target_candidate_and_mode_checks_are_read_only(self) -> None:
        root = self.source_archive(QUALIFIED_TARGET)
        self.apply_task_source_delta(
            root, "workflowkit/delivery.py", TARGET_DELIVERY_SHA256,
        )
        task_builder = self.install_task_builder_fixture(root)
        shutil.copyfile(
            ROOT / ".kent/workflows/kit-engineering-delivery-v6.spec.json",
            root / ".kent/workflows/kit-engineering-delivery-v6.spec.json",
        )
        candidate = root / ".kent/workflows/kit-engineering-delivery-v6.spec.json"

        before = self.tree_snapshot(root)
        profile_probe = self.run_target_candidate_profile_probe(root)
        self.assertEqual(profile_probe.returncode, 0, profile_probe.stderr)
        self.assertEqual(before, self.tree_snapshot(root))

        default = self.run_builder(root, builder_path=task_builder)
        self.assertEqual(default.returncode, 0, default.stderr)
        default_spec = json.loads(default.stdout)
        self.assertEqual(default_spec["name"], "Kit Engineering Delivery v5")
        default_nodes = {node["key"]: node for node in default_spec["nodes"]}
        self.assertNotEqual(default_nodes["plan"]["agent"], "complexity-planner")
        self.assertEqual(before, self.tree_snapshot(root))

        result = self.run_task_candidate_builder(
            root, task_builder, "--check-candidate",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.tree_snapshot(root))

        # Mutation control: invert the actual Task comparison and confirm the
        # exact production main() path rejects a matching candidate.
        task_source = task_builder.read_text()
        comparison = (
            'if target.read_bytes() != candidate_renderer().encode("utf-8"):\n'
        )
        self.assertEqual(task_source.count(comparison), 1)
        mutated_source = task_source.replace(
            comparison,
            'if target.read_bytes() == candidate_renderer().encode("utf-8"):\n',
            1,
        )
        mutated_builder = root / ".kent/workflows/kit_development_task_mutated.py"
        mutated_builder.write_text(mutated_source)
        before_mutation_check = self.tree_snapshot(root)
        mutation = self.run_task_candidate_builder(
            root, mutated_builder, "--check-candidate",
        )
        self.assertEqual(mutation.returncode, 1, mutation.stderr)
        self.assertIn(
            "source-only complexity candidate spec is stale", mutation.stderr,
        )
        self.assertEqual(before_mutation_check, self.tree_snapshot(root))

        candidate.write_bytes(candidate.read_bytes() + b" ")
        altered = self.tree_snapshot(root)
        result = self.run_task_candidate_builder(
            root, task_builder, "--check-candidate",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("source-only complexity candidate spec is stale", result.stderr)
        self.assertEqual(altered, self.tree_snapshot(root))

        candidate.write_bytes(candidate.read_bytes()[:-1])
        for arguments in (
            ("--check-candidate", "--bootstrap"),
            ("--check-candidate", "--write-spec"),
            ("--check-candidate", "--write-candidate-spec"),
        ):
            with self.subTest(arguments=arguments):
                rejected = self.tree_snapshot(root)
                result = self.run_task_candidate_builder(
                    root, task_builder, *arguments,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(rejected, self.tree_snapshot(root))
                if "--bootstrap" in arguments:
                    self.assertIn(
                        "cannot be combined with --bootstrap", result.stderr,
                    )

        # A v6 artifact change does not alter the legacy v5 --check result.
        candidate.write_bytes(candidate.read_bytes() + b" ")
        altered_v6 = self.tree_snapshot(root)
        result = self.run_builder(root, "--check", builder_path=task_builder)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("semantic workflow spec is stale", result.stderr)
        self.assertNotIn("complexity candidate spec is stale", result.stderr)
        self.assertEqual(altered_v6, self.tree_snapshot(root))

    def test_v6_candidate_captures_approved_delivery_delta_and_complexity_graph(self) -> None:
        import hashlib

        path = ROOT / ".kent/workflows/kit-engineering-delivery-v6.spec.json"
        raw = path.read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "c08c666607fccd627c29db241ab8436975fae9f30c4cb711f4866cd97d01a7ff",
        )
        candidate = json.loads(raw)
        historical = json.loads(
            (ROOT / ".kent/workflows/kit-engineering-delivery-v5.spec.json").read_text()
        )
        self.assertEqual(candidate["name"], "Kit Engineering Delivery v6")
        self.assertEqual(candidate["execution_target"], "default-branch")
        candidate_nodes = {node["key"]: node for node in candidate["nodes"]}
        historical_nodes = {node["key"]: node for node in historical["nodes"]}
        self.assertEqual(candidate_nodes.keys(), historical_nodes.keys())
        self.assertEqual(candidate_nodes["plan"]["agent"], "complexity-planner")
        self.assertEqual(
            candidate_nodes["plan_revalidation"]["agent"],
            "complexity-planner",
        )
        candidate_edges = {edge["key"]: edge for edge in candidate["edges"]}
        historical_edges = {edge["key"]: edge for edge in historical["edges"]}
        self.assertEqual(
            candidate_edges.keys() - historical_edges.keys(),
            {"implement_revalidate", "plan_contract_revalidate"},
        )
        self.assertTrue(candidate_edges["plan_review_accept"]["requires_approval"])
        self.assertEqual(
            len({(edge["source"], edge["transition"]) for edge in candidate["edges"]}),
            54,
        )
        self.assertEqual(len(candidate["nodes"]), 21)
        self.assertEqual(len(candidate["edges"]), 55)
        self.assertEqual(
            [param["key"] for param in candidate_edges["merge_watch_cleanup"]["parameters"]],
            [
                "workspace_path", "pr_url", "branch_name", "merge_strategy",
                "merge_report", "pr_feedback_cursor",
            ],
        )
        self.assertIn(
            "Git actions require exact current human approval",
            candidate_edges["gate_delivery_ready"]["prompt"],
        )

    def test_default_emits_only_json_without_materialization(self) -> None:
        with mock.patch.object(kit, "bootstrap") as bootstrap:
            with mock.patch.object(sys, "argv", [str(BUILDER)]):
                with mock.patch("sys.stdout") as output:
                    self.assertEqual(kit.main(), 0)
            bootstrap.assert_not_called()
            self.assertEqual(
                json.loads(output.write.call_args.args[0])["execution_target"],
                "default-branch",
            )

    def test_import_never_bootstraps(self) -> None:
        with mock.patch.object(Path, "write_text") as write:
            with mock.patch.object(Path, "write_bytes") as write_bytes:
                module_spec.loader.exec_module(kit)
            write.assert_not_called()
            write_bytes.assert_not_called()

    def test_governance_and_source_procedures(self) -> None:
        plan = (ROOT / ".kent/commands/plan.md").read_text()
        for text in (
            "exactly\n   one independent read-only preview review",
            "architecture-designer", "researcher", "SHA-256",
            "both independent reviews PASS on the same preview hash",
            "Revalidation", "refresh the first independent review",
            "no third routine preview review",
            "plan_path=not-applicable", "without tracked writes",
        ):
            self.assertIn(text, plan)
        review = (ROOT / ".kent/context/review.md").read_text()
        self.assertIn("Plan Review: `.kent/commands/plan.md`", review)
        contract = (ROOT / ".kent/project-contract.md").read_text()
        for text in (
            "immutable task-delta baseline", "moving PR target",
            "Missing agent bookkeeping is not",
            "no tracked or staged writes",
            "current published branch", "never checkout/fast-forward",
        ):
            self.assertIn(text, contract)
        readme = (ROOT / "README.md").read_text()
        self.assertIn("git fetch origin", readme)
        self.assertIn("does not implicitly fetch", readme)
        ship = (ROOT / ".kent/commands/ship-pr.md").read_text()
        self.assertIn("`rebase`", ship)
        self.assertIn("`canBeRebased`", ship)
        self.assertIn("must be true", ship)
        cleanup = (ROOT / ".kent/commands/cleanup-task.md").read_text()
        self.assertIn("actual worktree path absence and absent Git registration", cleanup)
        self.assertIn("exact original managed record", cleanup)
        self.assertIn("retained restorative metadata", cleanup)
        self.assertIn("Close only processes proven owned", cleanup)
        self.assertIn("cleanup_mode=no_pr", cleanup)

    def test_bootstrap_is_explicit_complete_and_idempotent(self) -> None:
        self.assert_bootstrap_is_explicit_complete_and_idempotent(self.fixture())

    def assert_bootstrap_is_explicit_complete_and_idempotent(self, root: Path) -> None:
        child = root / ".kent/scripts/workflow-compile-verify"
        child_mode = stat.S_IMODE(child.stat().st_mode)
        preparation = root / ".kent/scripts/workflow-prepare-cleanup"
        preparation_mode = stat.S_IMODE(preparation.stat().st_mode)
        kit.bootstrap(root)
        profile = kit.profile_at(root)
        before = {
            key: (root / path).read_bytes()
            for key, path in profile.commands.items()
        }
        kit.bootstrap(root)
        kit.verify_command_closure(root)
        self.assertEqual(before, {
            key: (root / path).read_bytes()
            for key, path in profile.commands.items()
        })
        for key in kit.SCHEMA3_COPIES | kit.PROJECT_COPIES:
            target = root / profile.command(key)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(child.stat().st_mode), child_mode)
        self.assertTrue(child_mode & stat.S_IXUSR)
        self.assertFalse(child_mode & 0o7022)
        self.assertEqual(stat.S_IMODE(preparation.stat().st_mode), preparation_mode)
        self.assertTrue(preparation_mode & stat.S_IXUSR)
        self.assertFalse(preparation_mode & 0o7022)

    def test_bootstrap_fixture_accepts_private_source_child(self) -> None:
        root = self.fixture()
        child = root / ".kent/scripts/workflow-compile-verify"
        child.chmod(0o700)
        (root / ".kent/scripts/workflow-prepare-cleanup").chmod(0o700)
        self.assert_bootstrap_is_explicit_complete_and_idempotent(root)

    def test_private_checkout_executable_commands_pass_closure(self) -> None:
        root = self.fixture()
        kit.bootstrap(root)
        profile = kit.profile_at(root)
        before = {
            path: (root / path).read_bytes()
            for path in profile.commands.values()
        }
        for path in profile.commands.values():
            (root / path).chmod(0o700)
        kit.verify_command_closure(root)
        self.assertEqual(before, {
            path: (root / path).read_bytes()
            for path in profile.commands.values()
        })
        for path in profile.commands.values():
            self.assertEqual(stat.S_IMODE((root / path).stat().st_mode), 0o700)

    def test_closure_rejects_missing_owner_execute_and_unsafe_modes(self) -> None:
        for mode in (0o600, 0o644, 0o611, 0o720, 0o702, 0o777, 0o4755):
            with self.subTest(mode=oct(mode)):
                root = self.fixture()
                kit.bootstrap(root)
                target = root / ".kent/scripts/workflow-compile-verify"
                target.chmod(mode)
                with self.assertRaises(ValueError):
                    kit.verify_command_closure(root)

    def test_closure_rejects_symlink_nonregular_and_byte_drift(self) -> None:
        for kind in ("symlink", "directory", "drift"):
            with self.subTest(kind=kind):
                root = self.fixture()
                kit.bootstrap(root)
                target = root / ".kent/scripts/workflow-verify-report"
                if kind == "drift":
                    target.write_text("changed generated copy\n")
                else:
                    target.rename(target.with_name("retained-report"))
                    if kind == "symlink":
                        target.symlink_to(ROOT / ".kent/scripts/workflow-verify-report")
                    else:
                        target.mkdir()
                with self.assertRaises(ValueError):
                    kit.verify_command_closure(root)

    def test_cleanup_document_completion_keys_match_graph(self) -> None:
        document = (ROOT / ".kent/commands/cleanup-task.md").read_text()
        edges = [
            edge for edge in kit.build_workflow(kit.profile_at(ROOT)).edges
            if edge.source == "cleanup"
        ]
        parameters = {parameter.key for edge in edges for parameter in edge.parameters}
        preparation = command_module(
            ROOT / ".kent/scripts/workflow-prepare-cleanup", "kit_doc_preparation",
        )
        named_keys = set(re.findall(r"`(cleanup_[a-z_]+)`", document))
        self.assertEqual(
            named_keys - parameters - preparation.REQUEST_KEYS,
            {edge.transition for edge in edges},
        )
        self.assertIn("closed_without_merge", document)
        self.assertIn("Before `kent worktree leave`, freeze the original", document)
        self.assertIn("prepare final evidence through that worktree", document)
        self.assertIn("do not run\nrelative project commands there", document)
        self.assertIn("Submit the frozen carrier", document)
        contract = (ROOT / ".kent/project-contract.md").read_text()
        self.assertIn("Source-only investigation and deterministic tests", contract)
        self.assertIn("including device-resource", contract)

    def test_bootstrap_preflights_all_drift_before_first_write(self) -> None:
        root = self.fixture()
        drift = root / ".kent/scripts/workflow_runtime_contracts.py"
        drift.write_text("unknown existing source\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            kit.bootstrap(root)
        self.assertEqual(drift.read_text(), "unknown existing source\n")
        self.assertFalse((root / ".kent/scripts/workflow-checkpoint").exists())

    def test_bootstrap_refuses_existing_generated_drift(self) -> None:
        root = self.fixture()
        kit.bootstrap(root)
        target = root / ".kent/scripts/workflow-plan-contract"
        target.write_text("changed generated copy\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            kit.bootstrap(root)
        self.assertEqual(target.read_text(), "changed generated copy\n")

    def test_bootstrap_refuses_target_symlink_and_directory(self) -> None:
        for kind in ("symlink", "directory"):
            with self.subTest(kind=kind):
                root = self.fixture()
                target = root / ".kent/scripts/workflow-evidence-ledger"
                if kind == "symlink":
                    target.symlink_to(CHILD)
                else:
                    target.mkdir()
                with self.assertRaises(ValueError):
                    kit.bootstrap(root)
                self.assertFalse(
                    (root / ".kent/scripts/workflow-checkpoint").exists(),
                )

    def test_bootstrap_refuses_parent_symlink(self) -> None:
        root = self.fixture()
        scripts = root / ".kent/scripts"
        destination = root / "other-scripts"
        scripts.rename(destination)
        scripts.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            kit.bootstrap(root)
        self.assertFalse((destination / "workflow-checkpoint").exists())

    def test_bootstrap_refuses_source_parent_symlink(self) -> None:
        root = self.fixture()
        source = self.fixture()
        (source / "templates").symlink_to(
            ROOT / "templates", target_is_directory=True,
        )
        sync = kit.load_synchronizer()
        with mock.patch.object(kit, "ROOT", source):
            with mock.patch.object(kit, "load_synchronizer", return_value=sync):
                with self.assertRaisesRegex(ValueError, "symlinks"):
                    kit.bootstrap(root)
        self.assertFalse((root / ".kent/scripts/workflow-checkpoint").exists())

    def test_bootstrap_rejects_unknown_command_before_writes(self) -> None:
        root = self.fixture()
        profile = kit.profile_at(root)
        with mock.patch.object(
            kit, "profile_at",
            return_value=replace(profile, commands={
                **profile.commands, "unknown": ".kent/scripts/unknown",
            }),
        ):
            with self.assertRaisesRegex(SpecError, "closed"):
                kit.bootstrap(root)
        self.assertFalse((root / ".kent/scripts/workflow-checkpoint").exists())

    def test_bootstrap_rejects_redirected_command_before_writes(self) -> None:
        root = self.fixture()
        profile = kit.profile_at(root)
        with mock.patch.object(
            kit, "profile_at",
            return_value=replace(profile, commands={
                **profile.commands, "checkpoint": ".kent/scripts/unapproved",
            }),
        ):
            with self.assertRaisesRegex(SpecError, "closed"):
                kit.bootstrap(root)
        self.assertFalse((root / ".kent/scripts/unapproved").exists())


class CompileVerifierTest(unittest.TestCase):
    def fixture(self, exit_code: int = 0) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / ".gitignore").write_text("/build/kent-workflow/\n")
        (root / "scripts").mkdir()
        validator = root / "scripts/validate"
        validator.write_text(
            "#!/bin/bash\nset -e\n"
            '[[ $# == 0 ]] || exit 97\n'
            "python3 -c 'import sys; assert sys.version_info >= (3, 11)'\n"
            "echo fixture-validation-log\n"
            f"exit {exit_code}\n",
        )
        validator.chmod(0o755)
        scripts = root / ".kent/scripts"
        scripts.mkdir(parents=True)
        for source in (
            CHILD,
            ROOT / ".kent/scripts/workflow-verify-report",
            ROOT / ".kent/scripts/workflow_runtime_contracts.py",
            IDENTITY,
        ):
            shutil.copy2(source, scripts / source.name)
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            [
                "git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid",
                "-c", "core.hooksPath=/dev/null",
                "commit", "--quiet", "-m", "Fixture baseline",
            ],
            check=True,
        )
        return root

    def run_child(self, root: Path, *, payload: str = "{}",
                  override: str | None = None) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment.pop("KENT_ENGINEERING_KIT_PYTHON", None)
        private_tmpdir = root / "build/kent-workflow/.verify-tmp-fixture"
        private_tmpdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment["TMPDIR"] = str(private_tmpdir)
        if override is not None:
            environment["KENT_ENGINEERING_KIT_PYTHON"] = override
        return subprocess.run(
            [str(root / ".kent/scripts/workflow-compile-verify")],
            cwd=root, input=payload, env=environment,
            text=True, capture_output=True, check=False,
        )

    def test_child_strict_pass_fail_and_stderr(self) -> None:
        for code, transition in ((0, "passed"), (7, "failed")):
            with self.subTest(code=code):
                result = self.run_child(self.fixture(code))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout), {"transition": transition},
                )
                self.assertIn("fixture-validation-log", result.stderr)

    def test_child_blocks_invalid_override_without_fallback(self) -> None:
        for override in ("", "/missing/python3", "/bin/false"):
            with self.subTest(override=override):
                result = self.run_child(self.fixture(), override=override)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(
                    json.loads(result.stdout), {"transition": "blocked"},
                )
                self.assertNotIn("fixture-validation-log", result.stderr)

    def test_child_honors_existing_override(self) -> None:
        result = self.run_child(self.fixture(), override=sys.executable)
        self.assertEqual(json.loads(result.stdout), {"transition": "passed"})

    def test_child_blocks_invalid_or_multiple_json_values(self) -> None:
        for payload in ("", "[]", "{} {}", "{bad", "x" * 1048577):
            with self.subTest(payload=payload[:20]):
                result = self.run_child(self.fixture(), payload=payload)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(
                    json.loads(result.stdout), {"transition": "blocked"},
                )

    def test_child_missing_local_validation_is_blocked(self) -> None:
        root = self.fixture()
        (root / "scripts/validate").rename(root / "scripts/retained-validate")
        result = self.run_child(root)
        self.assertEqual(json.loads(result.stdout), {"transition": "blocked"})

    def test_real_report_wrapper_descriptor_launch(self) -> None:
        for code, status in ((0, "passed"), (5, "needs_changes")):
            with self.subTest(code=code):
                root = self.fixture(code)
                # This isolated fixture only initializes Git metadata; it never
                # commits, pushes, or invokes native Kent workflow operations.
                subprocess.run(
                    ["git", "init", "--quiet", str(root)], check=True,
                    capture_output=True,
                )
                (root / ".gitignore").write_text("/build/kent-workflow/\n")
                scripts = root / ".kent/scripts"
                scripts.mkdir(parents=True, exist_ok=True)
                for name in (
                    "workflow-compile-verify", "workflow-verify-report",
                    "workflow_runtime_contracts.py",
                    "kit-verification-identity.py",
                ):
                    shutil.copy2(ROOT / ".kent/scripts" / name, scripts / name)
                result = subprocess.run(
                    [sys.executable, str(scripts / "workflow-verify-report")],
                    cwd=root, input=json.dumps({"workspace_path": str(root)}),
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(
                    payload["verification_status"], status, result.stdout,
                )
                report = json.loads(payload["verification_report"])
                log = root / report["log_path"]
                self.assertIn("fixture-validation-log", log.read_text())


def command_module(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class VerifierEnvironmentFixtureTest(unittest.TestCase):
    def test_source_copy_exclusions_are_rooted_and_preserve_working_source(self) -> None:
        from tests import test_ci_contract

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            destination = Path(temporary) / "copy"
            preserved = (
                "uncommitted-source.txt", "src/build/source.txt",
                "src/.kent/runtime/source.txt", "runtime/source.txt",
                ".kent/workflows/source.txt",
            )
            excluded = ("build/generated.txt", ".kent/runtime/private.txt")
            for relative in preserved + excluded:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            with mock.patch.object(test_ci_contract, "ROOT", root):
                shutil.copytree(
                    root, destination, ignore=test_ci_contract.source_validation_copy_ignore,
                )
            for relative in preserved:
                self.assertEqual((destination / relative).read_text(), relative)
            self.assertFalse((destination / "build").exists())
            self.assertFalse((destination / ".kent/runtime").exists())

    def run_actual_method_with_contained_tmpdir(self, method: str) -> None:
        build = ROOT / "build/kent-workflow"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=build, prefix="fixture-long-tmp-" + "x" * 80,
        ) as temporary:
            temporary_root = Path(temporary).resolve()
            self.assertTrue(temporary_root.is_relative_to(ROOT))
            self.assertGreater(len(os.fsencode(temporary_root / "socket")), 108)
            result = subprocess.run(
                [
                    sys.executable, "-B", "-c",
                    "import os, pathlib, sys, tempfile, unittest\n"
                    "assert pathlib.Path(tempfile.gettempdir()).resolve() == "
                    "pathlib.Path(os.environ['TMPDIR']).resolve()\n"
                    "suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])\n"
                    "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
                    "raise SystemExit(not result.wasSuccessful())\n",
                    method,
                ],
                cwd=ROOT,
                env={**os.environ, "TMPDIR": str(temporary_root)},
                text=True, capture_output=True, timeout=360, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(temporary_root.exists())

    def test_actual_source_validation_copy_under_long_contained_tmpdir(self) -> None:
        self.run_actual_method_with_contained_tmpdir(
            "tests.test_ci_contract.CiContractTest."
            "test_source_only_validation_does_not_create_installed_state",
        )

    def test_actual_socket_manifest_under_long_contained_tmpdir(self) -> None:
        self.run_actual_method_with_contained_tmpdir(
            "tests.test_workflow_retirement.WorkflowRetirementTest."
            "test_manifest_rejects_symlink_fifo_and_socket_entries",
        )


class CleanupPreparationTest(unittest.TestCase):
    def fixture(
        self,
        *,
        plan: bool = False,
        prepare_cleanup: bool = True,
    ) -> Path:
        fixture_owner = KitDevelopmentWorkflowTest()
        self.addCleanup(fixture_owner.doCleanups)
        root = fixture_owner.fixture()
        kit.bootstrap(root)
        if not prepare_cleanup:
            profile_path = root / ".kent/workflow-profile.toml"
            profile_contents = profile_path.read_text()
            profile_contents = re.sub(
                r"(?m)^prepare_cleanup\s*=\s*[^\n]*\n",
                "",
                profile_contents,
            )
            profile_path.write_text(profile_contents)
            import tomllib

            self.assertNotIn(
                "prepare_cleanup",
                tomllib.loads(profile_contents).get("commands", {}),
            )
        (root / ".gitignore").write_text("/.kent/runtime/\n/build/kent-workflow/\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        # Only a disposable fixture repository receives this initial commit.
        # The source worktree, remote refs and native Kent state are untouched.
        subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
        subprocess.run([
            "git", "-C", str(root), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null",
            "commit", "-q", "-m", "Fixture baseline",
        ], check=True)
        if plan:
            (root / "plan.md").write_text("# Plan\n- [ ] Implement scoped change\n")
        self.environment = {
            **os.environ, "KENT_SESSION_ID": "fixture-session",
            "KENT_RUN_ID": "fixture-plan", "KENT_STEP_ID": "fixture-step",
        }
        accepted = self.command(root, "workflow-plan-contract", {
            "workspace_path": str(root), "task_short_id": "TASK-1",
            "plan_path": "plan.md" if plan else "not-applicable",
            "work_kind": "test", "review_context": "Explicit fixture approval",
            "plan_route": "start", "plan_route_context": "not-applicable",
        }, environment={**self.environment, "KENT_PLAN_CONTRACT_MODE": "accept"})
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.event = {
            "node_key": "cleanup", "evidence_type": "cleanup_preparation",
            "summary": "Retained fixture authority before terminal preparation",
            "artifacts": [], "checks": ["Fixture readback passed"],
            "decisions": ["Explicit report-only fixture authority"],
            "context": {
                "manifest_path": ".kent/context/delivery.md",
                "files_read": [".kent/commands/cleanup-task.md"],
            },
        }
        appended = self.command(root, "workflow-evidence-ledger", self.event,
                                "append", "--task", "TASK-1", "--workspace", str(root))
        self.assertEqual(appended.returncode, 0, appended.stderr)
        self.seal_request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": "a" * 64},
            "retention_class": "cleanup_report_only",
        }
        return root

    def command(self, root, name, payload, *args, environment=None):
        return subprocess.run(
            [sys.executable, str(root / ".kent/scripts" / name), *args],
            cwd=root, input=json.dumps(payload), text=True, capture_output=True,
            env=environment or self.environment, check=False,
        )

    def admission(self, root, report):
        janitor = command_module(
            root / ".kent/scripts/workflow-task-janitor", "kit_fixture_janitor",
        )
        return janitor._prepare_v2_managed_runtime_state(
            root, "TASK-1", cleanup_report=report,
        )

    def helper(self, root):
        return command_module(
            root / ".kent/scripts/workflow-prepare-cleanup", "kit_fixture_preparation",
        )

    def request(self, root):
        helper = self.helper(root)
        raw = (root / ".kent/runtime/TASK-1/plan-contract.json").read_text()
        data = {
            "snapshot_utf8": raw, "snapshot_sha256": helper.digest(raw.encode()),
            "scope_sha256": "b" * 64, "review_refs": ["review-session-1", "review-session-2"],
            "approval_ref": "TASK-1/comment/approval",
        }
        event = dict(self.event, summary="Final cleanup preparation with retained Task evidence")
        return {
            "workspace_path": str(root), "task_short_id": "TASK-1",
            "snapshot_sha256": data["snapshot_sha256"], "scope_sha256": data["scope_sha256"],
            "retention_receipt": {
                "task_short_id": "TASK-1", "record_ref": "TASK-1/comment/retention",
                "readback_sha256": helper.digest(helper.canonical(data)), "data": data,
            },
            "final_event": event, "seal_request": self.seal_request,
            "cleanup_report_prefix": "Explicit fixture authority retained; cleanup pending.",
        }

    def prepare(self, root, request, *, run_id="fixture-cleanup"):
        return self.command(
            root, "workflow-prepare-cleanup", request,
            environment={**self.environment, "KENT_RUN_ID": run_id},
        )

    def add_ci_archive(
        self,
        root: Path,
        *,
        referenced: bool,
        wrong_digest: bool = False,
        invalid_schema: bool = False,
    ) -> tuple[Path, bytes, str]:
        import hashlib

        from tests.test_runtime_contracts import ci_report
        from workflowkit import runtime

        report = {"schema": "unsupported-ci-report"} if invalid_schema else ci_report()
        if not invalid_schema:
            report["attempts"][0]["head_oid"] = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        raw = runtime.canonical_bytes(report)
        content_digest = hashlib.sha256(raw).hexdigest()
        filename_digest = "0" * 64 if wrong_digest else content_digest
        name = f"ci-report-{filename_digest}.json"
        archive = root / ".kent/runtime/TASK-1" / name
        archive.write_bytes(raw)
        archive.chmod(0o600)
        if referenced:
            artifact = f".kent/runtime/TASK-1/{name}"
            event = {
                "node_key": "ci_prepare",
                "evidence_type": "ci_report",
                "summary": "Retained validated CI archive fixture",
                "artifacts": [artifact],
                "checks": ["Synthetic canonical CI archive"],
                "decisions": [],
                "context": {
                    "manifest_path": ".kent/context/delivery.md",
                    "files_read": [],
                    "model_calls": 0,
                    "compaction_count": 0,
                },
            }
            append = self.command(
                root,
                "workflow-evidence-ledger",
                event,
                "append",
                "--task",
                "TASK-1",
                "--workspace",
                str(root),
                environment={
                    **self.environment,
                    "KENT_RUN_ID": "fixture-ci-archive",
                    "KENT_STEP_ID": "fixture-ci-archive-step",
                },
            )
            self.assertEqual(append.returncode, 0, append.stderr)
        return archive, raw, name

    def test_actual_preparation_to_janitor_admission_and_tombstone_replay(self) -> None:
        for regular_plan in (False, True):
            with self.subTest(regular_plan=regular_plan):
                root = self.fixture(plan=regular_plan)
                request = self.request(root)
                result = self.prepare(root, request)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)["cleanup_report"]
                helper = self.helper(root)
                marker = helper.load_command(root, "workflow-evidence-ledger")._runtime_module().validate_cleanup_report(report)
                self.assertEqual(marker["task_short_id"], "TASK-1")
                state = root / ".kent/runtime/TASK-1"
                self.assertFalse((state / "plan-contract.json").exists())
                archive = root / f"build/kent-workflow/TASK-1/plan-contract-{request['snapshot_sha256']}.json"
                self.assertEqual(archive.read_text(), request["retention_receipt"]["data"]["snapshot_utf8"])
                self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
                receipt = root / "build/kent-workflow/TASK-1/cleanup-preparation.json"
                frozen = json.loads(receipt.read_text())
                self.assertEqual(frozen["request"]["scope_sha256"], "b" * 64)
                self.assertNotEqual(
                    "b" * 64,
                    json.loads(archive.read_text())["normalized_sha256"],
                )
                original = (state / "evidence-ledger.jsonl").read_bytes()
                actions = []
                invoke = helper.invoke_ledger

                def record_ledger_action(root, task, action, payload, environment):
                    actions.append(action)
                    return invoke(root, task, action, payload, environment)

                with mock.patch.object(
                    helper,
                    "invoke_ledger",
                    side_effect=record_ledger_action,
                ):
                    with mock.patch.dict(
                        os.environ,
                        {
                            **self.environment,
                            "KENT_RUN_ID": "fixture-retry",
                        },
                    ):
                        repeated = helper.prepare(request)
                self.assertEqual(repeated["cleanup_report"], report)
                self.assertEqual((state / "evidence-ledger.jsonl").read_bytes(), original)
                self.assertEqual(actions, [])
                admitted = self.admission(root, report)
                self.assertTrue(admitted[0], admitted)
                self.assertFalse(state.exists())
                tombstones = list(
                    (root / ".kent/runtime").glob(".evidence-cleanup-*")
                )
                self.assertEqual(len(tombstones), 1)
                retained_ledger = tombstones[0] / "evidence-ledger.jsonl"
                retained_bytes = retained_ledger.read_bytes()
                retained_records = [
                    json.loads(line)
                    for line in retained_bytes.decode().splitlines()
                ]
                original_ids = tuple(
                    retained_records[-2][key]
                    for key in ("run_id", "session_id", "step_id")
                )
                actions = []
                with mock.patch.object(
                    helper,
                    "invoke_ledger",
                    side_effect=lambda root, task, action, payload, environment: (
                        actions.append(action),
                        invoke(root, task, action, payload, environment),
                    )[1],
                ):
                    with mock.patch.dict(
                        os.environ,
                        {
                            **self.environment,
                            "KENT_RUN_ID": "fixture-after-janitor",
                        },
                    ):
                        after_tombstone = helper.prepare(request)
                self.assertEqual(after_tombstone["cleanup_report"], report)
                self.assertEqual(actions, [])
                self.assertEqual(retained_ledger.read_bytes(), retained_bytes)
                replayed_records = [
                    json.loads(line)
                    for line in retained_ledger.read_text().splitlines()
                ]
                self.assertEqual(
                    tuple(
                        replayed_records[-2][key]
                        for key in ("run_id", "session_id", "step_id")
                    ),
                    original_ids,
                )
                self.assertTrue(self.admission(root, report)[0])
                self.assertTrue(root.exists())  # Admission only; never native Delete.

    def test_referenced_ci_archive_flows_through_preparation_and_janitor(self) -> None:
        root = self.fixture()
        archive, expected, _ = self.add_ci_archive(root, referenced=True)
        request = self.request(root)
        source = root / ".kent/runtime/TASK-1/plan-contract.json"
        result = self.prepare(root, request)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)["cleanup_report"]
        self.assertFalse(source.exists())
        self.assertEqual(archive.read_bytes(), expected)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        admitted = self.admission(root, report)
        self.assertTrue(admitted[0], admitted)
        tombstones = list(
            (root / ".kent/runtime").glob(".evidence-cleanup-*")
        )
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(
            (tombstones[0] / archive.name).read_bytes(),
            expected,
        )

    def test_ci_archive_requires_ledger_reference_digest_and_schema(self) -> None:
        for defect in ("unreferenced", "wrong_digest", "invalid_schema"):
            with self.subTest(defect=defect):
                root = self.fixture()
                archive, archive_before, _ = self.add_ci_archive(
                    root,
                    referenced=defect != "unreferenced",
                    wrong_digest=defect == "wrong_digest",
                    invalid_schema=defect == "invalid_schema",
                )
                request = self.request(root)
                source = root / ".kent/runtime/TASK-1/plan-contract.json"
                source_before = source.read_bytes()
                ledger = source.with_name("evidence-ledger.jsonl")
                ledger_before = ledger.read_bytes()

                result = self.prepare(root, request)

                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(source.is_file())
                self.assertEqual(source.read_bytes(), source_before)
                self.assertEqual(ledger.read_bytes(), ledger_before)
                self.assertTrue(archive.is_file())
                self.assertEqual(archive.read_bytes(), archive_before)

    def test_completed_tombstone_replay_blocks_corrupt_chain_without_ledger_effects(
        self,
    ) -> None:
        from workflowkit import runtime

        root = self.fixture()
        request = self.request(root)
        prepared = self.prepare(root, request)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        report = json.loads(prepared.stdout)["cleanup_report"]
        self.assertTrue(self.admission(root, report)[0])
        tombstones = list(
            (root / ".kent/runtime").glob(".evidence-cleanup-*")
        )
        self.assertEqual(len(tombstones), 1)
        ledger = tombstones[0] / "evidence-ledger.jsonl"
        records = [
            json.loads(line)
            for line in ledger.read_text().splitlines()
        ]
        records[-2]["summary"] += " changed"
        ledger.write_bytes(
            b"".join(runtime.canonical_bytes(record) + b"\n" for record in records)
        )
        corrupted = ledger.read_bytes()
        helper = self.helper(root)
        actions = []
        invoke = helper.invoke_ledger

        def record_action(root, task, action, payload, environment):
            actions.append(action)
            return invoke(root, task, action, payload, environment)

        with mock.patch.object(
            helper,
            "invoke_ledger",
            side_effect=record_action,
        ):
            with mock.patch.dict(
                os.environ,
                {
                    **self.environment,
                    "KENT_RUN_ID": "fixture-corrupt-tombstone-retry",
                },
            ):
                with self.assertRaisesRegex(ValueError, "invalid hash"):
                    helper.prepare(request)

        self.assertEqual(actions, [])
        self.assertEqual(ledger.read_bytes(), corrupted)

    def test_unknown_runtime_entry_blocks_without_retirement(self) -> None:
        root = self.fixture()
        request = self.request(root)
        unexpected = root / ".kent/runtime/TASK-1/preview.json"
        unexpected.write_text("unknown preview\n")
        result = self.prepare(root, request)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown restricted", result.stderr)
        self.assertEqual(unexpected.read_text(), "unknown preview\n")
        self.assertTrue((unexpected.parent / "plan-contract.json").exists())

    def test_snapshot_and_retention_mismatches_preserve_source(self) -> None:
        for defect in ("raw", "scope", "receipt", "task", "plan", "work_kind", "normalized"):
            with self.subTest(defect=defect):
                root = self.fixture(plan=True)
                request = self.request(root)
                if defect == "raw":
                    request["snapshot_sha256"] = "c" * 64
                elif defect == "scope":
                    request["scope_sha256"] = "c" * 64
                elif defect == "receipt":
                    request["retention_receipt"] = None
                elif defect == "task":
                    request["retention_receipt"]["task_short_id"] = "TASK-2"
                elif defect == "plan":
                    (root / "plan.md").write_text("Changed plan\n")
                else:
                    helper = self.helper(root)
                    snapshot_path = root / ".kent/runtime/TASK-1/plan-contract.json"
                    snapshot = json.loads(snapshot_path.read_text())
                    snapshot["work_kind" if defect == "work_kind" else "normalized_sha256"] = (
                        "unconfigured" if defect == "work_kind" else "f" * 64
                    )
                    snapshot_path.write_text(json.dumps(snapshot))
                    request = self.request(root)
                result = self.prepare(root, request)
                self.assertNotEqual(result.returncode, 0, defect)
                self.assertTrue((root / ".kent/runtime/TASK-1/plan-contract.json").exists())

    def test_source_safety_and_tracked_state_block(self) -> None:
        for defect in ("symlink", "hardlink", "public", "directory", "tracked"):
            with self.subTest(defect=defect):
                root = self.fixture()
                request = self.request(root)
                source = root / ".kent/runtime/TASK-1/plan-contract.json"
                if defect == "public":
                    source.chmod(0o644)
                elif defect == "tracked":
                    subprocess.run(["git", "-C", str(root), "add", "-f", str(source)], check=True)
                else:
                    retained = root / "retained-snapshot"
                    if defect == "hardlink":
                        os.link(source, retained)
                    else:
                        source.rename(retained)
                        if defect == "symlink":
                            source.symlink_to(retained)
                        else:
                            source.mkdir()
                result = self.prepare(root, request)
                self.assertNotEqual(result.returncode, 0, defect)
                self.assertTrue(source.exists())

    def test_final_event_run_dedup_cannot_suppress_unrelated_event(self) -> None:
        root = self.fixture()
        request = self.request(root)
        before = (root / ".kent/runtime/TASK-1/evidence-ledger.jsonl").read_bytes()
        result = self.prepare(root, request, run_id="fixture-plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not appended exactly once", result.stderr)
        self.assertEqual((root / ".kent/runtime/TASK-1/evidence-ledger.jsonl").read_bytes(), before)
        retry = self.prepare(root, request, run_id="fixture-cleanup-new-run")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue(self.admission(root, json.loads(retry.stdout)["cleanup_report"])[0])

    def test_retry_after_append_before_seal_uses_same_final_event(self) -> None:
        root = self.fixture()
        request = self.request(root)
        helper = self.helper(root)
        invoke = helper.invoke_ledger

        def fail_seal(root, task, action, payload, environment):
            if action == "seal":
                raise ValueError("injected failure before seal")
            return invoke(root, task, action, payload, environment)

        with mock.patch.dict(os.environ, {**self.environment, "KENT_RUN_ID": "fixture-cleanup"}):
            with mock.patch.object(helper, "invoke_ledger", side_effect=fail_seal):
                with self.assertRaisesRegex(ValueError, "injected"):
                    helper.prepare(request)
        ledger = root / ".kent/runtime/TASK-1/evidence-ledger.jsonl"
        before = ledger.read_text().splitlines()
        self.assertEqual(len(before), 2)
        actions = []
        invoke = helper.invoke_ledger

        def record_ledger_action(root, task, action, payload, environment):
            actions.append(action)
            return invoke(root, task, action, payload, environment)

        with mock.patch.dict(
            os.environ,
            {**self.environment, "KENT_RUN_ID": "fixture-new-run"},
        ):
            with mock.patch.object(
                helper,
                "invoke_ledger",
                side_effect=record_ledger_action,
            ):
                retry = helper.prepare(request)
        self.assertTrue(retry["cleanup_report"])
        self.assertEqual(actions, ["seal"])
        after = ledger.read_text().splitlines()
        self.assertEqual(after[:2], before)
        self.assertEqual(len(after), 3)

    def test_matching_archive_and_prepared_source_recover_without_overwrite(self) -> None:
        for boundary in ("archive", "receipt", "seal"):
            with self.subTest(boundary=boundary):
                root = self.fixture()
                request = self.request(root)
                helper = self.helper(root)
                write = helper.write_private

                def fail_after_write(path, raw, *, replace=False):
                    if boundary == "seal" and replace:
                        raise ValueError("injected retained-write boundary")
                    write(path, raw, replace=replace)
                    if ((boundary == "archive" and path.name.startswith("plan-contract-"))
                            or (boundary == "receipt" and path.name == "cleanup-preparation.json")):
                        raise ValueError("injected retained-write boundary")

                with mock.patch.dict(os.environ, {**self.environment, "KENT_RUN_ID": "cleanup-start"}):
                    with mock.patch.object(helper, "write_private", side_effect=fail_after_write):
                        with self.assertRaisesRegex(ValueError, "injected"):
                            helper.prepare(request)
                source = root / ".kent/runtime/TASK-1/plan-contract.json"
                self.assertEqual(source.exists(), boundary != "seal")
                archive = root / f"build/kent-workflow/TASK-1/plan-contract-{request['snapshot_sha256']}.json"
                original = archive.read_bytes()
                if boundary == "seal":
                    ledger = root / ".kent/runtime/TASK-1/evidence-ledger.jsonl"
                    ledger_before = ledger.read_bytes()
                    records_before = [
                        json.loads(line)
                        for line in ledger_before.decode().splitlines()
                    ]
                    original_ids = tuple(
                        records_before[-2][key]
                        for key in ("run_id", "session_id", "step_id")
                    )
                    actions = []
                    invoke = helper.invoke_ledger

                    def record_ledger_action(
                        root, task, action, payload, environment,
                    ):
                        actions.append(action)
                        return invoke(root, task, action, payload, environment)

                    with mock.patch.object(
                        helper,
                        "invoke_ledger",
                        side_effect=record_ledger_action,
                    ):
                        with mock.patch.dict(
                            os.environ,
                            {
                                **self.environment,
                                "KENT_RUN_ID": "cleanup-recovery",
                            },
                        ):
                            retried = helper.prepare(request)
                    self.assertEqual(actions, [])
                    retried_report = retried["cleanup_report"]
                    self.assertEqual(ledger.read_bytes(), ledger_before)
                    records_after = [
                        json.loads(line)
                        for line in ledger.read_text().splitlines()
                    ]
                    self.assertEqual(
                        tuple(
                            records_after[-2][key]
                            for key in ("run_id", "session_id", "step_id")
                        ),
                        original_ids,
                    )
                else:
                    retried = self.prepare(
                        root, request, run_id="cleanup-recovery",
                    )
                    self.assertEqual(retried.returncode, 0, retried.stderr)
                    retried_report = json.loads(
                        retried.stdout,
                    )["cleanup_report"]
                self.assertEqual(archive.read_bytes(), original)
                self.assertTrue(self.admission(root, retried_report)[0])

    def test_plan_path_and_archive_parent_symlinks_block(self) -> None:
        for defect in ("plan", "archive_parent"):
            with self.subTest(defect=defect):
                root = self.fixture(plan=True)
                request = self.request(root)
                if defect == "plan":
                    (root / "plan.md").rename(root / "retained-plan.md")
                    (root / "plan.md").symlink_to(root / "retained-plan.md")
                else:
                    (root / "build").mkdir()
                    (root / "outside-build").mkdir()
                    (root / "build/kent-workflow").symlink_to(root / "outside-build")
                result = self.prepare(root, request)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((root / ".kent/runtime/TASK-1/plan-contract.json").exists())

    def test_retained_snapshot_has_closed_schema_and_correct_task(self) -> None:
        for defect in ("extra", "task", "schema"):
            with self.subTest(defect=defect):
                root = self.fixture()
                source = root / ".kent/runtime/TASK-1/plan-contract.json"
                payload = json.loads(source.read_text())
                if defect == "extra":
                    payload["unknown"] = "preserve"
                elif defect == "task":
                    payload["task_short_id"] = "TASK-2"
                else:
                    payload["schema_version"] = True
                source.write_text(json.dumps(payload))
                request = self.request(root)
                result = self.prepare(root, request)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(source.exists())

    def test_invalid_seal_kind_and_duplicate_marker_are_rejected(self) -> None:
        for defect in ("kind", "marker"):
            with self.subTest(defect=defect):
                root = self.fixture()
                request = self.request(root)
                if defect == "kind":
                    request["seal_request"]["operation_report_digests"] = [
                        {"kind": "plan", "sha256": "a" * 64},
                    ]
                else:
                    request["cleanup_report_prefix"] += "\nTERMINAL_EVIDENCE_V1 forged"
                result = self.prepare(root, request)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((root / ".kent/runtime/TASK-1/plan-contract.json").exists())

    def test_conflicting_or_missing_preparation_proof_is_not_rebuilt(self) -> None:
        for defect in ("receipt", "archive", "request"):
            with self.subTest(defect=defect):
                root = self.fixture()
                request = self.request(root)
                result = self.prepare(root, request)
                self.assertEqual(result.returncode, 0, result.stderr)
                store = root / "build/kent-workflow/TASK-1"
                if defect == "receipt":
                    (store / "cleanup-preparation.json").rename(store / "retained-receipt")
                elif defect == "archive":
                    archive = store / f"plan-contract-{request['snapshot_sha256']}.json"
                    archive.write_text("conflicting archive")
                else:
                    request["seal_request"]["redaction"]["report_sha256"] = "f" * 64
                repeated = self.prepare(root, request, run_id="retry")
                self.assertNotEqual(repeated.returncode, 0)
                self.assertFalse((root / ".kent/runtime/TASK-1/plan-contract.json").exists())

    def test_real_unsealed_plan_contract_is_blocked_by_janitor(self) -> None:
        root = self.fixture()
        result = self.admission(root, "Fixture retained authority")
        self.assertFalse(result[0])
        self.assertIn("not a sealed", result[1])
        self.assertTrue((root / ".kent/runtime/TASK-1/plan-contract.json").exists())

    def test_real_sealed_plan_snapshot_is_blocked_by_janitor(self) -> None:
        root = self.fixture()
        source = root / ".kent/runtime/TASK-1/plan-contract.json"
        expected = source.read_bytes()
        sealed = self.command(
            root, "workflow-evidence-ledger", self.seal_request,
            "seal", "--task", "TASK-1", "--workspace", str(root),
        )
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        report = "Fixture retained authority\n" + json.loads(sealed.stdout)["terminal_marker"]
        result = self.admission(root, report)
        self.assertFalse(result[0], result)
        self.assertIn("plan-contract.json", result[1])
        self.assertTrue((root / ".kent/runtime/TASK-1").exists())
        self.assertEqual(source.read_bytes(), expected)
        tombstones = list((root / ".kent/runtime").glob(".evidence-cleanup-*"))
        self.assertEqual(tombstones, [])

    def test_absent_preparation_opt_in_recovers_original_seal_read_only(self) -> None:
        import hashlib

        root = self.fixture(plan=True, prepare_cleanup=False)
        task = "TASK-1"
        source = root / f".kent/runtime/{task}/plan-contract.json"
        source_bytes = source.read_bytes()
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        retained_directory = root / f"build/kent-workflow/{task}"
        retained_directory.mkdir(parents=True)
        retained_cache = retained_directory / "plan-contract-no-helper.json"
        retained_cache.write_bytes(source_bytes)
        retained_cache.chmod(0o600)
        self.assertEqual(retained_cache.read_bytes(), source_bytes)
        self.assertEqual(
            hashlib.sha256(retained_cache.read_bytes()).hexdigest(),
            source_digest,
        )
        source.unlink()

        cleanup_environment = {
            **self.environment,
            "KENT_RUN_ID": "fixture-no-helper-cleanup",
            "KENT_STEP_ID": "fixture-no-helper-step",
        }
        final_event = {
            "node_key": "cleanup",
            "evidence_type": "delivery",
            "summary": "Completed synthetic no-helper Cleanup",
            "artifacts": [],
            "checks": ["Original retention and readback passed"],
            "decisions": ["No preparation helper is configured"],
            "context": {
                "manifest_path": ".kent/context/delivery.md",
                "files_read": [".kent/commands/cleanup-task.md"],
                "model_calls": None,
                "compaction_count": None,
                "repeated_questions": 0,
                "verification_loops": 0,
            },
        }
        appended = self.command(
            root,
            "workflow-evidence-ledger",
            final_event,
            "append",
            "--task",
            task,
            "--workspace",
            str(root),
            environment=cleanup_environment,
        )
        self.assertEqual(appended.returncode, 0, appended.stderr)

        runtime = command_module(
            root / ".kent/scripts/workflow_runtime_contracts.py",
            "kit_fixture_no_helper_runtime",
        )
        redaction_evidence = "synthetic redaction proof passed"
        seal_request = {
            "schema": runtime.TERMINAL_SEAL_REQUEST_SCHEMA,
            "operation_report_digests": [],
            "redaction": {
                "status": "passed",
                "report_sha256": hashlib.sha256(
                    redaction_evidence.encode("utf-8")
                ).hexdigest(),
            },
            "retention_class": "cleanup_report_only",
        }
        sealed = self.command(
            root,
            "workflow-evidence-ledger",
            seal_request,
            "seal",
            "--task",
            task,
            "--workspace",
            str(root),
            environment=cleanup_environment,
        )
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        marker_line = json.loads(sealed.stdout)["terminal_marker"]
        cleanup_report = (
            "Synthetic no-helper Cleanup completed from retained proof.\n"
            + marker_line
        )
        marker = runtime.validate_cleanup_report(cleanup_report)
        ledger = root / f".kent/runtime/{task}/evidence-ledger.jsonl"
        original_ledger = ledger.read_bytes()
        records = [
            json.loads(line)
            for line in original_ledger.decode("utf-8").splitlines()
        ]
        final_record = records[-2]
        original_ids = {
            key: final_record[key]
            for key in ("session_id", "run_id", "step_id")
        }
        retained_source = {
            "task_short_id": task,
            "plan_contract_sha256": source_digest,
            "seal_request": seal_request,
            "cleanup_report": cleanup_report,
            "marker": marker,
            "marker_line": marker_line,
            "operation_reports": [],
            "redaction_evidence": redaction_evidence,
            # Synthetic identity provenance is a fixture input, not Kent
            # authentication.
            "native_provenance": original_ids,
        }
        retained_source_path = retained_directory / "no-helper-cleanup-proof.json"
        retained_source_bytes = runtime.canonical_bytes(retained_source)
        retained_source_path.write_bytes(retained_source_bytes)
        retained_source_path.chmod(0o600)
        proof = json.loads(retained_source_path.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(retained_source_path.read_bytes()).hexdigest(),
            hashlib.sha256(retained_source_bytes).hexdigest(),
        )

        def recover(original_proof):
            required = {
                "task_short_id",
                "plan_contract_sha256",
                "seal_request",
                "cleanup_report",
                "marker",
                "marker_line",
                "operation_reports",
                "redaction_evidence",
                "native_provenance",
            }
            missing = required - set(original_proof)
            if missing:
                raise ValueError("missing original retained Cleanup proof")
            if original_proof["task_short_id"] != task:
                raise ValueError("retained Cleanup proof task identity conflicts")
            if (
                not retained_cache.is_file()
                or hashlib.sha256(retained_cache.read_bytes()).hexdigest()
                != original_proof["plan_contract_sha256"]
            ):
                raise ValueError("retained Plan Contract bytes are missing or conflicting")

            validation = self.command(
                root,
                "workflow-evidence-ledger",
                {},
                "validate",
                "--task",
                task,
                "--workspace",
                str(root),
            )
            if validation.returncode != 0:
                raise ValueError("original evidence ledger does not validate")
            readback = self.command(
                root,
                "workflow-evidence-ledger",
                {},
                "read",
                "--task",
                task,
                "--workspace",
                str(root),
            )
            if readback.returncode != 0:
                raise ValueError("original evidence ledger cannot be read")
            current_records = json.loads(readback.stdout)
            chain_marker = runtime.validate_terminal_chain(
                current_records,
                task_short_id=task,
            )
            request = runtime.validate_terminal_seal_request(
                original_proof["seal_request"]
            )
            report_marker = runtime.validate_cleanup_report(
                original_proof["cleanup_report"]
            )
            retained_marker = runtime.validate_terminal_marker(
                original_proof["marker"]
            )
            actual_report_digests = sorted(
                (
                    {
                        "kind": operation["kind"],
                        "sha256": hashlib.sha256(
                            operation["report"].encode("utf-8")
                        ).hexdigest(),
                    }
                    for operation in original_proof["operation_reports"]
                ),
                key=lambda item: item["kind"],
            )
            if actual_report_digests != request["operation_report_digests"]:
                raise ValueError("retained operation report digests conflict")
            if hashlib.sha256(
                original_proof["redaction_evidence"].encode("utf-8")
            ).hexdigest() != request["redaction"]["report_sha256"]:
                raise ValueError("retained redaction proof digest conflicts")
            request_from_marker = runtime.validate_terminal_seal_request({
                "schema": runtime.TERMINAL_SEAL_REQUEST_SCHEMA,
                "operation_report_digests": chain_marker["operation_report_digests"],
                "redaction": chain_marker["redaction"],
                "retention_class": chain_marker["retention_class"],
            })
            if request != request_from_marker:
                raise ValueError("original frozen request conflicts with seal marker")
            if report_marker != chain_marker or retained_marker != chain_marker:
                raise ValueError("original report or marker conflicts with ledger")
            canonical_marker = runtime.terminal_marker_line(chain_marker)
            if (
                original_proof["marker_line"] != canonical_marker
                or original_proof["cleanup_report"].splitlines()[-1]
                != canonical_marker
            ):
                raise ValueError("original report marker bytes conflict")
            final = current_records[-2]
            current_ids = {
                key: final.get(key)
                for key in ("session_id", "run_id", "step_id")
            }
            if final.get("node_key") != "cleanup":
                raise ValueError("original final ordinary event is not Cleanup")
            if current_ids != original_proof["native_provenance"]:
                raise ValueError("original final-event native provenance conflicts")
            return original_proof["cleanup_report"]

        actions = []
        helper_calls = []
        invoke_command = self.command

        def record_command(root, name, payload, *args, **kwargs):
            if name == "workflow-prepare-cleanup":
                helper_calls.append(name)
            if (
                name == "workflow-evidence-ledger"
                and args
                and args[0] in {"append", "seal"}
            ):
                actions.append(args[0])
            return invoke_command(root, name, payload, *args, **kwargs)

        missing_proof = dict(proof)
        missing_proof.pop("seal_request")
        conflicting_proof = dict(proof)
        conflicting_proof["marker"] = {
            **proof["marker"],
            "final_hash": "f" * 64,
        }
        with mock.patch.object(self, "command", side_effect=record_command):
            with mock.patch.object(
                self,
                "prepare",
                side_effect=AssertionError("no-helper recovery must not invoke helper"),
            ):
                with self.assertRaisesRegex(ValueError, "missing"):
                    recover(missing_proof)
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    recover(conflicting_proof)
                reused_report = recover(proof)
                self.assertEqual(ledger.read_bytes(), original_ledger)
                admitted = self.admission(root, reused_report)

        self.assertEqual(actions, [])
        self.assertEqual(helper_calls, [])
        self.assertTrue(admitted[0], admitted)
        self.assertFalse(ledger.exists())
        self.assertEqual(retained_cache.read_bytes(), source_bytes)
        tombstones = list(
            (root / ".kent/runtime").glob(".evidence-cleanup-*")
        )
        self.assertEqual(len(tombstones), 1)
        retained_ledger = tombstones[0] / "evidence-ledger.jsonl"
        self.assertEqual(retained_ledger.read_bytes(), original_ledger)
        replayed_records = [
            json.loads(line)
            for line in retained_ledger.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            {
                key: replayed_records[-2][key]
                for key in ("session_id", "run_id", "step_id")
            },
            original_ids,
        )
        self.assertEqual(
            runtime.validate_cleanup_report(reused_report),
            runtime.validate_terminal_chain(
                replayed_records,
                task_short_id=task,
            ),
        )


if __name__ == "__main__":
    unittest.main()
