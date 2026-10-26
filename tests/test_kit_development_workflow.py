from __future__ import annotations

from dataclasses import replace
import hashlib
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
import uuid
from unittest import mock

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
    def test_prompt_improvement_contracts_preserve_safety_boundaries(self) -> None:
        expectations = {
            "prompts/review-change.md": (
                "agents/standards-reviewer.md", "agents/spec-reviewer.md",
                "first read the installed `kent-engineering-kit` skill's `SKILL.md`",
                "block only the dependent action",
                "Session-control permissions", "same cause and remedy",
                "each axis's result", "relevant guards and callers",
            ),
            "agents/spec-reviewer.md": (
                "only this node's own required evidence",
                "qualified configured evidence adapter",
                "genuine current Task/Node/Session/Step identity",
                "Do not edit sources or checkpoints",
                "write the ledger directly",
                "append for another producer",
                "seal or recover",
                "other shell writes",
            ),
            "agents/standards-reviewer.md": (
                "trigger, affected behavior", "relevant guards and callers",
                "non-blocking preferences", "no confidence scores",
                "only this node's own required evidence",
                "qualified configured evidence adapter",
                "genuine current Task/Node/Session/Step identity",
                "Do not edit sources or checkpoints",
                "write the ledger directly",
                "append for another producer",
                "seal or recover",
                "other shell writes",
            ),
            "agents/compliance_reviewer.md": (
                "only this node's own required evidence",
                "qualified configured evidence adapter",
                "genuine current Task/Node/Session/Step identity",
                "Do not edit sources or checkpoints",
                "write the ledger directly",
                "append for another producer",
                "seal or recover",
                "other shell writes",
            ),
            "agents/workflow-gate.md": (
                "existing artifact through the qualified reader/reconstruction route",
                "truncated carrier", "freshness",
                "Recovery cannot turn an old BLOCKED report into PASS",
                "solely to decide eligibility for `retry_verification`",
                "it does not declare the blocked cycle successful",
                "settlement of previously owned processes/effects",
                "every enabled deterministic, Standards and Specification branch",
                "do not rewrite opaque packets",
                "only when the current graph declares that route",
                "report that no supported retry route is declared",
                "only this node's own required evidence",
                "qualified configured evidence adapter",
                "genuine current Task/Node/Session/Step identity",
                "Do not edit sources or checkpoints",
                "write the ledger directly",
                "append for another producer",
                "seal or recover",
                "other shell writes",
            ),
            "agents/runtime-smoke-tester.md": (
                "Emit the approval-gated `smoke_wont_do` outcome only when the current graph declares that route",
                "no supported cancellation route is declared",
                "do not invent an outcome, change the graph or move the Task directly to Cleanup",
                "safety-only continuation",
                "cancellation source and settlement/qualified-retention references",
                "Do not claim a native approval",
            ),
            "agents/ci-monitor.md": (
                "first read the installed `kent-engineering-kit` skill's `SKILL.md`",
                "block only the dependent action",
                "complete transitive dependent-job closure",
                "source-qualified job",
                "effect-label set",
                "do not infer effects from job names",
                "Do not invent a rerun endpoint",
                "only this node's own required evidence",
                "qualified configured evidence adapter",
                "genuine current Task/Node/Session/Step identity",
                "Do not edit sources or checkpoints",
                "write the ledger directly",
                "append for another producer",
                "seal or recover",
                "other shell writes",
            ),
            "agents/researcher.md": (
                "Only when assigned as a Workflow review producer",
                "ordinary research remains inspection-only",
                "only this node's own required evidence",
                "qualified configured evidence adapter",
                "genuine current Task/Node/Session/Step identity",
                "Do not edit sources or checkpoints",
                "write the ledger directly",
                "append for another producer",
                "seal or recover",
                "other shell writes",
            ),
            "agents/fix-worker.md": (
                "For Source-Fix, own source repair, reproducer and checkpoint duties",
                "When assigned to Evidence Repair",
                "only its named packaging artifacts",
                "only its approved packaging checks",
                "This role remains eligible for Evidence Repair",
                "Do not perform Source-Fix repairs or reproducer/checkpoint duties in that node",
                "does not create or update a Fix checkpoint",
                "metadata correction creates no Fix checkpoint",
                "final edited state", "original valid reproducer",
                "Reuse an existing failing test", "unavailable check remains unverified",
            ),
            "agents/delivery-operator.md": (
                "first read the installed `kent-engineering-kit` skill's `SKILL.md`",
                "block only the dependent action",
            ),
            "agents/build-doctor.md": (
                "exact authorized reproducer", "bounded read-only diagnosis",
                "cause unconfirmed", "grant no installation",
            ),
            "prompts/tdd-slice.md": (
                "not an unrelated setup", "missing new public symbol",
                "without weakening", "existing affected tests", "final edited state",
            ),
            "contracts/plan-contract.md": (
                "decisions that cannot be inferred", "required exact values",
                "verification conditions", "original authority",
                "not as an implementation transcript",
            ),
            "agents/task-supervisor.md": (
                "first remaining action or exact awaited change",
                "unknown effects", "owner/cleanup duties", "fresh pre-effect readbacks",
            ),
            "prompts/migrate-tasks.md": (
                "first remaining action or exact awaited change", "link completed proof",
                "unresolved effects", "owner/cleanup duties", "fresh pre-effect readbacks",
            ),
        }
        # These are instruction-contract checks, not a model-performance benchmark.
        for path, phrases in expectations.items():
            text = " ".join((ROOT / path).read_text().split())
            for phrase in phrases:
                with self.subTest(path=path, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_operations_project_contract_fragments_exist(self) -> None:
        operations = (ROOT / "docs/OPERATIONS.md").read_text()
        contract = (ROOT / ".kent/project-contract.md").read_text()
        fragments = re.findall(
            r"\]\(\.\./\.kent/project-contract\.md#([a-z0-9-]+)\)", operations,
        )
        self.assertTrue(fragments)
        headings = {
            title.lower().replace(" ", "-")
            for title in re.findall(r"^## (.+)$", contract, re.MULTILINE)
        }
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, headings)

    def test_current_source_snapshot_is_explicitly_unactivated(self) -> None:
        source = json.loads(kit.rendered_current_spec())
        ingress = [edge for edge in source["edges"]
                   if edge["source"] == "backlog" and edge["target"] == "plan"]
        self.assertEqual(len(ingress), 1)
        self.assertIn("UNACTIVATED SOURCE CANDIDATE", ingress[0]["prompt"])
        self.assertNotIn("KENT_SOURCE_ADMISSION_V1_BEGIN", json.dumps(source))
        self.assertNotIn("receipt_sha256", json.dumps(source))

    def test_published_current_builder_rejects_raw_admission_before_render(self) -> None:
        with mock.patch.object(kit, "build_current_workflow") as render:
            with self.assertRaisesRegex(SpecError, "VerifiedSourceAdmission"):
                kit.build_published_current_workflow(kit.profile_at(ROOT), {"approved": True})
            render.assert_not_called()

    def test_published_current_builder_uses_genuine_verified_admission(self) -> None:
        from tests.test_canonical_workflow_lifecycle import create_verified_source_fixture

        fixture, admitted = create_verified_source_fixture(
            self, "project-135413f9-2454-4d04-969e-3831b1527f18",
            "4e7cea43-54f5-4662-8d5d-c8db3d816c74",
        )
        # Admission uses real disposable source/native interfaces. Rendering
        # uses the Kit profile; this is not installed-project qualification.
        profile = replace(kit.profile_at(ROOT), project_root=fixture.project)
        with mock.patch.object(kit, "ROOT", fixture.kit):
            rendered = kit.build_published_current_workflow(profile, admitted)
            ingress = [edge for edge in rendered.edges
                       if edge.source == "backlog" and edge.target == "plan"]
            self.assertEqual(len(ingress), 1)
            self.assertTrue(ingress[0].prompt.startswith(admitted.gate + "\n\n"))
            self.assertNotIn("UNACTIVATED SOURCE CANDIDATE", ingress[0].prompt)
            with self.assertRaisesRegex(SpecError, "different Kit deployment"):
                kit.build_published_current_workflow(
                    replace(profile, project_root=fixture.kit), admitted)

    def test_r2_final_builder_rejects_plan_publication_before_source_render(self):
        from tests.test_canonical_workflow_lifecycle import (
            create_verified_source_fixture, synthetic_verified_source_admission,
        )
        fixture, admitted = create_verified_source_fixture(
            self, "project-135413f9-2454-4d04-969e-3831b1527f18",
            "4e7cea43-54f5-4662-8d5d-c8db3d816c74",
        )
        value = admitted.value
        value["schema"] = "delivery-source-admission-v2"
        value.pop("installation_binding_sha256")
        value["binding"] = {"kind": "plan-publication"}
        value["ingress"] = {"kind": "agent-edge", "edge_id": "entry"}
        source_only = synthetic_verified_source_admission(
            value, project_id=admitted.project_id, workflow_id=admitted.workflow_id,
            project_root=fixture.project, kent_path=fixture.native_bin,
        )
        with mock.patch.object(kit, "build_current_workflow") as render:
            with self.assertRaisesRegex(SpecError, "r2-installed"):
                kit.build_published_current_workflow(kit.profile_at(ROOT), source_only)
            render.assert_not_called()


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
            *kit.SCHEMA3_COPIES, *kit.PROJECT_COPIES, *kit.MEMORY_COPIES,
            "compile_verify", "prepare_cleanup",
        })
        self.assertEqual(set(profile.context_manifests), {
            "plan", "implement", "review", "delivery", "smoke",
        })
        for kind in profile.work_kinds.values():
            self.assertEqual(kind.plan, ".kent/commands/plan.md")
            self.assertEqual(kind.implement, ".kent/commands/implement.md")
        for path in profile.procedures.values():
            self.assertTrue((ROOT / path).is_file())
        root = self.fixture()
        kit.bootstrap(root)
        kit.verify_command_closure(root)


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

    def test_current_builder_under_long_contained_tmpdir(self) -> None:
        self.run_actual_method_with_contained_tmpdir(
            "tests.test_kit_delivery_slice.CurrentKitBootstrapTest."
            "test_current_cli_materialization_parity_and_stale_failure_are_source_only",
        )


