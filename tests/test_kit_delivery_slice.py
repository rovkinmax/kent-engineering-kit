from __future__ import annotations

from dataclasses import replace
import importlib.util
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

from workflowkit.model import SpecError


ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / ".kent/scripts/workflow-compile-verify"
WRAPPER = ROOT / ".kent/scripts/workflow-verify-report"
RUNTIME = ROOT / ".kent/scripts/workflow_runtime_contracts.py"
IDENTITY = ROOT / ".kent/scripts/kit-verification-identity.py"
BUILDER = ROOT / ".kent/workflows/kit_development.py"
kit_spec = importlib.util.spec_from_file_location("kit_delivery_slice", BUILDER)
assert kit_spec is not None and kit_spec.loader is not None
kit = importlib.util.module_from_spec(kit_spec)
kit_spec.loader.exec_module(kit)


class IdentityFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("fixture_identity", IDENTITY)
        self.identity = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.identity)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()


class ToolIdentityStabilityTest(IdentityFixtureTest):
    def tool(self, action: str = "") -> Path:
        path = self.root / "tool"
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text("#!/bin/sh\n" + action + "\necho fixture-version\n")
        path.chmod(0o700)
        return path

    def test_stable_tool_has_repeatable_identity(self) -> None:
        tool = self.tool()
        self.assertEqual(
            self.identity._tool_record(tool), self.identity._tool_record(tool)
        )

    def test_tool_mutation_during_version_is_rejected(self) -> None:
        actions = {
            "bytes": 'echo "# changed" >> "$0"',
            "inode": 'cp "$0" "$0.new"; mv "$0.new" "$0"',
            "mode": 'chmod 500 "$0"',
            "missing": 'rm "$0"',
            "symlink": 'cp "$0" "$0.target"; rm "$0"; ln -s "$0.target" "$0"',
            "restored_bytes": (
                'cp "$0" "$0.saved"; echo "# changed" >> "$0"; '
                'cat "$0.saved" > "$0"'
            ),
        }
        for name, action in actions.items():
            with self.subTest(mutation=name):
                tool = self.tool(action)
                with self.assertRaises(self.identity.IdentityError):
                    self.identity._tool_record(tool)
                if tool.is_symlink():
                    tool.unlink()

    def test_tool_mutation_during_hash_is_rejected(self) -> None:
        for hash_call in (1, 2):
            with self.subTest(hash_call=hash_call):
                tool = self.tool()
                original = self.identity._hash_file
                calls = 0

                def mutate_after_hash(path, *, limit):
                    nonlocal calls
                    result = original(path, limit=limit)
                    calls += 1
                    if calls == hash_call:
                        path.chmod(0o500)
                    return result

                with mock.patch.object(
                    self.identity, "_hash_file", side_effect=mutate_after_hash
                ):
                    with self.assertRaises(self.identity.IdentityError):
                        self.identity._tool_record(tool)

    def test_oversized_version_output_is_rejected(self) -> None:
        for redirect in ("", " >&2"):
            with self.subTest(redirect=redirect):
                tool = self.tool(
                    "i=0; while [ $i -lt 500 ]; do echo 1234567890"
                    + redirect + "; i=$((i+1)); done"
                )
                with self.assertRaises(self.identity.IdentityError):
                    self.identity._tool_record(tool)

    def test_version_timeout_is_rejected(self) -> None:
        tool = self.tool("while :; do :; done")
        with mock.patch.object(self.identity, "VERSION_TIMEOUT_SECONDS", 0.1):
            with self.assertRaises(self.identity.IdentityError):
                self.identity._tool_record(tool)

    def test_rehash_detects_bytes_even_with_equal_metadata(self) -> None:
        tool = self.tool()
        metadata = self.identity._tool_metadata(tool)

        def mutate_version(path):
            path.write_text("#!/bin/sh\necho changed\n")
            return b"fixture-version", 0

        with (
            mock.patch.object(self.identity, "_tool_metadata", return_value=metadata),
            mock.patch.object(self.identity, "_tool_version", side_effect=mutate_version),
        ):
            with self.assertRaisesRegex(self.identity.IdentityError, "after version"):
                self.identity._tool_record(tool)


