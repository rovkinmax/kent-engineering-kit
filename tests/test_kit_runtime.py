from __future__ import annotations

import hashlib
import io
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
from types import ModuleType
from types import SimpleNamespace
import unittest
from unittest import mock

from workflowkit import ci_contract, kit_runtime


REPO_ROOT = Path(__file__).resolve().parents[1]


class KitRuntimeBindingTest(unittest.TestCase):
    def load_checkpoint_template(self, name: str):
        checkpoint_path = REPO_ROOT / "templates/project/workflow-checkpoint"
        loader = importlib.machinery.SourceFileLoader(name, str(checkpoint_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        if spec is None:
            raise AssertionError("could not load checkpoint template")
        checkpoint = importlib.util.module_from_spec(spec)
        loader.exec_module(checkpoint)
        return checkpoint

    def test_missing_profile_preserves_legacy_loader_behavior(self) -> None:
        from workflowkit import delivery

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.assertIsNone(kit_runtime.qualified_kit_runtime(root))

            checkpoint_path = REPO_ROOT / "templates/project/workflow-checkpoint"
            loader = importlib.machinery.SourceFileLoader(
                "kit_runtime_legacy_checkpoint_test",
                str(checkpoint_path),
            )
            spec = importlib.util.spec_from_loader(loader.name, loader)
            self.assertIsNotNone(spec)
            if spec is None:
                raise AssertionError("could not load checkpoint template")
            checkpoint = importlib.util.module_from_spec(spec)
            loader.exec_module(checkpoint)

            home = root / "home"
            tool = home / ".kent/bin/kent-prepare-github-ci"
            tool.parent.mkdir(parents=True)
            tool.symlink_to(REPO_ROOT / "scripts/prepare-github-ci")
            with mock.patch.object(Path, "home", return_value=home):
                self.assertIsNone(checkpoint.qualified_kit_runtime(root))
                selected = checkpoint.delivery_module(root)
            self.assertIs(selected, delivery)

            with mock.patch.object(
                checkpoint,
                "_runtime_profile",
                side_effect=FileNotFoundError("profile removed"),
            ), mock.patch.dict(
                checkpoint._QUALIFIED_RUNTIME_HANDLES,
                {(str(root), None): object()},
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "profile disappeared",
                ):
                    checkpoint.qualified_kit_runtime(root)

    def test_failed_opted_in_qualification_does_not_use_legacy_loader(self) -> None:
        checkpoint_path = REPO_ROOT / "templates/project/workflow-checkpoint"
        loader = importlib.machinery.SourceFileLoader(
            "kit_runtime_failed_opt_in_checkpoint_test",
            str(checkpoint_path),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        if spec is None:
            raise AssertionError("could not load checkpoint template")
        checkpoint = importlib.util.module_from_spec(spec)
        loader.exec_module(checkpoint)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                mock.patch.object(
                    checkpoint,
                    "_runtime_profile",
                    return_value={"kit_runtime": {"commit": "bad"}},
                ),
                mock.patch.object(
                    checkpoint,
                    "_runtime_bootstrap",
                    side_effect=ValueError("invalid opt-in"),
                ),
                mock.patch.object(
                    checkpoint.importlib,
                    "import_module",
                ) as global_import,
            ):
                with self.assertRaisesRegex(ValueError, "invalid opt-in"):
                    checkpoint.delivery_module(root)
            global_import.assert_not_called()

    def test_explicit_qualified_handle_survives_project_root_deletion(self) -> None:
        checkpoint_path = REPO_ROOT / "templates/project/workflow-checkpoint"
        loader = importlib.machinery.SourceFileLoader(
            "kit_runtime_deleted_root_checkpoint_test",
            str(checkpoint_path),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        if spec is None:
            raise AssertionError("could not load checkpoint template")
        checkpoint = importlib.util.module_from_spec(spec)
        loader.exec_module(checkpoint)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "project"
            root.mkdir()
            root.rmdir()
            delivery = object()
            qualified = mock.Mock()
            qualified.project_root = root
            qualified.native_authorized = True
            qualified.module.return_value = delivery

            self.assertIs(checkpoint.delivery_module(root, qualified), delivery)
            qualified.module.assert_called_once_with("delivery")

            with self.assertRaisesRegex(ValueError, "another project"):
                checkpoint.delivery_module(root.parent / "other", qualified)

    def test_binding_is_closed_and_requires_explicit_publication_ref(self) -> None:
        binding = kit_runtime._parse_binding(
            {
                "schema": kit_runtime.KIT_RUNTIME_BINDING_SCHEMA,
                "root": "/opt/kent-engineering-kit",
                "commit": "a" * 40,
                "publication_ref": "refs/remotes/origin/main",
            }
        )

        self.assertEqual(binding.root, "/opt/kent-engineering-kit")
        self.assertEqual(binding.commit, "a" * 40)

        for invalid in (
            {
                "schema": kit_runtime.KIT_RUNTIME_BINDING_SCHEMA,
                "root": "/opt/kent-engineering-kit",
                "commit": "A" * 40,
                "publication_ref": "refs/remotes/origin/main",
            },
            {
                "schema": kit_runtime.KIT_RUNTIME_BINDING_SCHEMA,
                "root": "/opt/kent-engineering-kit",
                "commit": "a" * 40,
                "publication_ref": "refs/heads/main",
            },
            {
                "schema": kit_runtime.KIT_RUNTIME_BINDING_SCHEMA,
                "root": "/opt/kent-engineering-kit",
                "commit": "a" * 40,
                "publication_ref": "refs/remotes/origin/HEAD",
            },
            {
                "schema": kit_runtime.KIT_RUNTIME_BINDING_SCHEMA,
                "root": "/opt/kent-engineering-kit",
                "commit": "a" * 40,
                "publication_ref": "refs/remotes/origin/main",
                "extra": True,
            },
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                kit_runtime.KitRuntimeError
            ):
                kit_runtime._parse_binding(invalid)

    def test_script_binding_allows_r2_receipt_owner_only(self) -> None:
        admission = {
            "schema": "delivery-source-admission-v2",
            "receipt_ref": {"path": "/tmp/receipt.json", "sha256": "c" * 64},
            "primary_workspace_id": "workspace-1",
            "primary_root": "/tmp/project",
            "kit_root": "/tmp/kit",
            "source_ref": "refs/remotes/origin/main",
            "kit_ref": "refs/remotes/origin/main",
            "ci_helper": "/tmp/project/.kent/scripts/prepare-ci",
            "helper_bindings": [],
            "binding": {
                "kind": "r2-installed",
                "installation_binding_sha256": "d" * 64,
                "receipt_workflow_id": "a0a2a900-9f98-46bf-84e4-7661a95189eb",
            },
            "ingress": {"kind": "script-edge", "edge_id": "entry"},
        }

        self.assertIs(
            kit_runtime._closed_source_admission(admission),
            admission,
        )
        target_workflow_id = "11111111-1111-4111-8111-111111111111"
        self.assertEqual(
            kit_runtime._source_receipt_workflow_id(
                admission,
                target_workflow_id,
            ),
            "a0a2a900-9f98-46bf-84e4-7661a95189eb",
        )

        for binding in (
            {
                "kind": "plan-publication",
                "receipt_workflow_id": "a0a2a900-9f98-46bf-84e4-7661a95189eb",
            },
            {
                **admission["binding"],
                "receipt_workflow_id": "not-a-uuid",
            },
            {
                **admission["binding"],
                "receipt_workflow_id": "A0A2A900-9F98-46BF-84E4-7661A95189EB",
            },
        ):
            with self.subTest(binding=binding), self.assertRaises(
                kit_runtime.KitRuntimeError
            ):
                kit_runtime._closed_source_admission({
                    **admission,
                    "binding": binding,
                })

    def test_import_rejects_mixed_sys_modules_without_removing_entries(self) -> None:
        # Verification sets TMPDIR inside build/kent-workflow, which is itself
        # below REPO_ROOT. Keep this foreign source root outside the selected
        # Kit tree even when the test runs under that contained TMPDIR.
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            foreign_path = Path(temporary) / "workflowkit/foreign.py"
            self.assertFalse(foreign_path.resolve().is_relative_to(REPO_ROOT))
            foreign_path.parent.mkdir()
            foreign_path.write_text("# foreign Kit source\n")
            foreign = ModuleType("workflowkit.foreign")
            foreign.__file__ = str(foreign_path)

            with mock.patch.dict(sys.modules, {"workflowkit.foreign": foreign}):
                with self.assertRaisesRegex(
                    kit_runtime.KitRuntimeError,
                    "another source root",
                ):
                    kit_runtime._import_kit_module("kit_runtime", REPO_ROOT)
                self.assertIs(sys.modules["workflowkit.foreign"], foreign)

    def test_import_restores_path_and_bytecode_policy_without_reloading(self) -> None:
        path_before = list(sys.path)
        bytecode_before = sys.dont_write_bytecode

        selected = kit_runtime._import_kit_module("kit_runtime", REPO_ROOT)

        self.assertIs(selected, kit_runtime)
        self.assertEqual(sys.path, path_before)
        self.assertEqual(sys.dont_write_bytecode, bytecode_before)

    def test_ci_archive_reader_closure_adds_loader_only_for_opted_in_profile(self) -> None:
        commands = {
            "runtime_contracts": ".kent/scripts/workflow_runtime_contracts.py",
            "evidence": ".kent/scripts/workflow-evidence-ledger",
            "checkpoint": ".kent/scripts/workflow-checkpoint",
            "plan_contract": ".kent/scripts/workflow-plan-contract",
            "prepare_ci": ".kent/scripts/workflow-prepare-github-ci",
            "kit_runtime_loader": ".kent/scripts/workflow_kit_runtime.py",
        }

        def profile(*, opted_in: bool):
            return SimpleNamespace(
                kit_runtime={} if opted_in else None,
                kit_managed_commands=(
                    ["kit_runtime_loader"] if opted_in else []
                ),
                command=lambda key: commands.get(key, ""),
                capability=lambda _key: False,
                source_ci_contract=lambda: False,
                procedure=lambda _key: ".kent/commands/cleanup-task.md",
                ci_archive_reader_sources=[],
            )

        legacy = ci_contract._archive_project_reader_bindings(
            profile(opted_in=False),
            script_key="prepare_ci",
            script_path=commands["prepare_ci"],
        )
        self.assertNotIn(".kent/scripts/workflow_kit_runtime.py", legacy)

        qualified = ci_contract._archive_project_reader_bindings(
            profile(opted_in=True),
            script_key="prepare_ci",
            script_path=commands["prepare_ci"],
        )
        self.assertEqual(
            qualified[".kent/scripts/workflow_kit_runtime.py"],
            "workflowkit/kit_runtime.py",
        )

    def test_prepare_ci_wrapper_selects_qualified_cli_over_environment_override(self) -> None:
        checkpoint = self.load_checkpoint_template(
            "kit_runtime_prepare_ci_checkpoint_test"
        )

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "project"
            kit_root = Path(temporary).resolve() / "kit"
            scripts = project / ".kent/scripts"
            scripts.mkdir(parents=True)
            (project / ".kent/workflow-profile.toml").write_text(
                "kit_runtime = {}\n"
            )
            selected_cli = kit_root / "scripts/prepare-github-ci"
            selected_cli.parent.mkdir(parents=True)
            cli_bytes = b"#!/usr/bin/env python3\n# selected qualified CLI\n"
            selected_cli.write_bytes(cli_bytes)
            selected_cli.chmod(0o755)
            binding = SimpleNamespace(commit="a" * 40)
            qualified = SimpleNamespace(
                native_authorized=True,
                project_root=project,
                source_root=kit_root,
                binding=binding,
            )
            bootstrap = SimpleNamespace(
                _git_blob=mock.Mock(return_value=(cli_bytes, 0o755)),
                _read_regular_file=mock.Mock(return_value=(cli_bytes, 0o755)),
            )
            native_context = {"task_id": "task-1", "node_id": "script-1"}
            raw = json.dumps({
                "workspace_path": str(project),
                "_kent": native_context,
            }).encode()
            stdin = SimpleNamespace(buffer=io.BytesIO(raw))
            legacy_override = str(Path(temporary) / "legacy-prepare-ci")
            completed = subprocess.CompletedProcess([], 0)

            with (
                mock.patch.object(
                    checkpoint,
                    "_runtime_profile",
                    return_value={"kit_runtime": {}},
                ),
                mock.patch.object(
                    checkpoint,
                    "_runtime_bootstrap",
                    return_value=bootstrap,
                ),
                mock.patch.object(
                    checkpoint,
                    "qualified_kit_runtime",
                    return_value=qualified,
                ) as qualify,
                mock.patch.object(checkpoint.sys, "stdin", stdin),
                mock.patch.object(
                    checkpoint.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
                mock.patch.object(checkpoint.os, "execv") as legacy_exec,
                mock.patch.dict(
                    os.environ,
                    {"KENT_PREPARE_GITHUB_CI_BIN": legacy_override},
                ),
            ):
                self.assertEqual(checkpoint.prepare_github_ci_command(project), 0)

            qualify.assert_called_once_with(
                project,
                source_binding=native_context,
            )
            run.assert_called_once_with(
                [sys.executable, "-B", str(selected_cli)],
                cwd=project,
                input=raw,
                check=False,
            )
            legacy_exec.assert_not_called()

    def test_prepare_ci_wrapper_keeps_legacy_loader_when_unbound(self) -> None:
        checkpoint = self.load_checkpoint_template(
            "kit_runtime_legacy_prepare_ci_checkpoint_test"
        )

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "project"
            (project / ".kent").mkdir(parents=True)
            (project / ".kent/workflow-profile.toml").write_text(
                "schema_version = 4\n"
            )
            selected = str(Path(temporary) / "legacy-prepare-ci")
            with (
                mock.patch.object(
                    checkpoint,
                    "_runtime_profile",
                    return_value={},
                ),
                mock.patch.object(
                    checkpoint,
                    "_prepare_ci_legacy_cli",
                    return_value=selected,
                ),
                mock.patch.object(checkpoint.os, "execv") as legacy_exec,
                mock.patch.dict(
                    os.environ,
                    {"KENT_PREPARE_GITHUB_CI_BIN": selected},
                ),
            ):
                self.assertEqual(checkpoint.prepare_github_ci_command(project), 0)
            legacy_exec.assert_called_once_with(selected, [selected])

    def test_prepare_ci_wrapper_does_not_fall_back_after_opt_in_failure(self) -> None:
        checkpoint = self.load_checkpoint_template(
            "kit_runtime_failed_prepare_ci_checkpoint_test"
        )

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "project"
            (project / ".kent").mkdir(parents=True)
            (project / ".kent/workflow-profile.toml").write_text(
                "kit_runtime = { schema_version = 1 }\n"
            )
            native_context = {"task_id": "task-1", "node_id": "script-1"}
            stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps({
                "workspace_path": str(project),
                "_kent": native_context,
            }).encode()))
            stdout = SimpleNamespace(buffer=io.BytesIO())
            with (
                mock.patch.object(
                    checkpoint,
                    "_runtime_profile",
                    return_value={"kit_runtime": {}},
                ),
                mock.patch.object(
                    checkpoint,
                    "qualified_kit_runtime",
                    side_effect=RuntimeError("native binding mismatch"),
                ),
                mock.patch.object(checkpoint.sys, "stdin", stdin),
                mock.patch.object(checkpoint.sys, "stdout", stdout),
                mock.patch.object(checkpoint.os, "execv") as legacy_exec,
                mock.patch.object(checkpoint.subprocess, "run") as run,
                mock.patch.dict(
                    os.environ,
                    {"KENT_PREPARE_GITHUB_CI_BIN": "/tmp/legacy-prepare-ci"},
                ),
            ):
                self.assertEqual(checkpoint.prepare_github_ci_command(project), 2)
            self.assertEqual(stdout.buffer.getvalue(), b'{"error":"ci_prepare_input_invalid"}\n')
            legacy_exec.assert_not_called()
            run.assert_not_called()

    def test_prepare_ci_shell_wrapper_preserves_input_and_checkpoint_dispatch(self) -> None:
        wrapper_source = (
            REPO_ROOT / "templates/project/workflow-prepare-github-ci"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            scripts = project / ".kent/scripts"
            scripts.mkdir(parents=True)
            (project / ".kent/workflow-profile.toml").write_text(
                "kit_runtime = {}\n"
            )
            wrapper = scripts / "workflow-prepare-github-ci"
            wrapper.write_bytes(wrapper_source)
            wrapper.chmod(0o755)
            (scripts / "workflow-checkpoint").write_text(
                "import sys\n"
                "def prepare_github_ci_command(project_root, *, require_runtime_binding=False):\n"
                "    payload = sys.stdin.buffer.read()\n"
                "    sys.stdout.buffer.write(project_root.name.encode() + b':' + payload)\n"
                "    return 23\n"
            )
            input_bytes = (
                b'{"operation":"archive","workspace_path":"'
                + str(project).encode()
                + b'"}\n'
            )
            result = subprocess.run(
                [str(wrapper)],
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 23, result.stderr.decode())
            self.assertEqual(
                result.stdout,
                project.name.encode() + b":" + input_bytes,
            )
            self.assertEqual(result.stderr, b"")

    def test_prepare_ci_shell_wrapper_rejects_symlinked_kent_directory(self) -> None:
        wrapper_source = (
            REPO_ROOT / "templates/project/workflow-prepare-github-ci"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / "project"
            external = base / "external"
            external_scripts = external / ".kent/scripts"
            project.mkdir()
            external_scripts.mkdir(parents=True)
            (project / ".kent").symlink_to(
                external / ".kent",
                target_is_directory=True,
            )
            (external / ".kent/workflow-profile.toml").write_text(
                "schema_version = 4\n"
            )
            wrapper = external_scripts / "workflow-prepare-github-ci"
            wrapper.write_bytes(wrapper_source)
            wrapper.chmod(0o755)
            legacy_cli = base / "legacy-prepare-ci"
            legacy_cli.write_text("#!/bin/sh\necho legacy-fallback\n")
            legacy_cli.chmod(0o755)

            result = subprocess.run(
                [str(project / ".kent/scripts/workflow-prepare-github-ci")],
                input=b"{}\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "KENT_PREPARE_GITHUB_CI_BIN": str(legacy_cli),
                },
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertIn(
                b"project .kent directory is unsafe",
                result.stderr,
            )

    def test_published_cohort_requires_exact_clean_head_and_ancestor_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Kit Runtime Test")
            self.git(root, "config", "user.email", "kit-runtime@example.invalid")
            source = root / "workflowkit"
            source.mkdir()
            (source / "kit_runtime.py").write_bytes(
                (REPO_ROOT / "workflowkit/kit_runtime.py").read_bytes()
            )
            self.git(root, "add", "workflowkit/kit_runtime.py")
            self.git(root, "commit", "-q", "-m", "Initial Kit runtime")
            selected_commit = self.git(root, "rev-parse", "HEAD")
            self.git(
                root,
                "update-ref",
                "refs/remotes/origin/main",
                selected_commit,
            )
            binding = kit_runtime.KitRuntimeBinding(
                schema=kit_runtime.KIT_RUNTIME_BINDING_SCHEMA,
                root=str(root),
                commit=selected_commit,
                publication_ref="refs/remotes/origin/main",
            )

            self.assertEqual(
                kit_runtime._qualify_git_cohort(binding),
                root,
            )

            (source / "uncommitted.py").write_text("# dirty\n")
            with self.assertRaisesRegex(
                kit_runtime.KitRuntimeError,
                "differs from its native binding",
            ):
                kit_runtime._qualify_git_cohort(binding)

    def test_managed_bootstrap_must_match_pinned_source_bytes_and_copy_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            kit_root = base / "kit"
            project_root = base / "project"
            kit_root.mkdir()
            project_root.mkdir()
            self.git(kit_root, "init", "-q")
            self.git(kit_root, "config", "user.name", "Kit Runtime Test")
            self.git(
                kit_root,
                "config",
                "user.email",
                "kit-runtime@example.invalid",
            )
            source = kit_root / "workflowkit"
            source.mkdir()
            source_bytes = (REPO_ROOT / "workflowkit/kit_runtime.py").read_bytes()
            (source / "kit_runtime.py").write_bytes(source_bytes)
            self.git(kit_root, "add", "workflowkit/kit_runtime.py")
            self.git(kit_root, "commit", "-q", "-m", "Pin Kit runtime")
            commit = self.git(kit_root, "rev-parse", "HEAD")
            self.git(kit_root, "update-ref", "refs/tags/v1.0.0", commit)

            target = project_root / ".kent/scripts/workflow_kit_runtime.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(source_bytes)
            target.chmod(0o755)
            binding = {
                "schema_version": 1,
                "root": str(kit_root),
                "commit": commit,
                "publication_ref": "refs/tags/v1.0.0",
            }
            profile = {
                "kit_runtime": binding,
                "kit_managed_commands": ["kit_runtime_loader"],
                "command_versions": {"kit_runtime_loader": "1.0.0"},
                "commands": {
                    "kit_runtime_loader": ".kent/scripts/workflow_kit_runtime.py",
                },
            }
            parsed_binding = kit_runtime._profile_table_binding(binding)

            kit_runtime._validate_loader_profile(
                project_root,
                profile,
                parsed_binding,
            )

            target.chmod(0o644)
            with self.assertRaisesRegex(
                kit_runtime.KitRuntimeError,
                "managed project Kit bootstrap differs",
            ):
                kit_runtime._validate_loader_profile(
                    project_root,
                    profile,
                    parsed_binding,
                )

    def test_script_ingress_reference_binds_exact_graph_edge_without_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            document_path = root / "source-admission.json"
            admission = {
                "schema": "delivery-source-admission-v2",
                "receipt_ref": {
                    "path": str(root / "receipt.json"),
                    "sha256": "c" * 64,
                },
                "primary_workspace_id": "workspace-1",
                "primary_root": str(root / "project"),
                "kit_root": str(root / "kit"),
                "source_ref": "refs/remotes/origin/main",
                "kit_ref": "refs/remotes/origin/main",
                "ci_helper": str(root / "project/.kent/scripts/prepare-ci"),
                "helper_bindings": [],
                "binding": {"kind": "plan-publication"},
                "ingress": {"kind": "script-edge", "edge_id": "edge-1"},
            }
            frame = {
                "project_id": "project-1",
                "workflow_id": "workflow-1",
                "source_admission": admission,
            }
            raw = json.dumps(
                frame,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            document_path.write_bytes(raw)
            document_path.chmod(0o600)
            description = json.dumps(
                {
                    "schema": kit_runtime.SCRIPT_INGRESS_REF_SCHEMA,
                    "path": str(document_path),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            graph_response = {
                "workflow_id": "workflow-1",
                "graph": {
                    "nodes": [
                        {"id": "start-node", "key": "start"},
                        {"id": "script-node", "kind": "script"},
                    ],
                    "edges": [
                        {
                            "id": "edge-1",
                            "transition_group_id": "ingress-group",
                            "target_node_id": "script-node",
                            "prompt": "",
                        },
                    ],
                    "transition_groups": [
                        {
                            "id": "ingress-group",
                            "source_node_id": "start-node",
                            "description": description,
                        },
                    ],
                },
            }

            admitted = kit_runtime._validate_script_ingress_graph(
                graph_response,
                project_id="project-1",
                workflow_id="workflow-1",
            )
            self.assertEqual(admitted["admission"], admission)
            self.assertEqual(admitted["ingress_node_id"], "script-node")

            bypass = json.loads(json.dumps(graph_response))
            bypass["graph"]["transition_groups"].append(
                {
                    "id": "bypass-group",
                    "source_node_id": "start-node",
                    "description": "",
                }
            )
            bypass["graph"]["edges"].append(
                {
                    "id": "bypass-edge",
                    "transition_group_id": "bypass-group",
                    "target_node_id": "script-node",
                }
            )
            with self.assertRaisesRegex(
                kit_runtime.KitRuntimeError,
                "bypass route",
            ):
                kit_runtime._validate_script_ingress_graph(
                    bypass,
                    project_id="project-1",
                    workflow_id="workflow-1",
                )

            document_path.write_bytes(raw + b" ")
            with self.assertRaisesRegex(
                kit_runtime.KitRuntimeError,
                "digest drifted",
            ):
                kit_runtime._validate_script_ingress_graph(
                    graph_response,
                    project_id="project-1",
                    workflow_id="workflow-1",
                )

    def test_puber_initial_and_later_script_nodes_admit_same_frame_in_fresh_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project_root = base / "project"
            kit_root = base / "kit"
            project_root.mkdir()
            kit_root.mkdir()
            project_scripts = project_root / ".kent/scripts"
            project_scripts.mkdir(parents=True)
            profile_path = project_root / ".kent/workflow-profile.toml"

            self.git(kit_root, "init", "-q")
            self.git(kit_root, "config", "user.name", "Kit Runtime Test")
            self.git(
                kit_root,
                "config",
                "user.email",
                "kit-runtime@example.invalid",
            )
            kit_source_path = kit_root / "workflowkit/kit_runtime.py"
            kit_source_path.parent.mkdir()
            kit_source = (REPO_ROOT / "workflowkit/kit_runtime.py").read_bytes()
            kit_source_path.write_bytes(kit_source)
            self.git(kit_root, "add", "workflowkit/kit_runtime.py")
            self.git(kit_root, "commit", "-q", "-m", "Pin Kit runtime")
            kit_commit = self.git(kit_root, "rev-parse", "HEAD")
            self.git(
                kit_root,
                "update-ref",
                "refs/remotes/origin/main",
                kit_commit,
            )

            loader_copy = project_scripts / "workflow_kit_runtime.py"
            loader_copy.write_bytes(kit_source)
            loader_copy.chmod(0o755)
            profile_path.write_text(
                "schema_version = 4\n"
                f'kit_runtime = {{ schema_version = 1, root = "{kit_root}", '
                f'commit = "{kit_commit}", '
                'publication_ref = "refs/remotes/origin/main" }\n'
                'kit_managed_commands = ["kit_runtime_loader"]\n'
                "\n[command_versions]\n"
                'kit_runtime_loader = "1.0.0"\n'
                "\n[commands]\n"
                'kit_runtime_loader = ".kent/scripts/workflow_kit_runtime.py"\n'
            )

            project_id = "project-1"
            workflow_id = "11111111-1111-4111-8111-111111111111"
            task_id = "Puber-14"
            workspace_id = "workspace-1"
            initial_node = "initial-script"
            later_node = "later-script"
            initial_script_path = ".kent/scripts/workflow-puber-initial"
            later_script_path = ".kent/scripts/workflow-puber-later"
            (project_root / initial_script_path).write_text("# initial Script\n")
            (project_root / later_script_path).write_text("# later Script\n")

            private_dir = base / "private"
            private_dir.mkdir(mode=0o700)
            private_dir.chmod(0o700)
            receipt_path = private_dir / "receipt.json"
            admission = {
                "schema": "delivery-source-admission-v2",
                "receipt_ref": {
                    "path": str(receipt_path),
                    "sha256": "0" * 64,
                },
                "primary_workspace_id": workspace_id,
                "primary_root": str(project_root),
                "kit_root": str(kit_root),
                "source_ref": "refs/remotes/origin/main",
                "kit_ref": "refs/remotes/origin/main",
                "ci_helper": str(project_scripts / "workflow-prepare-github-ci"),
                "helper_bindings": [],
                "binding": {"kind": "plan-publication"},
                "ingress": {"kind": "script-edge", "edge_id": "entry-edge"},
            }
            receipt = {
                "schema": "delivery-source-publication-v1",
                "project_id": project_id,
                "workflow_id": workflow_id,
                "source_commit": "a" * 40,
                "kit_commit": kit_commit,
                "runtime_roots": [str(project_root), str(kit_root)],
                "files": [{
                    "root": str(kit_root),
                    "relative_path": "workflowkit/kit_runtime.py",
                    "sha256": hashlib.sha256(kit_source).hexdigest(),
                    "byte_size": len(kit_source),
                    "mode": 0o644,
                }],
                "policy": "published_source_v1",
                "local_inputs": [],
            }
            receipt_raw = kit_runtime._canonical_json(receipt)
            receipt_path.write_bytes(receipt_raw)
            receipt_path.chmod(0o600)
            admission["receipt_ref"]["sha256"] = hashlib.sha256(
                receipt_raw
            ).hexdigest()
            admission_path = base / "admission.json"
            admission_path.write_bytes(kit_runtime._canonical_json(admission))

            document_path = private_dir / "source-admission.json"
            graph_path = base / "graph-response.json"
            task_path = base / "task-response.json"

            graph = {
                "workflow_id": workflow_id,
                "graph": {
                    "nodes": [
                        {"id": "start-node", "key": "start", "kind": "agent"},
                        {
                            "id": initial_node,
                            "kind": "script",
                            "script_path": initial_script_path,
                        },
                        {
                            "id": later_node,
                            "kind": "script",
                            "script_path": later_script_path,
                        },
                    ],
                    "edges": [
                        {
                            "id": "entry-edge",
                            "transition_group_id": "entry-group",
                            "target_node_id": initial_node,
                            "prompt_template": "",
                            "prompt": "",
                        },
                        {
                            "id": "later-edge",
                            "transition_group_id": "later-group",
                            "target_node_id": later_node,
                            "prompt_template": "ordinary Script transition",
                        },
                    ],
                    "transition_groups": [
                        {
                            "id": "entry-group",
                            "source_node_id": "start-node",
                            "description": "",
                        },
                        {
                            "id": "later-group",
                            "source_node_id": initial_node,
                            "description": "",
                        },
                    ],
                },
            }

            def write_frame(source_admission: dict[str, object]) -> None:
                frame = {
                    "project_id": project_id,
                    "workflow_id": workflow_id,
                    "source_admission": source_admission,
                }
                frame_raw = kit_runtime._canonical_json(frame)
                document_path.write_bytes(frame_raw)
                document_path.chmod(0o600)
                reference = kit_runtime._canonical_json({
                    "schema": kit_runtime.SCRIPT_INGRESS_REF_SCHEMA,
                    "path": str(document_path),
                    "sha256": hashlib.sha256(frame_raw).hexdigest(),
                }).decode("utf-8")
                graph["graph"]["transition_groups"][0]["description"] = reference
                graph_path.write_text(json.dumps(graph))

            write_frame(admission)
            (base / "kent").write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                f"project = {str(project_root)!r}\n"
                f"project_id = {project_id!r}\n"
                f"task_id = {task_id!r}\n"
                f"workflow_id = {workflow_id!r}\n"
                "task_path = pathlib.Path(os.environ['KENT_TASK_FIXTURE'])\n"
                "graph_path = pathlib.Path(os.environ['KENT_GRAPH_FIXTURE'])\n"
                "if args == ['project', project]:\n"
                "    print(project_id)\n"
                "elif args == ['task', 'show', task_id, '--project', project_id, '--json']:\n"
                "    print(task_path.read_text())\n"
                "elif args == ['workflow', 'graph', 'inspect', workflow_id, '--json']:\n"
                "    print(graph_path.read_text())\n"
                "else:\n"
                "    raise SystemExit(2)\n"
            )
            kent = base / "kent"
            kent.chmod(0o755)

            child = textwrap.dedent(
                """
                import json
                import sys
                from pathlib import Path
                from types import SimpleNamespace
                from unittest import mock

                from workflowkit import kit_runtime

                project = Path(sys.argv[1])
                kent = Path(sys.argv[2])
                task_id = sys.argv[3]
                node_id = sys.argv[4]
                expected_route = sys.argv[5]
                expected_outcome = sys.argv[6]
                admission_path = Path(sys.argv[7])
                admission = json.loads(admission_path.read_text())
                calls = []

                def verified():
                    return SimpleNamespace(
                        project_id="project-1",
                        workflow_id="11111111-1111-4111-8111-111111111111",
                        primary_root=admission["primary_root"],
                        primary_workspace_id=admission["primary_workspace_id"],
                        kit_root=admission["kit_root"],
                        value=admission,
                        receipt_sha256=admission["receipt_ref"]["sha256"],
                    )

                def admit_initial(root, context, *, kent_path):
                    calls.append("initial")
                    assert root == project
                    assert kent_path == kent
                    assert context == {"task_id": task_id, "node_id": node_id}
                    return verified()

                def admit_generic(
                    value, *, project_id, workflow_id, project_root,
                    workspace_path, kent_path,
                ):
                    calls.append("later")
                    assert value == admission
                    assert project_id == "project-1"
                    assert workflow_id == "11111111-1111-4111-8111-111111111111"
                    assert project_root == admission["primary_root"]
                    assert workspace_path == project
                    assert kent_path == kent
                    return verified()

                operations = SimpleNamespace(
                    admit_script_source_ingress=admit_initial,
                    admit_source_admission=admit_generic,
                )
                try:
                    with (
                        mock.patch.object(
                            kit_runtime,
                            "_native_executable",
                            return_value=kent,
                        ),
                        mock.patch.object(
                            kit_runtime,
                            "_import_kit_module",
                            return_value=operations,
                        ),
                    ):
                        result = kit_runtime.qualified_kit_runtime(
                            project,
                            source_binding={"task_id": task_id, "node_id": node_id},
                        )
                    assert result is not None and result.native_authorized
                    if expected_outcome != "success":
                        raise AssertionError("foreign native Script context was accepted")
                    assert calls == [expected_route], calls
                    print(json.dumps({"route": calls[0], "qualified": True}))
                except kit_runtime.KitRuntimeError as error:
                    if expected_outcome != "reject":
                        raise
                    assert not calls, calls
                    print(json.dumps({"rejected": str(error)}))
                """
            )

            def run_case(
                *,
                node_id: str,
                route: str,
                outcome: str,
                selected_task_id: str = task_id,
            ) -> subprocess.CompletedProcess[str]:
                env = os.environ.copy()
                env.pop("KENT_SESSION_ID", None)
                env["PYTHONPATH"] = str(REPO_ROOT)
                env["KENT_TASK_FIXTURE"] = str(task_path)
                env["KENT_GRAPH_FIXTURE"] = str(graph_path)
                return subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        child,
                        str(project_root),
                        str(kent),
                        selected_task_id,
                        node_id,
                        route,
                        outcome,
                        str(admission_path),
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

            def write_task(node_id: str, current_path: str) -> None:
                task_path.write_text(json.dumps({
                    "summary": {
                        "id": task_id,
                        "project_id": project_id,
                        "short_id": task_id,
                        "workflow_id": workflow_id,
                    },
                    "source_workspace": {
                        "workspace_id": workspace_id,
                        "root_path": str(project_root),
                        "is_primary": True,
                    },
                    "worktree_path": str(project_root),
                    "current_nodes": [],
                    "live_sessions": [],
                    "current_scripts": [{
                        "current_node": {
                            "node_id": node_id,
                            "transition_branch_key": None,
                        },
                        "path": current_path,
                    }],
                }))

            write_task(initial_node, initial_script_path)
            initial_result = run_case(
                node_id=initial_node,
                route="initial",
                outcome="success",
            )
            self.assertEqual(initial_result.returncode, 0, initial_result.stderr)
            self.assertEqual(
                json.loads(initial_result.stdout),
                {"route": "initial", "qualified": True},
            )

            write_task(later_node, later_script_path)
            later_result = run_case(
                node_id=later_node,
                route="later",
                outcome="success",
            )
            self.assertEqual(later_result.returncode, 0, later_result.stderr)
            self.assertEqual(
                json.loads(later_result.stdout),
                {"route": "later", "qualified": True},
            )

            foreign_task = run_case(
                node_id=initial_node,
                route="initial",
                outcome="reject",
                selected_task_id="foreign-task",
            )
            self.assertEqual(foreign_task.returncode, 0, foreign_task.stderr)
            self.assertIn("rejected", json.loads(foreign_task.stdout))

            foreign_node = run_case(
                node_id="foreign-script",
                route="initial",
                outcome="reject",
            )
            self.assertEqual(foreign_node.returncode, 0, foreign_node.stderr)
            self.assertIn("rejected", json.loads(foreign_node.stdout))

            wrong_path = project_root / ".kent/scripts/foreign-script"
            wrong_path.write_text("# foreign path\n")
            write_task(later_node, str(wrong_path))
            foreign_path = run_case(
                node_id=later_node,
                route="later",
                outcome="reject",
            )
            self.assertEqual(foreign_path.returncode, 0, foreign_path.stderr)
            self.assertIn("rejected", json.loads(foreign_path.stdout))

            wrong_admission = dict(admission)
            wrong_admission["kit_root"] = str(base / "foreign-kit")
            write_frame(wrong_admission)
            admission_path.write_bytes(
                kit_runtime._canonical_json(wrong_admission)
            )
            write_task(initial_node, initial_script_path)
            foreign_binding = run_case(
                node_id=initial_node,
                route="initial",
                outcome="reject",
            )
            self.assertEqual(foreign_binding.returncode, 0, foreign_binding.stderr)
            self.assertIn("rejected", json.loads(foreign_binding.stdout))

    def test_puber_runtime_qualifies_real_r2_source_admission_without_session(self) -> None:
        from tests.test_canonical_workflow_lifecycle import (
            create_verified_source_fixture,
        )

        project_id = "project-22222222-2222-4222-8222-222222222222"
        workflow_id = "11111111-1111-4111-8111-111111111111"
        task_id = "task-puber-runtime"
        initial_node = "initial-script"
        later_node = "later-script"
        fixture, _ = create_verified_source_fixture(
            self,
            project_id,
            workflow_id,
            receipt_workflow_id=workflow_id,
        )

        # Turn the existing source-admission fixture into a complete published
        # Kit cohort. The real inspector requires the transitive Kit closure,
        # not just the runtime bootstrap module.
        kit_package = REPO_ROOT / "workflowkit"
        for source in kit_package.glob("*.py"):
            target = fixture.kit / "workflowkit" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(source.stat().st_mode & 0o777)
        fixture.repin_native_sources()
        kit_commit = fixture.commits[fixture.kit]

        loader_source = (kit_package / "kit_runtime.py").read_bytes()
        loader_copy = fixture.project / ".kent/scripts/workflow_kit_runtime.py"
        loader_copy.write_bytes(loader_source)
        loader_copy.chmod(0o755)
        initial_script_path = ".kent/scripts/puber-runtime-initial"
        later_script_path = ".kent/scripts/puber-runtime-later"
        fixture.put(fixture.project, initial_script_path, "# initial Script\n", 0o755)
        fixture.put(fixture.project, later_script_path, "# later Script\n", 0o755)
        profile = (
            "schema_version = 4\n"
            'kit_managed_commands = ["kit_runtime_loader"]\n'
            "\n[command_versions]\n"
            'kit_runtime_loader = "1.0.0"\n'
            "\n[commands]\n"
            'checkpoint = ".kent/scripts/workflow-checkpoint"\n'
            'kit_runtime_loader = ".kent/scripts/workflow_kit_runtime.py"\n'
            "\n[policies]\n"
            'delivery_reliability = "v1"\n'
            "\n[project_memory]\n"
            f"root = {json.dumps(str(fixture.receipt['project_memory']['root']))}\n"
            'project_key = "fixture"\n'
            'project_uid = "project-uid"\n'
            'mode = "r2"\n'
            "\n[kit_runtime]\n"
            "schema_version = 1\n"
            f"root = {json.dumps(str(fixture.kit))}\n"
            f'commit = "{kit_commit}"\n'
            'publication_ref = "refs/remotes/origin/main"\n'
        )
        fixture.put(
            fixture.project,
            ".kent/workflow-profile.toml",
            profile,
        )
        fixture.repin_native_sources()

        admission = dict(fixture.source_admission)
        admission["receipt_ref"] = dict(admission["receipt_ref"])
        admission["receipt_ref"]["sha256"] = fixture.digest
        admission["binding"] = dict(admission["binding"])
        admission["binding"]["installation_binding_sha256"] = hashlib.sha256(
            fixture.binding_path.read_bytes()
        ).hexdigest()
        admission["ingress"] = {
            "kind": "script-edge",
            "edge_id": "initial-ingress-edge",
        }
        frame = {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "source_admission": admission,
        }
        frame_raw = kit_runtime._canonical_json(frame)
        frame_path = fixture.evidence / "script-source-admission.json"
        frame_path.write_bytes(frame_raw)
        frame_path.chmod(0o600)
        group_description = json.dumps(
            {
                "schema": kit_runtime.SCRIPT_INGRESS_REF_SCHEMA,
                "path": str(frame_path),
                "sha256": hashlib.sha256(frame_raw).hexdigest(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        graph = {
            "workflow_id": workflow_id,
            "graph": {
                "nodes": [
                    {"id": "start-node", "key": "start", "kind": "agent"},
                    {
                        "id": initial_node,
                        "key": "initial",
                        "kind": "script",
                        "script_path": initial_script_path,
                    },
                    {
                        "id": later_node,
                        "key": "later",
                        "kind": "script",
                        "script_path": later_script_path,
                    },
                ],
                "node_groups": [],
                "edges": [
                    {
                        "id": "initial-ingress-edge",
                        "transition_group_id": "initial-ingress-group",
                        "target_node_id": initial_node,
                        "prompt_template": "",
                        "prompt": "",
                    },
                    {
                        "id": "later-edge",
                        "transition_group_id": "later-group",
                        "target_node_id": later_node,
                        "prompt_template": "ordinary later Script transition",
                    },
                ],
                "transition_groups": [
                    {
                        "id": "initial-ingress-group",
                        "source_node_id": "start-node",
                        "description": group_description,
                    },
                    {
                        "id": "later-group",
                        "source_node_id": initial_node,
                        "description": "",
                    },
                ],
            },
        }
        graph_path = fixture.base / "script-graph.json"
        graph_path.write_text(json.dumps(graph))
        task_path = fixture.base / "script-task.json"

        def write_task(node_id: str, script_path: str) -> None:
            task_path.write_text(json.dumps({
                "summary": {
                    "id": task_id,
                    "project_id": project_id,
                    "short_id": "PUBER-14",
                    "workflow_id": workflow_id,
                },
                "source_workspace": {
                    "workspace_id": "workspace-test",
                    "root_path": str(fixture.project),
                    "is_primary": True,
                },
                "project": {"default_workspace_id": "workspace-test"},
                "worktree_path": str(fixture.project),
                "current_nodes": [],
                "live_sessions": [],
                "current_scripts": [{
                    "current_node": {
                        "node_id": node_id,
                        "transition_branch_key": None,
                    },
                    "path": script_path,
                }],
            }))

        fixture.native_bin.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            f"project = {str(fixture.project)!r}\n"
            f"project_id = {project_id!r}\n"
            f"task_id = {task_id!r}\n"
            f"workflow_id = {workflow_id!r}\n"
            f"task_path = pathlib.Path({str(task_path)!r})\n"
            f"graph_path = pathlib.Path({str(graph_path)!r})\n"
            "args = sys.argv[1:]\n"
            "if args == ['project', project]:\n"
            "    print(project_id)\n"
            "elif args == ['project', 'list']:\n"
            "    print(project_id + '\\tFixture\\t' + project)\n"
            "elif len(args) == 6 and args[:2] == ['task', 'show'] and "
            "args[2] == task_id and args[3] == '--project' and "
            "args[4] == project_id and args[5] == '--json':\n"
            "    print(task_path.read_text())\n"
            "elif args[:4] == ['workflow', 'graph', 'inspect', workflow_id] "
            "and args[4:] == ['--json']:\n"
            "    print(graph_path.read_text())\n"
            "elif args[:2] == ['task', 'list']:\n"
            "    print(json.dumps({'items': [{'task_id': task_id}]}))\n"
            "else:\n"
            "    raise SystemExit(2)\n"
        )
        fixture.native_bin.chmod(0o700)

        child = textwrap.dedent(
            """
            import importlib.machinery
            import importlib.util
            import json
            import shutil
            import sys
            from pathlib import Path

            project = Path(sys.argv[1])
            task_id, node_id, workflow_id, project_id = sys.argv[2:]
            bootstrap_path = project / ".kent/scripts/workflow_kit_runtime.py"
            loader = importlib.machinery.SourceFileLoader(
                "published_puber_kit_runtime", str(bootstrap_path)
            )
            spec = importlib.util.spec_from_loader(loader.name, loader)
            bootstrap = importlib.util.module_from_spec(spec)
            sys.modules[loader.name] = bootstrap
            loader.exec_module(bootstrap)
            context = {"task_id": task_id, "node_id": node_id}
            qualified = bootstrap.qualified_kit_runtime(
                project, source_binding=context
            )
            assert qualified is not None and qualified.native_authorized
            operations = qualified.module("operations")
            kent = Path(shutil.which("kent")).resolve()
            if node_id == "initial-script":
                verified = operations.admit_script_source_ingress(
                    project, context, kent_path=kent
                )
            else:
                graph_response = bootstrap._native_graph(
                    kent, project, workflow_id
                )
                ingress = bootstrap._validate_script_ingress_graph(
                    graph_response,
                    project_id=project_id,
                    workflow_id=workflow_id,
                )
                verified = operations.admit_source_admission(
                    ingress["admission"],
                    project_id=project_id,
                    workflow_id=workflow_id,
                    project_root=ingress["admission"]["primary_root"],
                    workspace_path=project,
                    kent_path=kent,
                )
            assert Path(operations.__file__).resolve().is_relative_to(
                qualified.source_root
            )
            print(json.dumps({
                "native_authorized": qualified.native_authorized,
                "source_root": str(qualified.source_root),
                "operations_path": str(Path(operations.__file__).resolve()),
                "project_id": verified.project_id,
                "workflow_id": verified.workflow_id,
                "primary_root": verified.primary_root,
                "kit_root": verified.kit_root,
                "receipt_sha256": verified.receipt_sha256,
            }))
            """
        )

        def run_node(
            node_id: str,
            script_path: str,
        ) -> subprocess.CompletedProcess[str]:
            write_task(node_id, script_path)
            env = {
                key: value
                for key, value in os.environ.items()
                if key != "KENT_SESSION_ID"
            }
            env["HOME"] = str(fixture.home)
            env["PATH"] = str(fixture.bin) + os.pathsep + env.get("PATH", "")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            return subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    child,
                    str(fixture.project),
                    task_id,
                    node_id,
                    workflow_id,
                    project_id,
                ],
                cwd=fixture.project,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

        for node_id, script_path in (
            (initial_node, initial_script_path),
            (later_node, later_script_path),
        ):
            with self.subTest(node=node_id):
                admitted = run_node(node_id, script_path)
                self.assertEqual(admitted.returncode, 0, admitted.stderr)
                result = json.loads(admitted.stdout)
                self.assertTrue(result["native_authorized"])
                self.assertEqual(result["source_root"], str(fixture.kit))
                self.assertTrue(
                    Path(result["operations_path"]).is_relative_to(
                        fixture.kit / "workflowkit"
                    )
                )
                self.assertEqual(result["project_id"], project_id)
                self.assertEqual(result["workflow_id"], workflow_id)
                self.assertEqual(result["primary_root"], str(fixture.project))
                self.assertEqual(result["kit_root"], str(fixture.kit))
                self.assertEqual(result["receipt_sha256"], fixture.digest)

        original_receipt = fixture.receipt_path.read_bytes()
        fixture.receipt_path.write_bytes(original_receipt + b" ")
        fixture.receipt_path.chmod(0o600)
        receipt_drift = run_node(initial_node, initial_script_path)
        self.assertNotEqual(receipt_drift.returncode, 0)
        self.assertIn("native source receipt digest drifted", receipt_drift.stderr)
        fixture.receipt_path.write_bytes(original_receipt)
        fixture.receipt_path.chmod(0o600)

        kit_source = fixture.kit / "workflowkit/operations.py"
        original_source = kit_source.read_bytes()
        kit_source.write_bytes(original_source + b"\n# source drift\n")
        source_drift = run_node(later_node, later_script_path)
        self.assertNotEqual(source_drift.returncode, 0)
        self.assertIn(
            "published Kit cohort differs from its native binding",
            source_drift.stderr,
        )
        kit_source.write_bytes(original_source)

    def test_sdk_uses_adapter_blob_from_exact_initial_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Kit Runtime Test")
            self.git(root, "config", "user.email", "kit-runtime@example.invalid")
            (root / ".kent/workflows").mkdir(parents=True)
            binding = {
                "schema_version": 1,
                "root": "/opt/kent-engineering-kit",
                "commit": "b" * 40,
                "publication_ref": "refs/remotes/origin/main",
            }
            profile = {
                "kit_runtime": binding,
                "commands": {
                    kit_runtime.SDK_SOURCE_BINDING_COMMAND:
                    kit_runtime.SDK_SOURCE_BINDING_PATH,
                },
            }
            profile_toml = (
                'schema_version = 4\n'
                f'kit_runtime = {{ schema_version = 1, root = "{binding["root"]}", '
                f'commit = "{binding["commit"]}", '
                f'publication_ref = "{binding["publication_ref"]}" }}\n'
                '\n[commands]\n'
                'kit_runtime_source_binding = '
                '".kent/scripts/workflow_sdk_source_binding.py"\n'
            )
            header = (
                "SDK_INITIAL_SOURCE_ADMISSION_V1\n"
                "Qualified initial commit: @@SDK_INITIAL_COMMIT_OID@@\n"
                "Qualified initial Git tree: @@SDK_INITIAL_TREE_OID@@\n"
                "Qualified Kit root: @@SDK_KIT_ROOT@@\n"
                "Qualified Kit commit: @@SDK_KIT_COMMIT_OID@@\n"
                "Qualified Kit publication ref: @@SDK_KIT_PUBLICATION_REF@@\n"
            )
            adapter = (
                "def validate_source_binding(*, project_root, task, session, "
                "graph, initial_commit, initial_tree, profile, header):\n"
                "    assert task['execution_target']['commit_oid'] == initial_commit\n"
                "    assert session['session_id'] == 'session-123'\n"
                "    assert graph['workflow_id'] == 'workflow-123'\n"
                "    assert initial_tree and '@@SDK_INITIAL_COMMIT_OID@@' in header\n"
                "    assert '@@SDK_INITIAL_TREE_OID@@' in header\n"
                "    assert '@@SDK_KIT_ROOT@@' in header\n"
                "    assert '@@SDK_KIT_COMMIT_OID@@' in header\n"
                "    assert '@@SDK_KIT_PUBLICATION_REF@@' in header\n"
                "    expected = header.replace('@@SDK_INITIAL_COMMIT_OID@@', initial_commit)\n"
                "    expected = expected.replace('@@SDK_INITIAL_TREE_OID@@', initial_tree)\n"
                "    expected = expected.replace('@@SDK_KIT_ROOT@@', profile['kit_runtime']['root'])\n"
                "    expected = expected.replace('@@SDK_KIT_COMMIT_OID@@', profile['kit_runtime']['commit'])\n"
                "    expected = expected.replace('@@SDK_KIT_PUBLICATION_REF@@', profile['kit_runtime']['publication_ref'])\n"
                "    edges = [edge for edge in graph['graph']['edges'] if edge.get('key') == 'start_plan']\n"
                "    assert len(edges) == 1\n"
                "    assert edges[0]['prompt_template'].startswith(expected)\n"
                "    assert '@@SDK_INITIAL_' not in edges[0]['prompt_template']\n"
                "    assert '@@SDK_KIT_' not in edges[0]['prompt_template']\n"
                "    return {\n"
                "        'schema': 'kit-runtime-source-binding-v1',\n"
                "        'root': profile['kit_runtime']['root'],\n"
                "        'commit': profile['kit_runtime']['commit'],\n"
                "        'publication_ref': profile['kit_runtime']['publication_ref'],\n"
                "    }\n"
            )
            (root / ".kent/workflow-profile.toml").write_text(profile_toml)
            (root / ".kent/workflows/sdk-initial-source-admission.txt").write_text(
                header
            )
            adapter_path = root / kit_runtime.SDK_SOURCE_BINDING_PATH
            adapter_path.parent.mkdir(parents=True)
            adapter_path.write_text(adapter)
            self.git(root, "add", ".kent")
            self.git(root, "commit", "-q", "-m", "Pin initial SDK binding")
            initial_commit = self.git(root, "rev-parse", "HEAD")
            adapter_path.write_text("raise AssertionError('working tree adapter ran')\n")

            result = kit_runtime._sdk_initial_binding(
                project_root=root,
                profile=profile,
                task={
                    "detail": {
                        "execution_target": {
                            "provenance": "resolved",
                            "commit_oid": initial_commit,
                        },
                    },
                    "session_id": "session-123",
                },
                session={"session_id": "session-123"},
                graph={
                    "workflow_id": "workflow-123",
                    "graph": {
                        "nodes": [{"id": "plan-node", "key": "plan"}],
                        "edges": [{
                            "key": "start_plan",
                            "target_node_id": "plan-node",
                            "prompt_template": (
                        "SDK_INITIAL_SOURCE_ADMISSION_V1\n"
                        f"Qualified initial commit: {initial_commit}\n"
                        f"Qualified initial Git tree: "
                        f"{self.git(root, 'rev-parse', 'HEAD^{tree}')}\n"
                                f"Qualified Kit root: {binding['root']}\n"
                                f"Qualified Kit commit: {binding['commit']}\n"
                                "Qualified Kit publication ref: "
                                f"{binding['publication_ref']}\n\n"
                                "Plan {{.TaskShortId}}: {{.TaskTitle}}\n"
                            ),
                        }],
                        "transition_groups": [],
                    },
                },
            )

            self.assertEqual(result.root, binding["root"])
            self.assertEqual(result.commit, binding["commit"])
            self.assertEqual(
                result.publication_ref,
                binding["publication_ref"],
            )

    def test_sdk_script_context_qualifies_committed_adapter_without_agent_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project_root = base / "project"
            kit_root = base / "kit"
            project_root.mkdir()
            kit_root.mkdir()
            self.git(kit_root, "init", "-q")
            self.git(kit_root, "config", "user.name", "Kit Runtime Test")
            self.git(
                kit_root,
                "config",
                "user.email",
                "kit-runtime@example.invalid",
            )
            kit_module = kit_root / "workflowkit/kit_runtime.py"
            kit_module.parent.mkdir()
            kit_module.write_bytes(
                (REPO_ROOT / "workflowkit/kit_runtime.py").read_bytes()
            )
            self.git(kit_root, "add", "workflowkit/kit_runtime.py")
            self.git(kit_root, "commit", "-q", "-m", "Pin Kit runtime")
            kit_commit = self.git(kit_root, "rev-parse", "HEAD")
            self.git(
                kit_root,
                "update-ref",
                "refs/remotes/origin/main",
                kit_commit,
            )

            binding = {
                "schema_version": 1,
                "root": str(kit_root),
                "commit": kit_commit,
                "publication_ref": "refs/remotes/origin/main",
            }
            project_kent = project_root / ".kent"
            project_scripts = project_kent / "scripts"
            project_workflows = project_kent / "workflows"
            project_scripts.mkdir(parents=True)
            project_workflows.mkdir()
            profile = (
                "schema_version = 4\n"
                f'kit_runtime = {{ schema_version = 1, root = "{kit_root}", '
                f'commit = "{kit_commit}", '
                'publication_ref = "refs/remotes/origin/main" }\n'
                'kit_managed_commands = ["kit_runtime_loader"]\n'
                "\n[command_versions]\n"
                'kit_runtime_loader = "1.0.0"\n'
                "\n[commands]\n"
                'kit_runtime_loader = ".kent/scripts/workflow_kit_runtime.py"\n'
                'kit_runtime_source_binding = '
                '".kent/scripts/workflow_sdk_source_binding.py"\n'
            )
            (project_kent / "workflow-profile.toml").write_text(profile)
            loader_copy = project_scripts / "workflow_kit_runtime.py"
            loader_copy.write_bytes(kit_module.read_bytes())
            loader_copy.chmod(0o755)
            header = "SDK_INITIAL_SOURCE_ADMISSION_V1\nCommitted SDK header.\n"
            (project_workflows / "sdk-initial-source-admission.txt").write_text(
                header
            )
            workflow_id = "a0a2a900-9f98-46bf-84e4-7661a95189eb"
            project_id = "project-1"
            task_id = "task-1"
            script_path = ".kent/scripts/workflow-sdk-publication"
            script_file = project_root / script_path
            script_file.parent.mkdir(parents=True, exist_ok=True)
            script_file.write_text("# source-script fixture\n")
            adapter = (
                "def validate_source_binding(*, project_root, task, session, "
                "graph, initial_commit, initial_tree, profile, header, "
                "native_script_context=None):\n"
                "    assert session is None\n"
                f"    assert native_script_context == {{'task_id': {task_id!r}, "
                "'node_id': 'script-node'}\n"
                "    assert task['execution_target']['commit_oid'] == initial_commit\n"
                "    assert task['current_nodes'] == []\n"
                "    assert task['live_sessions'] == []\n"
                "    assert len(task['current_scripts']) == 1\n"
                "    assert task['current_scripts'][0]['current_node']['node_id'] == 'script-node'\n"
                "    assert graph['workflow_id'] == " + repr(workflow_id) + "\n"
                "    assert graph['graph']['nodes'][0]['kind'] == 'script'\n"
                "    assert header == " + repr(header) + "\n"
                "    return {\n"
                "        'schema': 'kit-runtime-source-binding-v1',\n"
                "        'root': profile['kit_runtime']['root'],\n"
                "        'commit': profile['kit_runtime']['commit'],\n"
                "        'publication_ref': profile['kit_runtime']['publication_ref'],\n"
                "    }\n"
            )
            adapter_path = project_scripts / "workflow_sdk_source_binding.py"
            adapter_path.write_text(adapter)
            self.git(project_root, "init", "-q")
            self.git(project_root, "config", "user.name", "Kit Runtime Test")
            self.git(
                project_root,
                "config",
                "user.email",
                "kit-runtime@example.invalid",
            )
            self.git(project_root, "add", ".kent")
            self.git(project_root, "commit", "-q", "-m", "Pin SDK source binding")
            initial_commit = self.git(project_root, "rev-parse", "HEAD")

            task_detail = {
                "summary": {
                    "id": task_id,
                    "project_id": project_id,
                    "short_id": "SDK-14",
                    "workflow_id": workflow_id,
                },
                "source_workspace": {
                    "workspace_id": "workspace-1",
                    "root_path": str(project_root),
                },
                "worktree_path": str(project_root),
                "execution_target": {
                    "provenance": "resolved",
                    "commit_oid": initial_commit,
                },
                "current_nodes": [],
                "live_sessions": [],
                "current_scripts": [{
                    "current_node": {
                        "node_id": "script-node",
                        "transition_branch_key": None,
                    },
                    "path": script_path,
                }],
            }
            graph_response = {
                "workflow_id": workflow_id,
                "graph": {
                    "nodes": [{
                        "id": "script-node",
                        "kind": "script",
                        "script_path": script_path,
                    }],
                    "edges": [],
                    "transition_groups": [],
                },
            }
            task_response_path = base / "task-response.json"
            task_response_path.write_text(json.dumps(task_detail))
            graph_response_path = base / "graph-response.json"
            graph_response_path.write_text(json.dumps(graph_response))
            kent = base / "kent"
            kent.write_text(
                f"#!{sys.executable}\n"
                "import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                f"project = {str(project_root)!r}\n"
                f"project_id = {project_id!r}\n"
                f"task_id = {task_id!r}\n"
                f"workflow_id = {workflow_id!r}\n"
                f"task_path = pathlib.Path({str(task_response_path)!r})\n"
                f"graph_path = pathlib.Path({str(graph_response_path)!r})\n"
                "if args == ['project', project]:\n"
                "    print(project_id)\n"
                "elif args == ['task', 'show', task_id, '--project', project_id, '--json']:\n"
                "    print(task_path.read_text())\n"
                "elif args == ['workflow', 'graph', 'inspect', workflow_id, '--json']:\n"
                "    print(graph_path.read_text())\n"
                "else:\n"
                "    raise SystemExit(2)\n"
            )
            kent.chmod(0o755)

            with (
                mock.patch.object(
                    kit_runtime,
                    "_native_executable",
                    return_value=kent,
                ),
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(
                    kit_runtime,
                    "_import_kit_module",
                    wraps=kit_runtime._import_kit_module,
                ) as selected_import,
            ):
                qualified = kit_runtime.qualified_kit_runtime(
                    project_root,
                    source_binding={"task_id": task_id, "node_id": "script-node"},
                )
                self.assertIsNotNone(qualified)
                if qualified is None:
                    raise AssertionError("SDK Script binding returned no handle")
                self.assertEqual(qualified.binding.root, str(kit_root))
                self.assertEqual(qualified.binding.commit, kit_commit)
                self.assertEqual(
                    qualified.binding.publication_ref,
                    "refs/remotes/origin/main",
                )
                selected_import.assert_not_called()

                task_detail["current_scripts"][0]["current_node"]["node_id"] = (
                    "another-script"
                )
                task_response_path.write_text(json.dumps(task_detail))
                with self.assertRaisesRegex(
                    kit_runtime.KitRuntimeError,
                    "current Script ownership is ambiguous",
                ):
                    kit_runtime.qualified_kit_runtime(
                        project_root,
                        source_binding={
                            "task_id": task_id,
                            "node_id": "script-node",
                        },
                    )
                selected_import.assert_not_called()

                task_detail["current_scripts"][0]["current_node"]["node_id"] = (
                    "script-node"
                )
                other_script_path = project_root / ".kent/scripts/another-script"
                other_script_path.write_text("# foreign current Script\n")
                task_detail["current_scripts"][0]["path"] = str(other_script_path)
                task_response_path.write_text(json.dumps(task_detail))
                with self.assertRaisesRegex(
                    kit_runtime.KitRuntimeError,
                    "Script path differs",
                ):
                    kit_runtime.qualified_kit_runtime(
                        project_root,
                        source_binding={
                            "task_id": task_id,
                            "node_id": "script-node",
                        },
                    )
                selected_import.assert_not_called()

                task_detail["current_scripts"][0]["path"] = script_path
                task_response_path.write_text(json.dumps(task_detail))
                with self.assertRaises(kit_runtime.KitRuntimeError):
                    kit_runtime.qualified_kit_runtime(
                        project_root,
                        source_binding={
                            "task_id": "foreign-task",
                            "node_id": "script-node",
                        },
                    )
                selected_import.assert_not_called()

    def git(self, root: Path, *arguments: str) -> str:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
