from __future__ import annotations

from dataclasses import replace
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
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
module_spec = importlib.util.spec_from_file_location("kit_development", BUILDER)
assert module_spec is not None and module_spec.loader is not None
kit = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(kit)


class KitDevelopmentWorkflowTest(unittest.TestCase):
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
        base = build_delivery_workflow(profile, 2)
        spec = kit.build_workflow(profile)
        spec.validate()  # Includes context source and parameter topology.
        self.assertEqual(spec.name, "Kit Engineering Delivery v2")
        self.assertEqual(spec.nodes, base.nodes)
        self.assertEqual(len(spec.nodes), 21)
        self.assertEqual(len(spec.edges), 52)
        self.assertEqual(len({
            (edge.source, edge.transition) for edge in spec.edges
        }), 51)
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

    def test_v2_preserves_v1_except_cleanup_prompts_and_candidate_name(self) -> None:
        import hashlib

        historical = ROOT / ".kent/workflows/kit-engineering-delivery-v1.spec.json"
        raw = historical.read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "3f586796ee574682251716f28eb38eb5abf475cb8bc0c2f04003bb0d9d41decc",
        )
        previous = json.loads(raw)
        current = json.loads(kit.rendered_spec())
        self.assertEqual(current["name"], "Kit Engineering Delivery v2")
        changed = []
        for old, new in zip(previous["edges"], current["edges"], strict=True):
            if old["target"] == "cleanup":
                self.assertNotEqual(new["prompt"], old["prompt"])
                changed.append(new["key"])
                new["prompt"] = old["prompt"]
        self.assertEqual(len(changed), 7)
        current["name"] = previous["name"]
        self.assertEqual(current, previous)

    def test_snapshot_is_exact_and_check_is_read_only(self) -> None:
        snapshot = ROOT / kit.SPEC_PATH
        before = snapshot.read_bytes()
        self.assertEqual(before.decode(), kit.rendered_spec())
        self.assertEqual(
            json.loads(before),
            json.loads(json.dumps(spec_as_json(
                kit.build_workflow(kit.profile_at(ROOT)),
            ))),
        )
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--check"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(before, snapshot.read_bytes())

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
    def fixture(self, *, plan: bool = False) -> Path:
        fixture_owner = KitDevelopmentWorkflowTest()
        self.addCleanup(fixture_owner.doCleanups)
        root = fixture_owner.fixture()
        kit.bootstrap(root)
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
                repeated = self.prepare(root, request, run_id="fixture-retry")
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                self.assertEqual(json.loads(repeated.stdout)["cleanup_report"], report)
                self.assertEqual((state / "evidence-ledger.jsonl").read_bytes(), original)
                admitted = self.admission(root, report)
                self.assertTrue(admitted[0], admitted)
                self.assertFalse(state.exists())
                after_tombstone = self.prepare(root, request, run_id="fixture-after-janitor")
                self.assertEqual(after_tombstone.returncode, 0, after_tombstone.stderr)
                self.assertEqual(json.loads(after_tombstone.stdout)["cleanup_report"], report)
                self.assertTrue(self.admission(root, report)[0])
                self.assertTrue(root.exists())  # Admission only; never native Delete.

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
        retry = self.prepare(root, request, run_id="fixture-new-run")
        self.assertEqual(retry.returncode, 0, retry.stderr)
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
                    write(path, raw, replace=replace)
                    if ((boundary == "archive" and path.name.startswith("plan-contract-"))
                            or (boundary == "receipt" and path.name == "cleanup-preparation.json")
                            or (boundary == "seal" and replace)):
                        raise ValueError("injected retained-write boundary")

                with mock.patch.dict(os.environ, {**self.environment, "KENT_RUN_ID": "cleanup-start"}):
                    with mock.patch.object(helper, "write_private", side_effect=fail_after_write):
                        with self.assertRaisesRegex(ValueError, "injected"):
                            helper.prepare(request)
                source = root / ".kent/runtime/TASK-1/plan-contract.json"
                self.assertEqual(source.exists(), boundary != "seal")
                archive = root / f"build/kent-workflow/TASK-1/plan-contract-{request['snapshot_sha256']}.json"
                original = archive.read_bytes()
                retried = self.prepare(root, request, run_id="cleanup-recovery")
                self.assertEqual(retried.returncode, 0, retried.stderr)
                self.assertEqual(archive.read_bytes(), original)
                self.assertTrue(self.admission(root, json.loads(retried.stdout)["cleanup_report"])[0])

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

    def test_real_sealed_plan_snapshot_is_unknown_to_janitor(self) -> None:
        root = self.fixture()
        sealed = self.command(
            root, "workflow-evidence-ledger", self.seal_request,
            "seal", "--task", "TASK-1", "--workspace", str(root),
        )
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        report = "Fixture retained authority\n" + json.loads(sealed.stdout)["terminal_marker"]
        result = self.admission(root, report)
        self.assertFalse(result[0], result)
        self.assertIn("plan-contract.json", result[1])
        self.assertTrue((root / ".kent/runtime/TASK-1/plan-contract.json").exists())


if __name__ == "__main__":
    unittest.main()