class TmpdirIdentityContractTest(IdentityFixtureTest):
    def test_invalid_tmpdir_never_returns_reusable_identity(self) -> None:
        workflow_root = self.root / "build/kent-workflow"
        workflow_root.mkdir(parents=True)
        public = workflow_root / ".verify-tmp-public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        regular = workflow_root / ".verify-tmp-file"
        regular.write_text("not a directory")
        loop = workflow_root / ".verify-tmp-loop"
        loop.symlink_to(loop.name)
        for value in (
            None, "", str(self.root), str(self.root / "missing"),
            str(public), str(regular), str(loop),
        ):
            with self.subTest(value=value), mock.patch.dict(os.environ):
                if value is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = value
                for _ in range(2):
                    with self.assertRaises(self.identity.IdentityError):
                        self.identity._tmpdir_contract(self.root)

    def test_valid_private_tmpdir_basename_is_not_identity(self) -> None:
        records = []
        for name in ("first", "second"):
            path = self.root / f"build/kent-workflow/.verify-tmp-{name}"
            path.mkdir(parents=True, mode=0o700)
            with mock.patch.dict(os.environ, {"TMPDIR": str(path)}):
                records.append(self.identity._tmpdir_contract(self.root))
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[0]["state"], "contained-private")

    def test_external_workflow_root_symlink_is_not_containment(self) -> None:
        outside = self.root / "outside"
        path = outside / ".verify-tmp-private"
        path.mkdir(parents=True, mode=0o700)
        workspace = self.root / "workspace"
        (workspace / "build").mkdir(parents=True)
        (workspace / "build/kent-workflow").symlink_to(outside, target_is_directory=True)
        with mock.patch.dict(os.environ, {"TMPDIR": str(path)}):
            with self.assertRaises(self.identity.IdentityError):
                self.identity._tmpdir_contract(workspace)

    def test_wrong_owner_is_not_a_private_tmpdir_contract(self) -> None:
        path = self.root / "build/kent-workflow/.verify-tmp-private"
        path.mkdir(parents=True, mode=0o700)
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(path)}),
            mock.patch.object(self.identity.os, "getuid", return_value=path.stat().st_uid + 1),
        ):
            with self.assertRaises(self.identity.IdentityError):
                self.identity._tmpdir_contract(self.root)

    def test_tmpdir_mode_must_match_wrapper_private_contract(self) -> None:
        path = self.root / "build/kent-workflow/.verify-tmp-mode"
        path.mkdir(parents=True, mode=0o700)
        self.addCleanup(path.chmod, 0o700)
        for mode in (0o500, 0o600, 0o1700):
            with self.subTest(mode=oct(mode)):
                path.chmod(mode)
                with mock.patch.dict(os.environ, {"TMPDIR": str(path)}):
                    with self.assertRaises(self.identity.IdentityError):
                        self.identity._tmpdir_contract(self.root)