class CleanupPreparationTest(unittest.TestCase):
    def test_r2_retention_uses_real_adapter_and_preserves_external_acceptance(self):
        from tests.test_delivery_source_admission import (
            DeliverySourceAdmissionTests,
            accept_r2_plan,
            branch_r2_plan,
            install_r2_lifecycle_sources,
            run_r2_cleanup,
        )

        owner = DeliverySourceAdmissionTests(
            "test_published_r2_acceptance_reaches_real_branch_selection_without_cache",
        )
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        install_r2_lifecycle_sources(owner, coder_selection="complexity")
        accepted = accept_r2_plan(owner)
        branch = branch_r2_plan(owner, accepted)
        self.assertEqual(branch["transition"], "branch_identity_ready", branch)
        self.assertEqual(
            accepted["accepted_snapshot"]["authority_refs"],
            ["question_answer(fixture-session/approved-plan)"],
        )

        result, _ = run_r2_cleanup(owner, accepted, branch_result=branch)

        self.assertEqual(result.returncode, 0, result.stderr)
        completed = json.loads(result.stdout)
        self.assertIn("TERMINAL_EVIDENCE_V1", completed["cleanup_report"])
        self.assertIn("retained_bundle_path", completed)
        self.assertFalse(accepted["cache_path"].exists())
        archive = (
            owner.project
            / f"build/kent-workflow/OSM-130/plan-contract-"
            f"{hashlib.sha256(accepted['cache_raw']).hexdigest()}.json"
        )
        self.assertEqual(archive.read_bytes(), accepted["cache_raw"])
        self.assertEqual(
            accepted["accepted_path"].read_bytes(), accepted["accepted_raw"],
        )
        self.assertFalse((owner.project / ".todo").exists())

        retained_bytes = accepted["accepted_path"].read_bytes()
        shutil.rmtree(owner.project)
        self.assertEqual(accepted["accepted_path"].read_bytes(), retained_bytes)

    def test_r2_cleanup_accepts_absent_local_cache_and_keeps_report_only_snapshot(self):
        from tests.test_delivery_source_admission import (
            DeliverySourceAdmissionTests,
            accept_r2_plan,
            install_r2_lifecycle_sources,
            run_r2_cleanup,
        )

        owner = DeliverySourceAdmissionTests(
            "test_published_r2_acceptance_reaches_real_branch_selection_without_cache",
        )
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        install_r2_lifecycle_sources(owner, coder_selection="disabled")
        accepted = accept_r2_plan(owner, report_only=True)
        self.assertEqual(
            accepted["accepted_snapshot"]["plan_kind"], "report_only",
        )
        result, _ = run_r2_cleanup(owner, accepted, remove_cache=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(accepted["cache_path"].exists())
        self.assertEqual(
            accepted["accepted_path"].read_bytes(), accepted["accepted_raw"],
        )
        shutil.rmtree(owner.project)
        self.assertEqual(accepted["accepted_path"].read_bytes(), accepted["accepted_raw"])

    def test_r2_cleanup_rejects_symlinked_local_cache(self):
        from tests.test_delivery_source_admission import (
            DeliverySourceAdmissionTests,
            accept_r2_plan,
            install_r2_lifecycle_sources,
            run_r2_cleanup,
        )

        owner = DeliverySourceAdmissionTests(
            "test_published_r2_acceptance_reaches_real_branch_selection_without_cache",
        )
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        install_r2_lifecycle_sources(owner, coder_selection="disabled")
        accepted = accept_r2_plan(owner, report_only=True)
        result, _ = run_r2_cleanup(owner, accepted, symlink_cache=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink is not admitted", result.stderr)
        self.assertTrue(accepted["cache_path"].is_symlink())
        self.assertEqual(
            accepted["accepted_path"].read_bytes(), accepted["accepted_raw"],
        )
        self.assertFalse(
            (owner.project / "build/kent-workflow/OSM-130/cleanup-preparation.json").exists(),
        )

    def test_legacy_cleanup_rejects_absent_local_snapshot(self):
        root = self.fixture()
        request = self.request(root)
        snapshot = root / ".kent/runtime/TASK-1/plan-contract.json"
        snapshot.unlink()

        result = self.prepare(root, request)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No such file", result.stderr)
        self.assertFalse(
            (root / "build/kent-workflow/TASK-1/cleanup-preparation.json").exists(),
        )

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
        # These retained cleanup scenarios exercise the legacy snapshot protocol.
        # Native R2 admission is qualified separately with published source fixtures.
        profile_path = root / ".kent/workflow-profile.toml"
        profile_path.write_text(re.sub(
            r"(?ms)^\[project_memory\]\n.*?(?=^\[|\Z)", "",
            profile_path.read_text(),
        ).replace('delivery_reliability = "v1"\n', ""))
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
            **os.environ, "KENT_SESSION_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-session")),
            "KENT_RUN_ID": "fixture-plan",
            "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-plan")),
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
            environment={**self.environment, "KENT_RUN_ID": run_id,
                         "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, run_id))},
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
                    "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-ci-archive")),
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
                            "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-retry")),
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
                    for key in ("session_id", "step_id")
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
                        for key in ("session_id", "step_id")
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

    def test_final_event_session_step_dedup_cannot_suppress_unrelated_event(self) -> None:
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

        with mock.patch.dict(os.environ, {
            **self.environment, "KENT_RUN_ID": "stale-ignored",
            "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-cleanup")),
        }):
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
            {**self.environment, "KENT_RUN_ID": "stale-ignored",
             "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-new-step"))},
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

    def test_historical_cleanup_v1_final_event_reused_after_upgrade(self):
        from workflowkit import runtime

        for completed in (False, True):
            with self.subTest(completed=completed):
                root = self.fixture()
                request = self.request(root)
                helper = self.helper(root)
                invoke = helper.invoke_ledger
                path = root / ".kent/runtime/TASK-1/evidence-ledger.jsonl"

                def historical_writer(root, task, action, payload, environment):
                    if action == "seal" and not completed:
                        raise ValueError("historical crash before seal")
                    result = invoke(root, task, action, payload, environment)
                    if action == "append":
                        lines = path.read_bytes().splitlines(keepends=True)
                        final = json.loads(lines[-1])
                        final.update(schema_version=1, run_id="33333333-3333-4333-8333-333333333333")
                        final["event_hash"] = runtime._event_hash(
                            {key: value for key, value in final.items() if key != "event_hash"})
                        path.write_bytes(b"".join(lines[:-1]) + runtime.canonical_bytes(final) + b"\n")
                    return result

                with mock.patch.dict(os.environ, {
                    **self.environment,
                    "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "historical-cleanup")),
                }), mock.patch.object(helper, "invoke_ledger", side_effect=historical_writer):
                    if completed:
                        original_report = helper.prepare(request)["cleanup_report"]
                    else:
                        with self.assertRaisesRegex(ValueError, "historical crash"):
                            helper.prepare(request)
                original = path.read_bytes()
                final = json.loads(original.splitlines()[-2 if completed else -1])
                self.assertEqual(final["schema_version"], 1)
                actions = []

                def record(root, task, action, payload, environment):
                    actions.append(action)
                    return invoke(root, task, action, payload, environment)

                environment = {**self.environment,
                               "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "after-upgrade"))}
                environment.pop("KENT_RUN_ID", None)
                with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                    helper, "invoke_ledger", side_effect=record,
                ):
                    retry = helper.prepare(request)
                self.assertNotIn("append", actions)
                self.assertEqual(json.loads(path.read_bytes().splitlines()[-2]), final)
                if completed:
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(retry["cleanup_report"], original_report)
                else:
                    self.assertTrue(path.read_bytes().startswith(original))
                self.assertTrue(self.admission(root, retry["cleanup_report"])[0])

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

                with mock.patch.dict(os.environ, {
                    **self.environment, "KENT_RUN_ID": "stale-ignored",
                    "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "cleanup-start")),
                }):
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
                        for key in ("session_id", "step_id")
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
                                "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "cleanup-recovery")),
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
                            for key in ("session_id", "step_id")
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
        self.assertIn("unknown_runtime_entry", result[1])
        self.assertNotIn("plan-contract.json", result[1])
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
            "KENT_STEP_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-no-helper-step")),
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
            for key in ("session_id", "step_id")
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
                for key in ("session_id", "step_id")
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
                for key in ("session_id", "step_id")
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