class DeliveryBaselineFixtureTest(unittest.TestCase):
    def test_focused_identity_then_wrapper_invokes_validator_once(self):
        root = self.fixture()
        identity = self.run_identity(root)
        self.assertEqual(identity.returncode, 0, identity.stderr)
        self.assertFalse((root / ".fixture-validation-count").exists())
        wrapped = self.run_wrapper(root)
        self.assertEqual(wrapped.returncode, 0, wrapped.stderr)
        payload = json.loads(wrapped.stdout)
        self.assertEqual(payload["verification_status"], "passed")
        report = json.loads(payload["verification_report"])
        log = (root / report["log_path"]).read_text()
        identities = [
            json.loads(line.split(" ", 2)[2]) for line in log.splitlines()
            if line.startswith("KENT_VERIFICATION_IDENTITY ")
        ]
        self.assertEqual(len(identities), 2, log)
        self.assertEqual(identities[0], identities[1])
        self.assertEqual((root / ".fixture-validation-count").read_text().strip(), "1")

    def private_tmpdir(self, root: Path) -> str:
        path = root / "build/kent-workflow/.verify-tmp-fixture"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return str(path)

    def fixture(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "scripts").mkdir()
        validator = root / "scripts/validate"
        validator.write_text(
            "#!/bin/bash\n"
            "set -eu\n"
            'count=0\n'
            '[[ ! -f .fixture-validation-count ]] || '
            'count=$(<.fixture-validation-count)\n'
            'printf "%s\\n" "$((count + 1))" > .fixture-validation-count\n'
            'printf "fixture-validation-log\\n"\n',
        )
        validator.chmod(0o755)
        (root / ".gitignore").write_text(
            "/.fixture-validation-count\n/build/kent-workflow/\n",
        )
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        scripts = root / ".kent/scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(CHILD, scripts / CHILD.name)
        shutil.copy2(WRAPPER, scripts / WRAPPER.name)
        shutil.copy2(RUNTIME, scripts / RUNTIME.name)
        shutil.copy2(IDENTITY, scripts / IDENTITY.name)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
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

    def run_child(
        self, root: Path, *, overrides: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.pop("KENT_ENGINEERING_KIT_PYTHON", None)
        environment["TMPDIR"] = self.private_tmpdir(root)
        environment.update(overrides or {})
        return subprocess.run(
            [str(root / ".kent/scripts/workflow-compile-verify")],
            cwd=root,
            input="{}",
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def run_wrapper(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(root / ".kent/scripts/workflow-verify-report")],
            cwd=root,
            input=json.dumps({"workspace_path": str(root)}),
            text=True,
            capture_output=True,
            check=False,
        )

    def run_identity(
        self,
        root: Path,
        *,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        child_environment = dict(os.environ)
        child_environment["TMPDIR"] = self.private_tmpdir(root)
        if environment:
            child_environment.update(environment)
        return subprocess.run(
            [
                sys.executable,
                str(root / ".kent/scripts/kit-verification-identity.py"),
                "--workspace",
                str(root),
                "--python-executable",
                sys.executable,
            ],
            cwd=root,
            env=child_environment,
            capture_output=True,
            text=True,
            check=False,
        )


    def test_compile_output_has_matching_closed_identity_lines(self) -> None:
        root = self.fixture()
        result = self.run_child(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [
            line for line in result.stderr.splitlines()
            if line.startswith("KENT_VERIFICATION_IDENTITY ")
        ]
        self.assertEqual(len(lines), 2, result.stderr)
        before = json.loads(lines[0].split(" ", 2)[2])
        after = json.loads(lines[1].split(" ", 2)[2])
        self.assertEqual(before, after)
        self.assertEqual(
            set(before),
            {"schema", "source_sha256", "environment_sha256", "head", "workspace_path"},
        )
        self.assertEqual(before["schema"], "kit-verification-identity-v1")
        self.assertRegex(before["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(before["environment_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(before["head"], r"^[0-9a-f]{40}$")
        self.assertEqual(before["workspace_path"], str(root))

    def test_invalid_tmpdir_blocks_identity_and_compile_child(self) -> None:
        root = self.fixture()
        for value in (str(root), str(root / "missing")):
            with self.subTest(tmpdir=value):
                for _ in range(2):
                    identity = self.run_identity(root, environment={"TMPDIR": value})
                    self.assertNotEqual(identity.returncode, 0)
                    self.assertEqual(identity.stdout, "")
                    self.assertIn("TMPDIR", identity.stderr)
                child = self.run_child(root, overrides={"TMPDIR": value})
                self.assertEqual(json.loads(child.stdout), {"transition": "blocked"})
                self.assertFalse((root / ".fixture-validation-count").exists())

    def test_mutating_tool_blocks_identity_and_compile_child(self) -> None:
        root = self.fixture()
        tools = root / "build/kent-workflow/tools"
        tools.mkdir(parents=True)
        ruby = tools / "ruby"
        ruby.write_text('#!/bin/sh\necho "# mutation" >> "$0"\necho fixture-ruby\n')
        ruby.chmod(0o700)
        overrides = {"PATH": str(tools) + os.pathsep + os.environ["PATH"]}
        for _ in range(2):
            identity = self.run_identity(root, environment=overrides)
            self.assertNotEqual(identity.returncode, 0)
            self.assertEqual(identity.stdout, "")
            self.assertIn("tool changed", identity.stderr)
        python = tools / "python3"
        python.symlink_to(sys.executable)
        overrides["KENT_ENGINEERING_KIT_PYTHON"] = str(python)
        child = self.run_child(root, overrides=overrides)
        self.assertEqual(json.loads(child.stdout), {"transition": "blocked"})
        self.assertFalse((root / ".fixture-validation-count").exists())

    def test_child_tmpdir_drift_blocks_pass_but_preserves_failure(self) -> None:
        for exit_code, expected in ((0, "blocked"), (7, "failed")):
            with self.subTest(exit_code=exit_code):
                root = self.fixture()
                validator = root / "scripts/validate"
                validator.write_text(
                    '#!/bin/bash\nchmod 755 "$TMPDIR"\n'
                    f"exit {exit_code}\n"
                )
                child = self.run_child(root)
                self.assertEqual(json.loads(child.stdout), {"transition": expected})
                self.assertIn("TMPDIR is not a contained private directory", child.stderr)
                self.assertEqual(child.stderr.count("KENT_VERIFICATION_IDENTITY "), 1)

    def test_real_wrapper_rejects_tmpdir_drift_without_reusable_report(self) -> None:
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                root = self.fixture()
                validator = root / "scripts/validate"
                validator.write_text(
                    '#!/bin/bash\nchmod 755 "$TMPDIR"\n'
                    f"exit {exit_code}\n"
                )
                wrapped = self.run_wrapper(root)
                self.assertEqual(wrapped.returncode, 0, wrapped.stderr)
                payload = json.loads(wrapped.stdout)
                self.assertEqual(payload["verification_status"], "blocked", wrapped.stdout)
                report = json.loads(payload["verification_report"])
                self.assertEqual(report["code"], "log_path_unsafe")
                self.assertIsNone(report["log_path"])
                self.assertIsNone(report["log_sha256"])

    def test_source_mutation_during_validation_blocks_pass(self) -> None:
        root = self.fixture()
        validator = root / "scripts/validate"
        validator.write_text(
            "#!/bin/bash\n"
            "set -eu\n"
            'printf "new source\\n" > generated-source.txt\n',
        )
        validator.chmod(0o755)
        result = self.run_child(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"transition": "blocked"})
        self.assertIn("identity changed during validation", result.stderr)

    def test_identity_environment_change_changes_environment_digest(self) -> None:
        root = self.fixture()
        first = self.run_identity(root)
        changed = self.run_identity(root, environment={"CI": "ken-5-mutated"})
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        first_identity = json.loads(first.stdout)
        changed_identity = json.loads(changed.stdout)
        self.assertEqual(first_identity["source_sha256"], changed_identity["source_sha256"])
        self.assertNotEqual(
            first_identity["environment_sha256"],
            changed_identity["environment_sha256"],
        )

    def test_external_source_symlink_blocks_identity(self) -> None:
        root = self.fixture()
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        target = outside / "target.txt"
        target.write_text("outside\n")
        (root / "external-link").symlink_to(target)
        result = self.run_child(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"transition": "blocked"})
        self.assertIn("escapes workspace", result.stderr)


class CurrentKitGraphTest(unittest.TestCase):
    def test_current_graph_modes_and_retained_replan_contract(self):
        for complexity in (False, True):
            with self.subTest(complexity=complexity):
                graph = kit.build_current_workflow(self.profile(complexity=complexity))
                graph.validate()
                edges = {edge.key: edge for edge in graph.edges}
                self.assertEqual(graph.execution_target, "default-branch")
                self.assertEqual(
                    (len(graph.nodes),
                     len({(edge.source, edge.transition) for edge in graph.edges}),
                     len(graph.edges)),
                    (21, 60, 61) if complexity else (21, 59, 60),
                )
                route = edges["implement_replan"]
                self.assertEqual(
                    (route.source, route.target, route.context, route.context_source),
                    ("implement", "plan_revalidation", "continue_session", "node:plan"),
                )
                self.assertEqual(route.parameters, edges["fix_replan"].parameters)
                self.assertFalse(route.requires_approval)
                self.assertTrue(edges["plan_review_accept"].requires_approval)
                self.assertFalse(edges["prepare_pr_fix"].requires_approval)
                self.assertFalse(edges["waiting_pr_fix"].requires_approval)
                pr_target_fields = {
                    "pr_url",
                    "branch_name",
                    "pr_report",
                    "merge_report",
                    "pr_feedback_cursor",
                    "pr_head_oid",
                    "pr_base_oid",
                    "ci_contract",
                    "ci_report",
                    "publication_report",
                }
                for edge in graph.edges:
                    fields = {parameter.key for parameter in edge.parameters}
                    if edge.key != "prepare_pr_no_pr" and fields & pr_target_fields:
                        with self.subTest(complexity=complexity, edge=edge.key):
                            self.assertIn("pr_base_branch", fields)
                self.assertNotIn(
                    "pr_base_branch",
                    {parameter.key for parameter in edges["prepare_pr_no_pr"].parameters},
                )
                self.assertNotIn(
                    "ci_monitor_retry_preparation",
                    {edge.key for edge in graph.edges},
                )
                self.assertNotIn(
                    "ci_monitor_preparation_needs_user_action",
                    {edge.key for edge in graph.edges},
                )
                for node in graph.nodes:
                    if node.kind == "agent":
                        self.assertEqual(node.completion_mode, "shell_command")

    def profile(self, *, complexity: bool = False):
        profile = kit.profile_at(ROOT, check_files=False)
        policies = dict(profile.policies)
        policies["coder_selection"] = "complexity" if complexity else "disabled"
        return replace(
            profile, policies=policies,
            commands=dict(kit.CURRENT_COMMAND_TARGETS),
        )

    def test_current_requires_opt_in_complete_commands_and_continuous_writer(self):
        current = self.profile()
        for invalid in (
            replace(current, policies={
                key: value for key, value in current.policies.items()
                if key != "delivery_reliability"
            }),
            replace(current, commands={
                key: value for key, value in current.commands.items()
                if key not in kit.MEMORY_COPIES
            }),
            replace(current, policies={**current.policies, "delivery_reliability": "v2"}),
            replace(current, policies={**current.policies, "writer_sessions": "fresh_per_slice"}),
        ):
            with self.subTest(policies=invalid.policies), self.assertRaises(SpecError):
                kit.build_current_workflow(invalid)


class CurrentKitBootstrapTest(unittest.TestCase):
    def fixture(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        for directory in (".kent/context", ".kent/commands", "workflowkit"):
            shutil.copytree(
                ROOT / directory, root / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        (root / ".kent/workflows").mkdir()
        shutil.copy2(BUILDER, root / ".kent/workflows/kit_development.py")
        (root / ".kent/scripts").mkdir()
        for command in ("workflow-compile-verify", "workflow-prepare-cleanup"):
            shutil.copy2(ROOT / ".kent/scripts" / command,
                         root / ".kent/scripts" / command)
        for source in (kit.SCHEMA3_COPIES | kit.PROJECT_COPIES | kit.MEMORY_COPIES).values():
            target = root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / source, target)
        (root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/sync-project-adapters",
                     root / "scripts/sync-project-adapters")
        profile = (ROOT / ".kent/workflow-profile.toml").read_text()
        profile = re.sub(r"(?ms)^\[project_memory\]\n.*?(?=^\[|\Z)", "", profile)
        (root / ".kent/workflow-profile.toml").write_text(profile)
        return root

    def run_builder(self, root: Path, *arguments: str):
        return subprocess.run(
            [sys.executable, "-B", str(root / ".kent/workflows/kit_development.py"),
             *arguments],
            cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, check=False,
        )

    def snapshot(self, root: Path):
        return {
            str(path.relative_to(root)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()
        }

    def test_explicit_materialization_supports_complete_current_closure(self):
        root = self.fixture()
        self.assertFalse((root / kit.CURRENT_COMMAND_TARGETS["project_memory"]).exists())
        kit.bootstrap(root)
        kit.verify_command_closure(root)
        before = self.snapshot(root)
        kit.bootstrap(root)
        self.assertEqual(self.snapshot(root), before)
        for key, source in kit.MEMORY_COPIES.items():
            target = root / kit.CURRENT_COMMAND_TARGETS[key]
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), (ROOT / source).read_bytes())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_memory_drift_blocks_before_any_generated_write(self):
        root = self.fixture()
        target = root / kit.CURRENT_COMMAND_TARGETS["project_memory_core"]
        target.write_text("foreign modified helper\n")
        before = self.snapshot(root)
        with self.assertRaises((SpecError, ValueError)):
            kit.bootstrap(root)
        self.assertEqual(self.snapshot(root), before)
        self.assertFalse((root / kit.CURRENT_COMMAND_TARGETS["project_memory"]).exists())

    def test_half_pair_and_redirected_pair_fail_before_writes(self):
        for mutation in ("missing", "redirected"):
            with self.subTest(mutation=mutation):
                root = self.fixture()
                profile_path = root / ".kent/workflow-profile.toml"
                profile = profile_path.read_text()
                if mutation == "missing":
                    profile = re.sub(r"(?m)^project_memory_core\s*=.*\n", "", profile)
                else:
                    profile = profile.replace(
                        kit.CURRENT_COMMAND_TARGETS["project_memory_core"],
                        ".kent/scripts/other_memory.py",
                    )
                profile_path.write_text(profile)
                before = self.snapshot(root)
                with self.assertRaises((SpecError, ValueError)):
                    kit.bootstrap(root)
                self.assertEqual(self.snapshot(root), before)

    def test_v1_without_memory_pair_blocks_before_any_generated_write(self):
        root = self.fixture()
        profile_path = root / ".kent/workflow-profile.toml"
        profile_path.write_text(re.sub(
            r"(?m)^(?:project_memory|project_memory_core)\s*=.*\n",
            "", profile_path.read_text(),
        ))
        before = self.snapshot(root)
        with self.assertRaises(SpecError):
            kit.bootstrap(root)
        self.assertEqual(self.snapshot(root), before)

    def test_memory_symlink_blocks_without_touching_its_target(self):
        root = self.fixture()
        foreign = root / "foreign.py"
        foreign.write_text("foreign content\n")
        target = root / kit.CURRENT_COMMAND_TARGETS["project_memory_core"]
        target.symlink_to(foreign)
        before = self.snapshot(root)
        with self.assertRaises((SpecError, ValueError)):
            kit.bootstrap(root)
        self.assertEqual(self.snapshot(root), before)
        self.assertTrue(target.is_symlink())

    def test_current_cli_materialization_parity_and_stale_failure_are_source_only(self):
        root = self.fixture()
        before = self.snapshot(root)
        result = self.run_builder(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        default_graph = json.loads(result.stdout)
        self.assertEqual(before, self.snapshot(root))
        result = self.run_builder(root, "--bootstrap", "--write-current-spec")
        self.assertEqual(result.returncode, 0, result.stderr)
        current = root / kit.CURRENT_SPEC_PATH
        graph = json.loads(current.read_text())
        self.assertEqual(graph, default_graph)
        self.assertEqual(graph["name"], "Kit Engineering Delivery v5")
        self.assertIn("implement_replan", {edge["key"] for edge in graph["edges"]})
        self.assertEqual(
            list((root / ".kent/workflows").glob("*.spec.json")), [current],
        )
        before = self.snapshot(root)
        for flag in ("--check", "--check-current"):
            result = self.run_builder(root, flag)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, self.snapshot(root))
        result = self.run_builder(root, "--write-spec")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.snapshot(root))
        current.write_text("{}\n")
        before = self.snapshot(root)
        result = self.run_builder(root, "--check-current")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current Kit workflow spec is stale", result.stderr)
        self.assertEqual(before, self.snapshot(root))

    def test_check_current_rejects_bootstrap_before_any_write(self):
        root = self.fixture()
        before = self.snapshot(root)
        for flag in ("--check", "--check-current"):
            result = self.run_builder(root, flag, "--bootstrap")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot be combined with --bootstrap", result.stderr)
            self.assertEqual(before, self.snapshot(root))

    def test_current_spec_symlink_is_never_read_or_overwritten(self):
        root = self.fixture()
        kit.bootstrap(root)
        foreign = root / "foreign.json"
        foreign.write_text("{}\n")
        (root / kit.CURRENT_SPEC_PATH).symlink_to(foreign)
        before = self.snapshot(root)
        for flag in ("--write-current-spec", "--check-current"):
            result = self.run_builder(root, flag)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not contain symlinks", result.stderr)
            self.assertEqual(before, self.snapshot(root))
