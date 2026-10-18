from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import os
import runpy
import shutil
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from workflowkit.project_memory import MAX_BYTES, Conflict, MemoryError, ProjectMemory


class ProjectMemoryTests(unittest.TestCase):
    def test_publication_inventory_v1_v2_closed_and_combined_limits(self):
        adapter = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                    "templates/project/workflow-project-memory"))
        validate = adapter["publication_inventory"]
        entry = {"root": "/primary", "relative_path": "AGENTS.md",
                 "sha256": "a" * 64, "byte_size": 1, "mode": 0o644}
        receipt = {key: None for key in adapter["PUBLICATION_RECEIPT_FIELDS"]}
        receipt.update(schema="delivery-runtime-publication-v1",
                       runtime_roots=["/primary", "/kit"], files=[entry])
        self.assertEqual(validate(receipt, "/primary", "/kit"), 1)
        local = entry | {"kind": "local-instruction",
                         "relative_path": ".kent/skills/private/instruction.md", "mode": 0o600}
        receipt.update(schema="delivery-runtime-publication-v2", local_inputs=[local])
        self.assertEqual(validate(receipt, "/primary", "/kit"), 2)
        self.assertEqual(validate(receipt | {"local_inputs": [
            local | {"relative_path": ".kent/skills/instruction.md"}]}, "/primary", "/kit"), 2)
        full = [entry | {"relative_path": f"file-{i}"} for i in range(511)]
        self.assertEqual(validate(receipt | {"files": full}, "/primary", "/kit"), 512)
        with self.assertRaises(ValueError):
            validate(receipt | {"files": full + [entry]}, "/primary", "/kit")
        for change in ({"kind": "module"}, {"mode": 0o700}, {"mode": 0o622},
                       {"relative_path": ".kent/skills/../instruction.md"},
                       {"relative_path": ".kent/skills/private/*.md"},
                       {"root": "/task"}, {"byte_size": True},
                       {"extra": True}, {"sha256": "invalid"}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    validate(receipt | {"local_inputs": [local | change]}, "/primary", "/kit")
        with self.assertRaises(ValueError):
            validate(receipt | {"local_inputs": [local, local]}, "/primary", "/kit")
        with self.assertRaises(ValueError):
            validate(receipt | {"files": [entry] * 512}, "/primary", "/kit")
        with self.assertRaises(ValueError):
            validate(receipt | {"schema": "delivery-runtime-publication-v1"}, "/primary", "/kit")
        with self.assertRaises(ValueError):
            validate(receipt | {"files": [entry | {"byte_size": 8 * 1024 * 1024,
                      "relative_path": f"file-{i}"} for i in range(4)]}, "/primary", "/kit")

    def test_native_adapter_v2_private_instruction_both_validators(self):
        installed, source, environment = self.native_adapter_fixture()
        ignore = source / ".gitignore"
        ignore.write_text(ignore.read_text() + "\n.kent/skills/private/\n")
        subprocess.run(["git", "-C", str(source), "add", ".gitignore"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "ignore private fixture"],
                       check=True, capture_output=True)
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                         text=True).strip()
        subprocess.run(["git", "-C", str(source), "update-ref", "refs/remotes/origin/main", commit],
                       check=True, capture_output=True)
        self.receipt["source_commit"] = commit
        item = next(row for row in self.receipt["files"]
                    if row["root"] == str(source) and row["relative_path"] == ".gitignore")
        raw = ignore.read_bytes()
        item.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
        path = source / ".kent/skills/private/instruction.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"private fixture instruction\n")
        path.chmod(0o600)
        self.receipt.update(schema="delivery-runtime-publication-v2", local_inputs=[{
            "kind": "local-instruction", "root": str(source),
            "relative_path": path.relative_to(source).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "byte_size": path.stat().st_size, "mode": 0o600,
        }])
        self.pin_adapter_receipt()
        self.write_adapter_binding()
        result = self.adapter_call(installed, source, None, "initialize-task",
                                   native_environment=environment, interface="workflow")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path.write_bytes(b"changed private fixture\n")
        result = self.adapter_call(installed, source, None, "initialize-task",
                                   native_environment=environment, interface="workflow")
        self.assertNotEqual(result.returncode, 0)

    def test_report_only_preparation_requires_gate_before_writing_and_retries_exactly(self):
        import workflowkit.project_memory as core
        adapter = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                    "templates/project/workflow-project-memory"))
        dispatch = adapter["workflow_dispatch"]
        request = {
            "schema": "r2-workflow-request-v1", "operation": "prepare-report-only",
            "workspace_path": self.temp.name, "provenance": [],
            "native_context": {"task_id": "task-native", "node_id": "node-script"}}
        with patch.dict(dispatch.__globals__, {
                "resolve_native": lambda *args, **kwargs: (self.store, "OSM-130", None),
                "require_acceptance_script": lambda *args: None}):
            with patch.dict(dispatch.__globals__, {
                    "require_acceptance_script": unittest.mock.Mock(
                        side_effect=ValueError("current graph node does not own acceptance"))}):
                with self.assertRaisesRegex(ValueError, "does not own acceptance"):
                    dispatch(core, request)
            self.assertFalse((self.store.path / "tasks/OSM-130/plan").exists())
            first = dispatch(core, request)
            self.assertEqual(dispatch(core, request), first)
            self.assertEqual(Path(first["path"]).read_text(), "not-applicable\n")
            self.assertIsNone(self.store.accepted_head("OSM-130"))
            changed = self.store.publish("tasks/OSM-130/plan", first["token"], "Changed scope")
            with self.assertRaises(Conflict):
                dispatch(core, request)
            self.assertEqual(self.store.read("tasks/OSM-130/plan").token, changed.token)

    def test_report_only_contract_has_explicit_kind_and_sentinel(self):
        revision = self.store.publish("tasks/OSM-130/plan", None, "not-applicable\n")
        self.store.accept_plan("OSM-130", revision.token, None, {
            "work_kind": "feature", "authority_refs": ["session_message(p/s/e/d)"],
            "plan_kind": "report_only"})
        status = self.store.check_plan("OSM-130")
        self.assertEqual(status["comparison"], "unchanged")
        self.assertEqual(status["accepted"]["plan_kind"], "report_only")
        contract = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                      "templates/project/workflow-plan-contract"))
        self.assertEqual(contract["memory_snapshot_projection"](status, "OSM-130")["plan_path"],
                         "not-applicable")
        changed = self.store.publish("tasks/OSM-130/plan", revision.token, "New scope")
        self.assertEqual(self.store.check_plan("OSM-130")["comparison"], "changed")
        with self.assertRaisesRegex(ValueError, "sentinel"):
            self.store.accept_plan("OSM-130", changed.token, status["accepted_snapshot"], {
                "work_kind": "feature", "authority_refs": ["session_message(p/s/e/d)"],
                "plan_kind": "report_only"})

    def test_r2_plan_contract_accepts_external_plan_and_checks_without_cache_repair(self):
        self.check_external_contract(report_only=False)

    def test_r2_report_only_accepts_external_sentinel_and_checks_without_cache_repair(self):
        self.check_external_contract(report_only=True)

    def check_external_contract(self, *, report_only):
        import io
        import contextlib
        root = Path(self.temp.name).resolve() / "native-contract"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        (root / ".kent").mkdir()
        (root / ".kent/workflow-profile.toml").write_text(
            '[project_memory]\nmode="r2"\n[policies]\ncoder_selection="disabled"\n')
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                   "templates/project/workflow-plan-contract"))
        first = None if report_only else self.store.publish(
            "tasks/OSM-130/plan", None,
            "Implement\nsession_message(project/session/event/digest)\n")
        task_binding = hashlib.sha256(
            (self.store.path / "tasks/OSM-130/task.json").read_bytes()).hexdigest()

        def call(workspace, payload, operation, **fields):
            if operation == "resolve-task":
                return {"mode": "r2"}
            if operation == "accepted-head":
                return {"accepted_snapshot": self.store.accepted_head("OSM-130")}
            if operation == "prepare-report-only":
                revision = self.store.publish(
                    "tasks/OSM-130/plan", None, "not-applicable\n",
                    operation_id="report-only-contract", provenance=fields["provenance"])
                return {"token": revision.token, "path": str(revision.path),
                        "sha256": revision.sha256}
            if operation == "resolve-plan":
                if fields["plan_path"] != "not-applicable":
                    self.store.plan_revision("OSM-130", fields["plan_path"])
                revision = self.store.read("tasks/OSM-130/plan")
                return {"working_path": str(revision.path), "working_revision": revision.token,
                        "working_sha256": revision.sha256}
            if operation == "accept-plan":
                revision = self.store.accept_plan("OSM-130", fields["expected_revision"],
                                                  fields["expected_accepted"], fields["metadata"])
                return {"token": revision.token}
            status = self.store.check_plan("OSM-130", coder_selection=fields["coder_selection"])
            status["cache_ref"] = {
                "schema": "r2-plan-contract-cache-v1", "task_short_id": "OSM-130",
                "project_uid": "project-a", "task_uid": "task-a",
                "task_binding_sha256": task_binding,
                "accepted_snapshot_path": status["accepted_path"],
                "accepted_snapshot_sha256": status["accepted_snapshot"]}
            return status

        payload = {"workspace_path": str(root), "task_short_id": "OSM-130",
                   "plan_path": "not-applicable" if report_only else str(first.path),
                   "work_kind": "feature",
                   "review_context": "reviewed session_message(project/session/event/digest)",
                   "plan_route": "start",
                   "plan_route_context": "not-applicable"}
        execute = module["execute"]
        with patch.dict(execute.__globals__, {"memory_call": call}), \
                patch.dict(os.environ, {"KENT_PLAN_CONTRACT_MODE": "accept"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute(payload), 0)
        cache = root / ".kent/runtime/OSM-130/plan-contract.json"
        self.assertEqual(json.loads(cache.read_text())["schema"], "r2-plan-contract-cache-v1")
        cache.unlink()
        with patch.dict(execute.__globals__, {"memory_call": call}), \
                patch.dict(os.environ, {"KENT_PLAN_CONTRACT_MODE": "check",
                                        "KENT_PLAN_CONTRACT_ROUTE": "verify",
                                        "KENT_PLAN_CONTRACT_STABLE_TRANSITION": "stable"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(execute(payload), 0)
        self.assertEqual(json.loads(output.getvalue())["transition"], "stable")
        self.assertEqual(module["memory_snapshot_projection"](
            self.store.check_plan("OSM-130"), "OSM-130")["plan_path"], payload["plan_path"])
        self.assertEqual(self.store.check_plan("OSM-130")["accepted"]["plan_kind"],
                         "report_only" if report_only else "markdown")
        self.assertFalse(cache.exists())
        self.assertFalse((root / ".todo").exists())
        original_head = self.store.accepted_head("OSM-130")
        preparation = root / "build/kent-workflow/OSM-130/cleanup-preparation.json"
        preparation.parent.mkdir(parents=True)
        preparation.write_text('{"state":"prepared"}')
        with patch.dict(execute.__globals__, {"memory_call": call}), \
                patch.dict(os.environ, {"KENT_PLAN_CONTRACT_MODE": "accept"}), \
                self.assertRaisesRegex(ValueError, "acceptance is closed"):
            execute(payload)
        self.assertEqual(self.store.accepted_head("OSM-130"), original_head)
        self.assertFalse(cache.exists())

    def test_plan_and_branch_producers_capture_existing_output_only_when_opted_in(self):
        import io
        import types
        for name in ("workflow-plan-contract", "workflow-branch-identity"):
            with self.subTest(name=name):
                root = Path(self.temp.name).resolve() / name
                scripts = root / ".kent/scripts"
                scripts.mkdir(parents=True)
                (root / ".kent/workflow-profile.toml").write_text(
                    '[policies]\ndelivery_reliability = "v1"\n')
                source = Path(__file__).resolve().parents[1] / "templates/project" / name
                shutil.copyfile(source, scripts / name)
                (scripts / "workflow-checkpoint").write_text(
                    "import json\n"
                    "def emit_producer_result(root, payload, raw, kind):\n"
                    "    assert payload['review_context'] == ' exact '\n"
                    "    output = json.loads(raw)\n"
                    "    output['commentary'] = kind\n"
                    "    return json.dumps(output).encode()\n")
                module = runpy.run_path(str(scripts / name))
                main = module["main"]
                output = io.BytesIO()

                def execute(payload):
                    print('{"transition":"blocked","review_context":" exact output "}')
                    return 0

                with patch.dict(main.__globals__, {"execute": execute}), \
                        patch.object(Path, "cwd", return_value=root), \
                        patch.object(sys, "stdin", io.StringIO('{"review_context":" exact "}')), \
                        patch.object(sys, "stdout", types.SimpleNamespace(buffer=output)), \
                        patch.dict(os.environ, {"KENT_PLAN_CONTRACT_MODE": "accept"}):
                    self.assertEqual(main(), 0)
                self.assertEqual(json.loads(output.getvalue())["review_context"], " exact output ")

    def test_plan_contract_memory_bridge_preserves_native_script_context(self) -> None:
        source = Path(__file__).resolve().parents[1] / "templates/project/workflow-plan-contract"
        installed = Path(self.temp.name).resolve() / "plan-adapter"
        installed.mkdir()
        shutil.copyfile(source, installed / source.name)
        helper = installed / "workflow-project-memory"
        helper.write_text(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "print(json.dumps({'schema':'r2-workflow-response-v1','result':request}))\n")
        module = runpy.run_path(str(installed / source.name))
        context = {"task_id": "task-native", "node_id": "node-script"}
        result = module["memory_call"](installed, {"_kent": context},
                                       "check-plan", coder_selection=None)
        self.assertEqual(result, {
            "schema": "r2-workflow-request-v1", "operation": "check-plan",
            "workspace_path": str(installed), "native_context": context,
            "coder_selection": None})
        helper.write_text("print('{\"schema\":\"unexpected\",\"result\":{}}')\n")
        with self.assertRaises(ValueError):
            module["memory_call"](installed, {}, "resolve-task")

    def test_plan_contract_cache_is_only_a_validated_locator(self) -> None:
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                   "templates/project/workflow-plan-contract"))
        cache = {
            "schema": "r2-plan-contract-cache-v1", "task_short_id": "OSM-130",
            "project_uid": "project-a", "task_uid": "task-a",
            "task_binding_sha256": "a" * 64,
            "accepted_snapshot_path": "/memory/accepted/" + "b" * 64 + ".json",
            "accepted_snapshot_sha256": "b" * 64,
        }
        validate = module["validate_memory_cache"]
        status = {"cache_ref": cache}
        validate(cache.copy(), status, "OSM-130")
        for key in cache:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({**cache, key: "different"}, status, "OSM-130")
        with self.assertRaises(ValueError):
            validate({**cache, "normalized_plan": "untrusted"}, status, "OSM-130")

    def test_acceptance_requires_effective_graph_script_command(self) -> None:
        adapter = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                    "templates/project/workflow-project-memory"))
        require = adapter["require_acceptance_script"]
        source = Path(self.temp.name).resolve()
        context = {"task_id": "task-native", "node_id": "node-script"}
        request = {"workspace_path": str(source), "native_context": context}
        graph = {"workflow_id": "workflow-native", "graph": {"nodes": [{
            "id": "node-script", "kind": "script",
            "script_path": ".kent/scripts/workflow-plan-contract-accept"}]}}

        def read(arguments, workspace):
            if arguments[0] == "project":
                return b"project-native\n"
            if arguments[0] == "task":
                return json.dumps({"summary": {"workflow_id": "workflow-native"}}).encode()
            return json.dumps(graph).encode()

        import workflowkit.project_memory as core
        with patch.dict(require.__globals__, {
                "git_root": lambda value: source, "native_read": read,
                "record_bytes": lambda *args, **kwargs: (
                    b'[commands]\nplan_contract_accept = '
                    b'".kent/scripts/workflow-plan-contract-accept"\n')}):
            require(core, request)
            for key, value in (("kind", "agent"), ("script_path", "writer-script"),
                               ("id", "other-node")):
                original = graph["graph"]["nodes"][0][key]
                graph["graph"]["nodes"][0][key] = value
                with self.assertRaises(ValueError):
                    require(core, request)
                graph["graph"]["nodes"][0][key] = original
            with self.assertRaises(ValueError):
                require(core, {"workspace_path": str(source)})

    def test_concurrent_first_ingress_allocates_one_neutral_task_identity(self) -> None:
        ready = threading.Barrier(2)

        def initialize(_):
            ready.wait()
            return self.store.initialize_task("OSM-131", None, [])

        with ThreadPoolExecutor(max_workers=2) as pool:
            bindings = list(pool.map(initialize, (1, 2)))
        self.assertEqual(bindings[0], bindings[1])
        self.assertIsInstance(bindings[0]["task_uid"], str)
        self.assertTrue(bindings[0]["task_uid"])

    def test_acceptance_binds_optional_coder_selection(self) -> None:
        plan = self.store.publish("tasks/OSM-130/plan", None, "Plan")
        selection = {"strategy": "simple", "reason": "bounded"}
        self.store.accept_plan(
            "OSM-130", plan.token, None,
            {"work_kind": "implementation", "authority_refs": ["human:1"],
             "coder_selection": selection})
        self.assertEqual(self.store.check_plan(
            "OSM-130", coder_selection=selection)["comparison"], "unchanged")
        self.assertEqual(self.store.check_plan(
            "OSM-130", coder_selection={**selection, "strategy": "complex"})["comparison"],
            "changed")
        self.assertEqual(self.store.check_plan("OSM-130")["comparison"], "changed")

    def test_accept_retry_after_published_head_returns_same_snapshot(self) -> None:
        self.assertIsNone(self.store.accepted_head("OSM-130"))
        plan = self.store.publish("tasks/OSM-130/plan", None, "Plan")
        metadata = {"work_kind": "implementation", "authority_refs": ["human:1"]}
        first = self.store.accept_plan("OSM-130", plan.token, None, metadata)
        self.assertEqual(self.store.accepted_head("OSM-130"), first.token)
        retried = self.store.accept_plan("OSM-130", plan.token, None, metadata)
        self.assertEqual(first, retried)
        with self.assertRaises(Conflict):
            self.store.accept_plan("OSM-130", plan.token, None,
                                   {**metadata, "authority_refs": ["human:2"]})

    def test_process_death_at_publication_boundaries_is_exactly_retryable(self) -> None:
        program = """
import os, sys
from pathlib import Path
from workflowkit.project_memory import ProjectMemory
store = ProjectMemory(Path(sys.argv[1]), "appsome", "project-a")
store.publish(sys.argv[2], sys.argv[3], "next", operation_id="crash-write",
              phase_hook=lambda phase: os._exit(77) if phase == sys.argv[4] else None)
"""
        for index, phase in enumerate(("revision_durable", "head_published")):
            with self.subTest(phase=phase):
                key = f"knowledge/crash-{index}"
                original = self.store.publish(key, None, "original")
                result = subprocess.run([sys.executable, "-c", program, str(self.root),
                                         key, original.token, phase],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 77, result.stderr)
                current = self.store.read(key)
                self.assertEqual(current.path.read_text(),
                                 "original" if phase == "revision_durable" else "next")
                settled = self.store.publish(key, original.token, "next",
                                             operation_id="crash-write")
                self.assertEqual(self.store.read(key), settled)
                self.assertEqual(original.path.read_text(), "original")

    def adapter_fixture(self, *, seed_standalone=True, legacy_task_ids=(),
                        task_id="task-native", short_id="OSM-130",
                        workflow_id="workflow-native", legacy_mode=False):
        """Build a disposable attached project and Kit with a complete v2 source pin."""
        base = Path(self.temp.name).resolve()
        project = base / "project"
        kit = base / "kit"
        repository = Path(__file__).resolve().parents[1]
        self.root = base / "memory"
        self.store = ProjectMemory.initialize(self.root, "appsome", "project-a")

        def put(root, relative, content, mode=0o644):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(mode)
            return path

        def git(root, *arguments):
            return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                                  capture_output=True, text=True).stdout

        for root in (project, kit):
            root.mkdir()
            git(root, "init", "-q")
            git(root, "config", "user.name", "Fixture")
            git(root, "config", "user.email", "fixture@example.invalid")

        put(project, "AGENTS.md", "Disposable source-admission fixture.\n")
        put(project, ".gitignore",
            "build/\n.kent/runtime/\n.kent/retained-evidence/\n__pycache__/\n")
        put(project, ".kent/project-contract.md", "Disposable fixture contract.\n")
        profile = (
            'schema_version = 3\n'
            '[policies]\ndelivery_reliability = "v1"\n'
            '[commands]\n'
            'checkpoint = ".kent/scripts/workflow-checkpoint"\n'
            'plan_contract_accept = ".kent/scripts/workflow-plan-contract-accept"\n'
        )
        if not legacy_mode:
            profile += (
                '[project_memory]\n'
                f'root = "{self.root}"\n'
                'project_key = "appsome"\nproject_uid = "project-a"\nmode = "r2"\n'
            )
        put(project, ".kent/workflow-profile.toml", profile)
        put(project, ".kent/scripts/workflow-checkpoint", "#!/bin/sh\nexit 0\n", 0o755)
        put(project, ".kent/scripts/workflow-plan-contract-accept",
            "#!/usr/bin/env python3\n# Disposable graph-owned acceptance command.\n", 0o755)
        put(project, ".kent/scripts/workflow-project-memory",
            (repository / "templates/project/workflow-project-memory").read_text(), 0o755)
        shutil.copyfile(repository / "workflowkit/project_memory.py",
                        project / ".kent/scripts/workflow_project_memory.py")

        put(kit, ".gitignore", "__pycache__/\n")
        put(kit, ".kent/workflow-profile.toml",
            '[commands]\n'
            'janitor = ".kent/scripts/workflow-task-janitor"\n'
            'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n')
        put(kit, ".kent/scripts/workflow-project-memory",
            (repository / "templates/project/workflow-project-memory").read_text(), 0o755)
        shutil.copyfile(repository / "workflowkit/project_memory.py",
                        kit / ".kent/scripts/workflow_project_memory.py")
        put(kit, ".kent/scripts/workflow-task-janitor",
            "#!/usr/bin/env python3\n# Disposable Kit Janitor fixture.\n", 0o755)
        put(kit, ".kent/scripts/workflow_runtime_contracts.py",
            '"""Disposable Kit Janitor sibling fixture."""\n')
        (kit / "scripts").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repository / "scripts/inspect-delivery-source",
                        kit / "scripts/inspect-delivery-source")
        (kit / "scripts/inspect-delivery-source").chmod(0o755)
        put(kit, "scripts/prepare-github-ci",
            "#!/usr/bin/env python3\nfrom workflowkit import ci_contract\n", 0o755)
        for name in ("__init__", "ci_contract", "delivery", "runtime", "profile",
                     "model", "release", "revision", "kent", "graph", "project_memory"):
            put(kit, f"workflowkit/{name}.py", f'"""Disposable {name} fixture module."""\n')

        self.home = base / "home"
        self.home.mkdir(mode=0o700)
        self.kent_home = self.home / ".kent"
        self.kent_home.mkdir(mode=0o700)
        self.bin = self.kent_home / "bin"
        self.bin.mkdir(mode=0o700)
        binding_dir = self.kent_home / "project-memory-bindings"
        binding_dir.mkdir(mode=0o700)
        installed = kit / ".kent/scripts"
        adapter_link = self.bin / "kent-project-memory"
        adapter_link.symlink_to(installed / "workflow-project-memory")
        ci_link = self.bin / "kent-prepare-github-ci"
        ci_link.symlink_to(kit / "scripts/prepare-github-ci")

        self.native_task = {
            "summary": {"id": task_id, "project_id": "project-test",
                        "short_id": short_id, "workflow_id": workflow_id},
            "source_workspace": {"root_path": str(project), "is_primary": True,
                                 "workspace_id": "workspace-test"},
            "project": {"default_workspace_id": "workspace-test"},
            "worktree_path": None, "live_sessions": [],
            "current_nodes": [{"node_id": "node-native", "session_id": "session-native"}],
            "current_scripts": [{
                "current_node": {"node_id": "node-accept", "transition_branch_key": None},
            }],
        }
        self.extra_native_tasks = []
        self.invalid_utf8_task_ids = set()
        self.native_worktree_status = None
        self.native_project_list_root = str(project)
        self.native_task_show_log = base / "native-task-show.log"
        self.native_command_log = base / "native-command.log"

        def write_fake_kent():
            binary = self.bin / "kent"
            tasks_json = json.dumps(
                [self.native_task, *self.extra_native_tasks], separators=(",", ":"))
            invalid_task_ids = json.dumps(sorted(self.invalid_utf8_task_ids))
            native_project_list_root = self.native_project_list_root
            native_worktree_status = self.native_worktree_status
            if native_worktree_status is None:
                native_worktree_status = {
                    "target": {
                        "workspace_id": "workspace-test",
                        "workspace_name": "Fixture",
                        "workspace_root": str(project),
                        "workspace_availability": "available",
                        "worktree": None,
                        "cwd_relpath": ".",
                        "effective_workdir": str(project),
                    },
                    "worktree": {
                        "recorded_root": str(project),
                        "observed_root": str(project),
                    },
                    "problems": [],
                }
            worktree_status_json = json.dumps(native_worktree_status)
            binary.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                f"project = {str(project)!r}\n"
                f"native_project_list_root = {native_project_list_root!r}\n"
                f"tasks = json.loads({tasks_json!r})\n"
                "tasks_by_id = {item['summary']['id']: item for item in tasks}\n"
                f"invalid_task_ids = set(json.loads({invalid_task_ids!r}))\n"
                f"worktree_status = json.loads({worktree_status_json!r})\n"
                f"task_show_log = {str(self.native_task_show_log)!r}\n"
                f"native_command_log = {str(self.native_command_log)!r}\n"
                f"workflow = {workflow_id!r}\n"
                "with open(native_command_log, 'a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(args) + '\\n')\n"
                "if len(args) == 2 and args[0] == 'project' and args[1] == project:\n"
                "    print('project-test')\n"
                "elif args == ['project', 'list']:\n"
                "    print('project-test\\tFixture\\t' + native_project_list_root)\n"
                "elif args == ['worktree', 'status', '--session', 'session-native', '--json']:\n"
                "    print(json.dumps(worktree_status))\n"
                "elif (len(args) == 6 and args[:2] == ['task', 'show']\n"
                "      and args[3:] == ['--project', 'project-test', '--json']):\n"
                "    task_id = args[2]\n"
                "    with open(task_show_log, 'a', encoding='utf-8') as log:\n"
                "        log.write(task_id + '\\n')\n"
                "    if task_id in invalid_task_ids:\n"
                "        sys.stdout.buffer.write(b'\\xff')\n"
                "        sys.exit(0)\n"
                "    if task_id not in tasks_by_id:\n"
                "        sys.exit(2)\n"
                "    print(json.dumps(tasks_by_id[task_id]))\n"
                "elif args[:6] == ['task', 'list', '--project', 'project-test', '--limit', "
                "'100'] and len(args) == 9 and args[6] == '--offset' and args[8] == '--json':\n"
                "    inventory = [{'task_id': item['summary']['id'], "
                "'short_id': item['summary']['short_id']} for item in tasks]\n"
                "    offset = int(args[7])\n"
                "    rows = inventory[offset:offset + 100]\n"
                "    print(json.dumps({'scope': {'project_id': 'project-test'}, "
                "'tasks': rows}))\n"
                "elif args == ['workflow', 'graph', 'inspect', workflow, '--json']:\n"
                "    print(json.dumps({'workflow_id': workflow, 'graph': {'nodes': [{"
                "'id': 'node-accept', 'kind': 'script', "
                "'script_path': '.kent/scripts/workflow-plan-contract-accept'}]}}))\n"
                "else:\n"
                "    sys.exit(2)\n")
            binary.chmod(0o700)

        for root in (project, kit):
            git(root, "add", ".")
            git(root, "commit", "-qm", "disposable fixture")
            head = git(root, "rev-parse", "HEAD").strip()
            git(root, "update-ref", "refs/remotes/origin/main", head)

        evidence = base / "evidence"
        evidence.mkdir(mode=0o700)
        self.receipt_path = evidence / "publication.json"
        self.receipt = {
            "schema": "delivery-runtime-publication-v1",
            "project_id": "project-test", "workflow_id": workflow_id,
            "source_commit": git(project, "rev-parse", "HEAD").strip(),
            "kit_commit": git(kit, "rev-parse", "HEAD").strip(),
            "runtime_roots": [str(project), str(kit)], "files": [],
            "policy": "delivery_reliability_v1",
            "project_memory": {
                "root": str(self.root), "project_key": "appsome",
                "project_uid": "project-a", "legacy_task_ids": list(legacy_task_ids),
            },
        }
        for root in (project, kit):
            for relative in git(root, "ls-files").splitlines():
                path = root / relative
                raw = path.read_bytes()
                self.receipt["files"].append({
                    "root": str(root), "relative_path": relative,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "byte_size": len(raw), "mode": stat.S_IMODE(path.stat().st_mode),
                })
        self.pin_adapter_receipt()

        profile_raw = (project / ".kent/workflow-profile.toml").read_bytes()
        self.installation_binding = {
            "schema": "r2-installation-binding-v2", "project_id": "project-test",
            "primary_root": str(project), "primary_workspace_id": "workspace-test",
            "profile_sha256": hashlib.sha256(profile_raw).hexdigest(),
            "receipt_path": str(self.receipt_path),
            "receipt_sha256": self.receipt_digest, "kit_root": str(kit),
            "source_ref": "refs/remotes/origin/main", "kit_ref": "refs/remotes/origin/main",
            "ci_helper": str(ci_link),
            "helper_bindings": [{"link": str(adapter_link),
                                 "target": str(installed / "workflow-project-memory")}],
        }
        self.installation_binding_path = binding_dir / "project-test.json"
        if not legacy_mode:
            self.write_adapter_binding()
        else:
            self.installation_binding_path.unlink(missing_ok=True)
        self._write_adapter_fake_kent = write_fake_kent
        write_fake_kent()

        self.project = project
        self.kit = kit
        self.binding_path = self.store.path / "tasks/OSM-130/task.json"
        self.binding = None
        if seed_standalone:
            standalone_task = self.store.initialize_task("OSM-130", "task-a", [])
            raw_binding = self.binding_path.read_bytes()
            self.binding = {
                "project_path": str(self.store.path), "project_uid": "project-a",
                "task_key": "OSM-130", "task_uid": standalone_task["task_uid"],
                "task_binding_sha256": hashlib.sha256(raw_binding).hexdigest(),
            }
        return installed, project, self.binding, self.binding_path

    def pin_adapter_receipt(self):
        raw = json.dumps(self.receipt, separators=(",", ":")).encode()
        self.receipt_path.write_bytes(raw)
        self.receipt_path.chmod(0o600)
        self.receipt_digest = hashlib.sha256(raw).hexdigest()

    def write_adapter_binding(self):
        self.installation_binding["receipt_sha256"] = self.receipt_digest
        self.installation_binding_path.write_text(json.dumps(
            self.installation_binding, separators=(",", ":")))
        self.installation_binding_path.chmod(0o600)

    def native_adapter_fixture(self, *, legacy_task_ids=(), task_id="task-native",
                               short_id="OSM-130", script=False, legacy=False):
        installed, source, _, _ = self.adapter_fixture(
            seed_standalone=False, legacy_task_ids=legacy_task_ids,
            task_id=task_id, short_id=short_id, legacy_mode=legacy)
        if script:
            self.native_task["current_scripts"] = [{
                "current_node": {"node_id": "node-script", "transition_branch_key": None},
            }]
            self._write_adapter_fake_kent()
            self.store.initialize_task(
                short_id, "task-native-uid",
                [{"namespace": "kent", "project_id": "project-test", "task_id": task_id}])
        environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "KENT_TASK_ID": task_id, "KENT_SESSION_ID": "session-native",
        }
        return installed, source, environment

    def adapter_call(self, installed, source, binding, operation, *, raw_body=None,
                     native_environment=None, interface="mcp", **fields):
        request = {"schema": f"r2-{interface}-request-v1", "operation": operation,
                   "workspace_path": str(source), **fields}
        if binding is not None:
            request["standalone_binding"] = binding
        environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(self.home),
        }
        environment.update(native_environment or {})
        payload = json.dumps(request)
        if raw_body is not None:
            payload = payload.encode() + b"\n" + raw_body
        return subprocess.run([str(self.bin / "kent-project-memory"), interface],
                              input=payload, text=raw_body is None, capture_output=True,
                              env=environment, cwd=source, timeout=120)

    def test_native_adapter_legacy_requires_verified_task_workspace(self) -> None:
        installed, source, environment = self.native_adapter_fixture(legacy=True)
        result = self.adapter_call(installed, source, None, "resolve-mcp",
                                   native_environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"], {"mode": "legacy", "binding_ref": None})
        untasked = self.adapter_call(installed, source, None, "resolve-mcp",
                                     native_environment={"PATH": environment["PATH"]})
        self.assertEqual(untasked.returncode, 0, untasked.stderr)
        self.assertEqual(json.loads(untasked.stdout)["result"]["mode"], "legacy")
        profile = source / ".kent/workflow-profile.toml"
        profile.write_text('[project_memory]\nmode = "r2"\n')
        incomplete = self.adapter_call(installed, source, None, "resolve-mcp",
                                       native_environment=environment)
        self.assertEqual(incomplete.returncode, 2)
        profile.write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
            '[commands]\ncheckpoint = ".kent/scripts/workflow-checkpoint"\n'
            'plan_contract_accept = ".kent/scripts/workflow-plan-contract-accept"\n')
        self.installation_binding_path.write_text("{}")
        self.installation_binding_path.chmod(0o600)
        incomplete = self.adapter_call(installed, source, None, "resolve-mcp",
                                       native_environment=environment)
        self.assertEqual(incomplete.returncode, 2)
        result = self.adapter_call(installed, source, None, "resolve-mcp",
                                   native_environment={**environment, "KENT_SESSION_ID": "foreign"})
        self.assertEqual(result.returncode, 2)

    def test_unadopted_primary_session_skips_task_discovery_and_r2_evidence_blocks(self) -> None:
        base = Path(self.temp.name).resolve()
        workspace = base / "unadopted-primary"
        (workspace / ".kent/scripts").mkdir(parents=True)
        profile = workspace / ".kent/workflow-profile.toml"
        profile.write_text('[policies]\ndelivery_reliability = "v1"\n')
        adapter = workspace / ".kent/scripts/workflow-project-memory"
        shutil.copyfile(
            Path(__file__).resolve().parents[1]
            / "templates/project/workflow-project-memory", adapter)
        adapter.chmod(0o755)

        home = base / "unadopted-home"
        (home / ".kent/project-memory-bindings").mkdir(parents=True, mode=0o700)
        binary_dir = base / "unadopted-bin"
        binary_dir.mkdir(mode=0o700)
        unexpected_queries = base / "unadopted-native-query.log"
        fake_kent = binary_dir / "kent"
        fake_kent.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            f"workspace = {str(workspace)!r}\n"
            f"unexpected_queries = {str(unexpected_queries)!r}\n"
            "if args == ['project', workspace]:\n"
            "    print('project-unadopted')\n"
            "elif args == ['project', 'list']:\n"
            "    print('project-unadopted\\tUnadopted\\t./')\n"
            "elif args[:2] in (['task', 'list'], ['worktree', 'status']):\n"
            "    with open(unexpected_queries, 'a', encoding='utf-8') as output:\n"
            "        output.write(' '.join(args) + '\\n')\n"
            "    raise SystemExit(3)\n"
            "else:\n"
            "    raise SystemExit(2)\n")
        fake_kent.chmod(0o700)
        request = {
            "schema": "r2-mcp-request-v1", "operation": "resolve-mcp",
            "workspace_path": str(workspace),
        }
        environment = {
            "HOME": str(home),
            "PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", ""),
            "KENT_SESSION_ID": "session-without-task",
        }

        def invoke():
            return subprocess.run(
                [sys.executable, str(adapter), "mcp"],
                input=json.dumps(request), text=True, capture_output=True,
                timeout=30, env=environment, cwd=base)

        legacy = invoke()
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(json.loads(legacy.stdout)["result"]["mode"], "legacy")
        self.assertFalse(unexpected_queries.exists())

        profile.write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
            '[project_memory]\nmode = "r2"\n')
        blocked = invoke()
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(blocked.stdout, "")
        self.assertFalse(unexpected_queries.exists())

    def test_native_adapter_script_context_is_verified_without_session_impersonation(self) -> None:
        installed, source, environment = self.native_adapter_fixture(script=True)
        context = {"task_id": "task-native", "node_id": "node-script"}
        environment = {"PATH": environment["PATH"]}
        result = self.adapter_call(installed, source, None, "resolve-task", interface="workflow",
                                   native_environment=environment, native_context=context)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["mode"], "r2")
        forged = self.adapter_call(installed, source, None, "resolve-task", interface="workflow",
                                   native_environment=environment,
                                   native_context={**context, "node_id": "foreign"})
        self.assertEqual(forged.returncode, 2)
        impersonated = self.adapter_call(
            installed, source, None, "initialize-task", interface="workflow",
            native_environment=environment, native_context=context)
        self.assertEqual(impersonated.returncode, 2)

    def test_native_adapter_pinned_cutover_routes_existing_and_new_tasks(self) -> None:
        installed, source, environment = self.native_adapter_fixture()
        initialized = self.adapter_call(
            installed, source, None, "initialize-task", interface="workflow",
            native_environment=environment)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        task = self.store._task("OSM-130")
        task_path = self.store.path / "tasks/OSM-130/task.json"
        initial_binding = task_path.read_bytes()
        self.assertEqual(task["external_refs"], [{
            "namespace": "kent", "project_id": "project-test", "task_id": "task-native",
        }])
        self.assertEqual(json.loads(initialized.stdout)["result"]["task_uid"], task["task_uid"])
        retry = self.adapter_call(
            installed, source, None, "initialize-task", interface="workflow",
            native_environment=environment)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(task_path.read_bytes(), initial_binding)
        published = self.adapter_call(
            installed, source, None, "publish", interface="workflow",
            native_environment=environment, document_key="tasks/OSM-130/plan",
            expected_revision=None, content="Initial plan", operation_id="plan-1", provenance=[])
        self.assertEqual(published.returncode, 0, published.stderr)
        revision = json.loads(published.stdout)["result"]
        readback = self.adapter_call(
            installed, source, None, "read", interface="workflow",
            native_environment=environment, document_key="tasks/OSM-130/plan")
        self.assertEqual(readback.returncode, 0, readback.stderr)
        self.assertEqual(json.loads(readback.stdout)["result"], revision)
        resolved = self.adapter_call(
            installed, source, None, "resolve-plan", interface="workflow",
            native_environment=environment, plan_path=revision["path"])
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["result"]["working_revision"],
                         revision["token"])
        script_context = {"task_id": "task-native", "node_id": "node-accept"}
        accepted_result = self.adapter_call(
            installed, source, None, "accept-plan", interface="workflow",
            native_environment={"PATH": environment["PATH"]}, native_context=script_context,
            expected_revision=revision["token"], expected_accepted=None,
            metadata={"work_kind": "feature", "authority_refs": ["human:fixture"]})
        self.assertEqual(accepted_result.returncode, 0, accepted_result.stderr)
        accepted = self.store.check_plan("OSM-130")
        accepted_token = json.loads(accepted_result.stdout)["result"]["token"]
        self.assertEqual(accepted["accepted_snapshot"], accepted_token)
        checked = self.adapter_call(
            installed, source, None, "check-plan", interface="workflow",
            native_environment=environment, coder_selection=None)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        checked_result = json.loads(checked.stdout)["result"]
        self.assertEqual(checked_result["comparison"], "unchanged")
        self.assertEqual(checked_result["accepted_snapshot"], accepted_token)
        self.assertEqual(checked_result["cache_ref"], {
            "schema": "r2-plan-contract-cache-v1", "task_short_id": "OSM-130",
            "project_uid": "project-a",
            "task_uid": task["task_uid"],
            "task_binding_sha256": hashlib.sha256(initial_binding).hexdigest(),
            "accepted_snapshot_path": str(self.store.check_plan("OSM-130")["accepted_path"]),
            "accepted_snapshot_sha256": accepted_token})
        self.assertFalse((source / ".kent/runtime/OSM-130/plan-contract.json").exists())
        foreign = self.adapter_call(
            installed, source, None, "publish", interface="workflow",
            native_environment=environment, document_key="tasks/OTHER/plan",
            expected_revision=None, content="Wrong task", operation_id="wrong", provenance=[])
        self.assertEqual(foreign.returncode, 2)
        self.assertFalse((self.store.path / "tasks/OTHER").exists())
        admitted = self.adapter_call(installed, source, None, "resolve-mcp",
                                     native_environment=environment)
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        self.assertEqual(json.loads(admitted.stdout)["result"]["mode"], "r2")
        without_task_id = self.adapter_call(
            installed, source, None, "resolve-mcp",
            native_environment={key: value for key, value in environment.items()
                                if key != "KENT_TASK_ID"})
        self.assertEqual(without_task_id.returncode, 0, without_task_id.stderr)
        self.assertEqual(json.loads(without_task_id.stdout)["result"]["mode"], "r2")
        self.assertFalse((source / ".todo").exists())
        original_receipt = self.receipt.copy()
        self.receipt["source_commit"] = "not-a-commit"
        self.pin_adapter_receipt()
        self.write_adapter_binding()
        blocked = self.adapter_call(installed, source, None, "resolve-mcp",
                                    native_environment=environment)
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(blocked.stdout, "")
        self.receipt = original_receipt
        self.pin_adapter_receipt()
        self.write_adapter_binding()
        profile = source / ".kent/workflow-profile.toml"
        profile.write_text(profile.read_text() + "\n# changed after qualification\n")
        changed = self.adapter_call(installed, source, None, "resolve-mcp",
                                    native_environment=environment)
        self.assertEqual(changed.returncode, 2)

    def test_native_helper_knowledge_publish_preserves_accepted_plan_digest(self) -> None:
        installed, source, environment = self.native_adapter_fixture()
        initialized = self.adapter_call(
            installed, source, None, "initialize-task", interface="workflow",
            native_environment=environment)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

        plan_result = self.adapter_call(
            installed, source, None, "publish", interface="workflow",
            native_environment=environment, document_key="tasks/OSM-130/plan",
            expected_revision=None, content="Accepted scope\n",
            operation_id="accepted-plan", provenance=[])
        self.assertEqual(plan_result.returncode, 0, plan_result.stderr)
        plan = json.loads(plan_result.stdout)["result"]
        accepted_result = self.adapter_call(
            installed, source, None, "accept-plan", interface="workflow",
            native_environment={"PATH": environment["PATH"]},
            native_context={"task_id": "task-native", "node_id": "node-accept"},
            expected_revision=plan["token"], expected_accepted=None,
            metadata={"work_kind": "feature", "authority_refs": ["human:fixture"]})
        self.assertEqual(accepted_result.returncode, 0, accepted_result.stderr)
        accepted = json.loads(accepted_result.stdout)["result"]
        accepted_token = accepted["token"]
        accepted_snapshot = Path(accepted["path"]).read_bytes()

        knowledge_result = self.adapter_call(
            installed, source, None, "publish", interface="workflow",
            native_environment=environment, document_key="knowledge/research",
            expected_revision=None, content="Verified research fact\n",
            operation_id="research-fact", provenance=["fixture:research"])
        self.assertEqual(knowledge_result.returncode, 0, knowledge_result.stderr)
        knowledge = json.loads(knowledge_result.stdout)["result"]
        readback = self.adapter_call(
            installed, source, None, "read", interface="workflow",
            native_environment=environment, document_key="knowledge/research")
        self.assertEqual(readback.returncode, 0, readback.stderr)
        self.assertEqual(json.loads(readback.stdout)["result"], knowledge)
        self.assertEqual(Path(knowledge["path"]).read_text(), "Verified research fact\n")

        head = self.adapter_call(
            installed, source, None, "accepted-head", interface="workflow",
            native_environment=environment)
        self.assertEqual(head.returncode, 0, head.stderr)
        self.assertEqual(json.loads(head.stdout)["result"]["accepted_snapshot"], accepted_token)
        check = self.adapter_call(
            installed, source, None, "check-plan", interface="workflow",
            native_environment=environment, coder_selection=None)
        self.assertEqual(check.returncode, 0, check.stderr)
        checked = json.loads(check.stdout)["result"]
        self.assertEqual(checked["comparison"], "unchanged")
        self.assertEqual(checked["accepted_snapshot"], accepted_token)
        self.assertEqual(Path(accepted["path"]).read_bytes(), accepted_snapshot)
        self.assertFalse((source / ".todo").exists())

    def test_native_adapter_resolves_exact_standalone_binding(self) -> None:
        installed, source, binding, binding_path = self.adapter_fixture()
        result = self.adapter_call(installed, source, binding, "resolve-mcp")
        self.assertEqual(result.returncode, 0, result.stderr)
        resolved = json.loads(result.stdout)
        self.assertEqual(resolved["schema"], "r2-mcp-response-v1")
        self.assertEqual(resolved["result"], {"mode": "r2", "binding_ref": {
            "path": str(binding_path), "sha256": binding["task_binding_sha256"]}})

    def test_native_adapter_rejects_installed_core_symlink(self) -> None:
        installed, source, binding, _ = self.adapter_fixture()
        core = installed / "workflow_project_memory.py"
        retained = installed / "original.py"
        core.rename(retained)
        core.symlink_to(retained)
        result = self.adapter_call(installed, source, binding, "resolve-mcp")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_native_adapter_rejects_profile_directory_symlink(self) -> None:
        installed, source, binding, _ = self.adapter_fixture()
        profile_dir = source / ".kent"
        retained = source / "profile-source"
        profile_dir.rename(retained)
        profile_dir.symlink_to(retained, target_is_directory=True)
        result = self.adapter_call(installed, source, binding, "resolve-mcp")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_native_adapter_allocates_and_binds_invocation(self) -> None:
        installed, source, binding, binding_path = self.adapter_fixture()
        locator = {"path": str(binding_path), "sha256": binding["task_binding_sha256"]}
        allocated = self.adapter_call(installed, source, binding, "new-invocation",
                                      binding_ref=locator)
        self.assertEqual(allocated.returncode, 0, allocated.stderr)
        invocation = json.loads(allocated.stdout)["result"]["invocation_id"]
        metadata = {"operation": "call", "server": "example", "tool": "read",
                    "config_sha256": "a" * 64, "output_mode": "json",
                    "safe_mode": True, "save_raw": False}
        begun = self.adapter_call(installed, source, binding, "begin-invocation",
                                  binding_ref=locator, invocation_id=invocation,
                                  request_sha256="b" * 64, permitted_metadata=metadata)
        self.assertEqual(begun.returncode, 0, begun.stderr)
        start = json.loads(begun.stdout)["result"]
        self.assertEqual(start["state"], "started")
        read = self.adapter_call(installed, source, binding, "read-invocation",
                                 binding_ref=locator, invocation_id=invocation)
        self.assertEqual(read.returncode, 0, read.stderr)
        observed = json.loads(read.stdout)["result"]
        self.assertEqual(observed["state"], "unknown")
        self.assertNotIn(start["owner_token"], read.stdout)
        self.assertNotIn("owner_sha256", read.stdout)

    def test_native_adapter_rejects_stale_or_foreign_standalone_binding(self) -> None:
        installed, source, binding, _ = self.adapter_fixture()
        for field, value in (("task_uid", "foreign"), ("project_uid", "foreign"),
                             ("project_path", "/tmp/untrusted"),
                             ("task_binding_sha256", "0" * 64),
                             ("task_key", "../OSM-130")):
            with self.subTest(field=field):
                result = self.adapter_call(installed, source, {**binding, field: value},
                                           "resolve-mcp")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(str(self.root), result.stderr)
        self.assertFalse((source / ".todo").exists())
        self.assertFalse((self.store.path / "tasks/OSM-130/artifacts").exists())

    def test_native_adapter_raw_artifact_privacy_and_exact_binary(self) -> None:
        installed, source, binding, binding_path = self.adapter_fixture()
        locator = {"path": str(binding_path), "sha256": binding["task_binding_sha256"]}
        raw = b"\x00secret\xff\n"
        digest = hashlib.sha256(raw).hexdigest()
        for save_raw in (False, True):
            invocation = self.store.new_invocation("OSM-130")["invocation_id"]
            metadata = {"operation": "call", "server": "example", "tool": "read",
                        "config_sha256": "a" * 64, "output_mode": "json",
                        "safe_mode": True, "save_raw": save_raw}
            owner = self.store.begin_invocation(
                "OSM-130", invocation, "b" * 64,
                permitted_metadata=metadata)["owner_token"]
            result = self.adapter_call(installed, source, binding, "put-artifact",
                                       binding_ref=locator, invocation_id=invocation,
                                       owner_token=owner, kind="raw", sha256=digest,
                                       byte_size=len(raw), raw_body=raw)
            self.assertEqual(result.returncode, 0 if save_raw else 2, result.stderr)
            self.assertNotIn(b"secret", result.stdout + result.stderr)
            if save_raw:
                artifact = json.loads(result.stdout)["result"]
                self.assertEqual(Path(artifact["object_path"]).read_bytes(), raw)
            else:
                self.assertFalse((self.store.path / "tasks/OSM-130/artifacts/mcp/objects").exists())

    def test_native_adapter_cache_rejects_non_schema_invocation(self) -> None:
        installed, source, binding, binding_path = self.adapter_fixture()
        locator = {"path": str(binding_path), "sha256": binding["task_binding_sha256"]}
        invocation = self.store.new_invocation("OSM-130")["invocation_id"]
        metadata = {"operation": "call", "server": "example", "tool": "read",
                    "config_sha256": "a" * 64, "output_mode": "json",
                    "safe_mode": True, "save_raw": True}
        owner = self.store.begin_invocation("OSM-130", invocation, "b" * 64,
                                            permitted_metadata=metadata)["owner_token"]
        artifact = self.store.put_artifact("OSM-130", b'{"secret":"not-schema"}')
        result = self.adapter_call(installed, source, binding, "publish-cache",
                                   binding_ref=locator, invocation_id=invocation,
                                   owner_token=owner, cache_key="c" * 64,
                                   object_sha256=artifact.sha256)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.store.read_cache("OSM-130", "c" * 64), {"state": "miss"})

    def test_native_adapter_rejects_schema_body_and_digest_before_publication(self) -> None:
        installed, source, binding, binding_path = self.adapter_fixture()
        locator = {"path": str(binding_path), "sha256": binding["task_binding_sha256"]}
        invocation = self.store.new_invocation("OSM-130")["invocation_id"]
        metadata = {"operation": "list", "server": "example", "tool": "list-schema",
                    "config_sha256": "a" * 64, "output_mode": "json",
                    "safe_mode": False, "save_raw": False}
        owner = self.store.begin_invocation("OSM-130", invocation, "b" * 64,
                                            permitted_metadata=metadata)["owner_token"]
        for body, digest in ((b"not json", hashlib.sha256(b"not json").hexdigest()),
                             (b"{}", "0" * 64)):
            result = self.adapter_call(installed, source, binding, "put-artifact",
                                       binding_ref=locator, invocation_id=invocation,
                                       owner_token=owner, kind="schema", sha256=digest,
                                       byte_size=len(body), raw_body=body)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.store.path / "tasks/OSM-130/artifacts/mcp/objects").exists())
        body = b'{"tools":[]}'
        result = self.adapter_call(installed, source, binding, "put-artifact",
                                   binding_ref=locator, invocation_id=invocation,
                                   owner_token=owner, kind="schema",
                                   sha256=hashlib.sha256(body).hexdigest(),
                                   byte_size=len(body), raw_body=body)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_mcp_wrappers_with_installed_adapter_and_core(self) -> None:
        installed, source, binding, _ = self.adapter_fixture()
        executable = installed / "workflow-project-memory"
        home = self.home
        adapter_link = self.bin / "kent-project-memory"
        self.assertTrue(adapter_link.is_symlink())
        self.assertEqual(adapter_link.resolve(), executable.resolve())
        counter = self.bin / "calls"
        mcporter = self.bin / "mcporter"
        mcporter.write_text(f"""#!{sys.executable}
from pathlib import Path
with Path({str(counter)!r}).open("a") as output:
    output.write("call\\n")
print('{{"tools":[],"ok":true}}')
""")
        mcporter.chmod(0o700)
        repository = Path(__file__).resolve().parents[1]
        environment = {
            "HOME": str(home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
        }

        def invoke(name, args, token, *, task_locator=None):
            identity_args = (["--task-locator", task_locator] if task_locator is not None
                             else ["--standalone-binding", json.dumps(binding)])
            return subprocess.run(
                ["bash", str(repository / "adapters/mcp" / name), *args,
                 *identity_args, "--invocation-id", token],
                cwd=source,
                env={**environment, **({"KENT_SESSION_ID": "session-native"}
                                       if task_locator is not None else {})},
                capture_output=True, text=True, timeout=120)

        invocation = self.store.new_invocation("OSM-130")["invocation_id"]
        call_args = ["example.inspect", "--output", "json", "--allow-mutate"]
        result = invoke("kent-mcp-call", call_args, invocation)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(self.store.read_invocation("OSM-130", invocation)["state"], "finished")
        duplicate = invoke("kent-mcp-call", call_args, invocation)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(counter.read_text().splitlines(), ["call"])
        for _ in range(2):
            token = self.store.new_invocation("OSM-130")["invocation_id"]
            listed = invoke("kent-mcp-list", ["example", "--schema"], token)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)["tools"], [])
            self.assertEqual(self.store.read_invocation("OSM-130", token)["state"], "finished")
        self.assertEqual(counter.read_text().splitlines(), ["call", "call"])
        self.assertFalse((source / ".todo").exists())
        self.assertFalse((source / "build/mcp-cache").exists())

        self.native_task["summary"]["short_id"] = "OSM-131"
        self._write_adapter_fake_kent()
        self.store.initialize_task(
            "OSM-131", "native-wrapper-uid",
            [{"namespace": "kent", "project_id": "project-test",
              "task_id": "task-native"}])
        native_invocation = self.store.new_invocation("OSM-131")["invocation_id"]
        native_result = invoke("kent-mcp-call", call_args, native_invocation,
                               task_locator="task-native")
        self.assertEqual(native_result.returncode, 0, native_result.stderr)
        self.assertEqual(
            self.store.read_invocation("OSM-131", native_invocation)["state"], "finished")
        self.assertEqual(counter.read_text().splitlines(), ["call", "call", "call"])

    def test_real_mcp_session_only_wrapper_avoids_unrelated_corrupt_task_detail(self) -> None:
        installed, source, _, _ = self.adapter_fixture(seed_standalone=False)
        self.native_task["worktree_path"] = str(source)
        self.extra_native_tasks = [{
            "summary": {"id": "task-unrelated", "project_id": "project-test",
                        "short_id": "OSM-102", "workflow_id": "workflow-native"},
            "worktree_path": None, "live_sessions": [], "current_nodes": [],
        }]
        self.invalid_utf8_task_ids = {"task-unrelated"}
        self.native_worktree_status = {
            "target": {
                "workspace_id": "workspace-test",
                "workspace_name": "Fixture",
                "workspace_root": str(source),
                "workspace_availability": "available",
                "worktree": {
                    "id": "worktree-native", "name": "OSM-130",
                    "root": str(source), "availability": "available",
                },
                "cwd_relpath": ".",
                "effective_workdir": str(source),
            },
            "worktree": {
                "recorded_root": str(source),
                "observed_root": str(source),
                "display_name": "OSM-130",
                "recorded_branch_ref": "refs/heads/task-OSM-130",
            },
            "problems": [],
        }
        self.native_project_list_root = "./"
        self._write_adapter_fake_kent()
        self.store.initialize_task(
            "OSM-130", "native-wrapper-uid",
            [{"namespace": "kent", "project_id": "project-test",
              "task_id": "task-native"}])

        result = self.adapter_call(
            installed, source, None, "resolve-mcp",
            native_environment={
                "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                "KENT_SESSION_ID": "session-native",
            })

        calls = [json.loads(line) for line in self.native_command_log.read_text().splitlines()]
        self.assertEqual(result.returncode, 0, f"{result.stderr}; native calls={calls!r}")
        self.assertEqual(json.loads(result.stdout)["result"]["mode"], "r2")
        self.assertEqual(
            [call for call in calls if call[:2] == ["worktree", "status"]],
            [["worktree", "status", "--session", "session-native", "--json"]])
        self.assertEqual(
            self.native_task_show_log.read_text().splitlines(),
            ["task-native", "task-native"])

    def test_concurrent_real_mcp_wrappers_share_invocation_and_replay_exact_artifact(self) -> None:
        installed, source, binding, binding_path = self.adapter_fixture()
        executable = installed / "workflow-project-memory"
        adapter_link = self.bin / "kent-project-memory"
        self.assertTrue(adapter_link.is_symlink())
        self.assertEqual(adapter_link.resolve(), executable.resolve())

        temp_path = Path(self.temp.name).resolve()
        counter = temp_path / "concurrent-mcp-calls"
        release = temp_path / "release-mcporter"
        mcporter = self.bin / "mcporter"
        payload = b'{"result":"single-effect"}\n'
        mcporter.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            "import sys, time\n"
            f"counter = Path({str(counter)!r})\n"
            f"release = Path({str(release)!r})\n"
            "with counter.open('a') as output:\n"
            "    output.write('call\\n')\n"
            "deadline = time.monotonic() + 30\n"
            "while not release.exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise SystemExit('test release was not signaled')\n"
            "    time.sleep(0.01)\n"
            f"sys.stdout.buffer.write({payload!r})\n")
        mcporter.chmod(0o700)
        repository = Path(__file__).resolve().parents[1]
        environment = {
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
        }
        invocation = self.store.new_invocation("OSM-130")["invocation_id"]
        call_args = [
            "example.inspect", "--output", "json", "--allow-mutate", "--save-raw",
        ]
        start = threading.Barrier(3)

        def call_same_invocation():
            start.wait(timeout=30)
            return subprocess.run(
                ["bash", str(repository / "adapters/mcp/kent-mcp-call"),
                 *call_args, "--standalone-binding", json.dumps(binding),
                 "--invocation-id", invocation],
                cwd=source, env=environment, capture_output=True, text=False, timeout=120)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call_same_invocation) for _ in range(2)]
            start.wait(timeout=30)
            try:
                completed, _ = wait(futures, timeout=30, return_when=FIRST_COMPLETED)
                self.assertEqual(len(completed), 1, "one concurrent retry should be refused")
            finally:
                release.write_text("go\n")
            results = [future.result(timeout=120) for future in futures]

        self.assertEqual(counter.read_text().splitlines(), ["call"])
        self.assertEqual(sorted(result.returncode for result in results), [0, 5])
        winner = next(result for result in results if result.returncode == 0)
        refused = next(result for result in results if result.returncode != 0)
        self.assertEqual(winner.stdout, payload)
        self.assertIn(b"mcp_invocation_unknown", refused.stderr)
        self.assertEqual(refused.stdout, b"")

        locator = {"path": str(binding_path), "sha256": binding["task_binding_sha256"]}
        readback = self.adapter_call(
            installed, source, binding, "read-invocation",
            binding_ref=locator, invocation_id=invocation)
        self.assertEqual(readback.returncode, 0, readback.stderr)
        recorded = json.loads(readback.stdout)["result"]
        self.assertEqual(recorded["state"], "finished")
        self.assertEqual(recorded["exit_code"], 0)
        self.assertEqual(recorded["error_code"], "")
        self.assertEqual(recorded["permitted_metadata"]["save_raw"], True)
        self.assertEqual(Path(recorded["object_path"]).read_bytes(), payload)
        self.assertEqual(recorded["object_sha256"], hashlib.sha256(payload).hexdigest())

        duplicate = subprocess.run(
            ["bash", str(repository / "adapters/mcp/kent-mcp-call"),
             *call_args, "--standalone-binding", json.dumps(binding),
             "--invocation-id", invocation],
            cwd=source, env=environment, capture_output=True, text=False, timeout=120)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn(b"mcp_invocation_finished", duplicate.stderr)
        self.assertEqual(counter.read_text().splitlines(), ["call"])
        repeated_readback = self.adapter_call(
            installed, source, binding, "read-invocation",
            binding_ref=locator, invocation_id=invocation)
        self.assertEqual(repeated_readback.returncode, 0, repeated_readback.stderr)
        self.assertEqual(json.loads(repeated_readback.stdout)["result"], recorded)

    def test_cli_invocation_metadata_and_cache_miss(self) -> None:
        invocation = self.store.new_invocation("OSM-130")["invocation_id"]
        metadata = {"operation": "list", "server": "example", "tool": "",
                    "config_sha256": "a" * 64, "output_mode": "json",
                    "safe_mode": True, "save_raw": False}
        begun = self.cli({"operation": "begin-invocation", "task_key": "OSM-130",
                          "invocation_id": invocation, "request_sha256": "b" * 64,
                          "permitted_metadata": metadata})
        self.assertEqual(begun.returncode, 0, begun.stderr)
        self.assertEqual(json.loads(begun.stdout)["result"]["started"]["permitted_metadata"],
                         metadata)
        cache = self.cli({"operation": "read-cache", "task_key": "OSM-130",
                          "cache_key": "c" * 64})
        self.assertEqual(cache.returncode, 0, cache.stderr)
        self.assertEqual(json.loads(cache.stdout)["result"], {"state": "miss"})

    def test_invocation_artifact_requires_owner_before_any_write(self) -> None:
        invocation = self.store.new_invocation("OSM-130")["invocation_id"]
        owner = self.store.begin_invocation("OSM-130", invocation, "a" * 64)["owner_token"]
        objects = self.store.path / "tasks/OSM-130/artifacts/mcp/objects"
        with self.assertRaises(Conflict):
            self.store.put_invocation_artifact("OSM-130", invocation, "b" * 64, b"secret")
        self.assertFalse(objects.exists())
        artifact = self.store.put_invocation_artifact("OSM-130", invocation, owner, b"exact")
        self.assertEqual(artifact.path.read_bytes(), b"exact")
        self.store.finish_invocation("OSM-130", invocation, owner, exit_code=0,
                                    error_code="", object_sha256=artifact.sha256)
        with self.assertRaises(Conflict):
            self.store.put_invocation_artifact("OSM-130", invocation, owner, b"late")

    def test_read_empty_invocation_directory_does_not_create_lock(self) -> None:
        invocation = self.store.new_invocation("OSM-130")
        directory = Path(invocation["started_path"]).parent
        directory.mkdir(parents=True, mode=0o700)
        for parent in (directory.parent, directory.parent.parent, directory.parent.parent.parent):
            parent.chmod(0o700)
        self.assertEqual(self.store.read_invocation(
            "OSM-130", invocation["invocation_id"]), {"state": "not-started"})
        self.assertEqual(list(directory.iterdir()), [])

    def test_partial_identity_write_is_not_published_and_exact_retry_succeeds(self) -> None:
        original = os.write
        calls = 0

        def interrupted_write(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(fd, data[:5])
            raise OSError("simulated interrupted write")

        with patch("workflowkit.project_memory.os.write", side_effect=interrupted_write):
            with self.assertRaises(OSError):
                self.store.initialize_task("OSM-131", "task-b", [])
        self.assertFalse((self.store.path / "tasks/OSM-131/task.json").exists())
        task = self.store.initialize_task("OSM-131", "task-b", [])
        self.assertEqual(task["task_uid"], "task-b")

    def test_acceptance_validates_snapshot_semantics_not_only_filename_hash(self) -> None:
        plan = self.store.publish("tasks/OSM-130/plan", None, "Plan\n")
        accepted = self.store.accept_plan(
            "OSM-130", plan.token, None,
            {"work_kind": "implementation", "authority_refs": ["human:1"]})
        snapshot = json.loads(accepted.path.read_bytes())
        snapshot["normalized_sha256"] = "0" * 64
        raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        forged = accepted.path.with_name(digest + ".json")
        forged.write_bytes(raw)
        forged.chmod(0o600)
        head = accepted.path.parent.parent / "accepted-head.json"
        head.write_text(json.dumps({"schema": "r2-head-v1", "revision": digest}))
        with self.assertRaises(MemoryError):
            self.store.check_plan("OSM-130")

    def test_separate_processes_have_one_cas_winner(self) -> None:
        initial = self.store.publish("knowledge/process-race", None, "initial")
        program = """
import sys
from pathlib import Path
from workflowkit.project_memory import ProjectMemory, Conflict
store = ProjectMemory(Path(sys.argv[1]), "appsome", "project-a")
print("ready", flush=True)
sys.stdin.readline()
try:
    store.publish("knowledge/process-race", sys.argv[2], sys.argv[3])
except Conflict:
    sys.exit(3)
"""
        processes = [
            subprocess.Popen([sys.executable, "-c", program, str(self.root),
                              initial.token, value], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for value in ("first", "second")
        ]
        try:
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), "ready")
            for process in processes:
                process.stdin.write("go\n")
                process.stdin.flush()
            results = [process.communicate(timeout=15) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 3],
                             results)
            self.assertEqual(initial.path.read_text(), "initial")
            self.assertIn(self.store.read("knowledge/process-race").path.read_text(),
                          ("first", "second"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_numbered_and_heading_checkbox_progress_preserves_acceptance(self) -> None:
        plan = self.store.publish("tasks/OSM-130/plan", None, "1. [ ] One\n## [ ] Two\n")
        self.store.accept_plan("OSM-130", plan.token, None,
                               {"work_kind": "implementation", "authority_refs": ["human:1"]})
        self.store.publish("tasks/OSM-130/plan", plan.token, "1. [x] One\n## [X] Two\n")
        self.assertEqual(self.store.check_plan("OSM-130")["comparison"], "unchanged")

    def test_cache_requires_active_owner_and_verified_object(self) -> None:
        key = "a" * 64
        self.assertEqual(self.store.read_cache("OSM-130", key), {"state": "miss"})
        invocation = self.store.new_invocation("OSM-130")["invocation_id"]
        owner = self.store.begin_invocation("OSM-130", invocation, "b" * 64)["owner_token"]
        artifact = self.store.put_artifact("OSM-130", b'{"schema":true}')
        with self.assertRaises(Conflict):
            self.store.publish_cache("OSM-130", key, artifact.sha256, invocation, "c" * 64)
        self.store.publish_cache("OSM-130", key, artifact.sha256, invocation, owner)
        self.assertEqual(self.store.read_cache("OSM-130", key)["object_sha256"], artifact.sha256)
        self.store.finish_invocation("OSM-130", invocation, owner,
                                    exit_code=0, error_code="", object_sha256=artifact.sha256)
        with self.assertRaises(Conflict):
            self.store.publish_cache("OSM-130", key, artifact.sha256, invocation, owner)
        artifact.path.write_bytes(b"corrupt")
        with self.assertRaises(MemoryError):
            self.store.read_cache("OSM-130", key)

    def test_invocation_metadata_is_closed_and_bound_to_retry(self) -> None:
        invocation = self.store.new_invocation("OSM-130")["invocation_id"]
        metadata = {"operation": "list", "server": "example", "tool": "",
                    "config_sha256": "a" * 64, "output_mode": "json",
                    "safe_mode": True, "save_raw": False}
        started = self.store.begin_invocation(
            "OSM-130", invocation, "b" * 64, permitted_metadata=metadata)
        self.assertEqual(started["started"]["permitted_metadata"], metadata)
        with self.assertRaises(Conflict):
            self.store.begin_invocation("OSM-130", invocation, "b" * 64,
                                        permitted_metadata={**metadata, "save_raw": True})
        with self.assertRaises(MemoryError):
            self.store.begin_invocation(
                "OSM-130", self.store.new_invocation("OSM-130")["invocation_id"],
                "b" * 64, permitted_metadata={**metadata, "arguments": "secret"})

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "R2"
        self.store = ProjectMemory.initialize(self.root, "appsome", "project-a")
        self.task = self.store.initialize_task("OSM-130", "task-a", [])

    def test_project_identity_cannot_be_replaced(self) -> None:
        with self.assertRaises(Conflict):
            ProjectMemory.initialize(self.root, "appsome", "other-project")

    def test_read_publish_preserves_immutable_history(self) -> None:
        first = self.store.publish("knowledge/api", None, "First\n")
        second = self.store.publish("knowledge/api", first.token, "Second\n")
        self.assertEqual(first.path.read_text(), "First\n")
        self.assertEqual(second.path.read_text(), "Second\n")
        self.assertEqual(self.store.read("knowledge/api").token, second.token)
        self.assertEqual(second.sha256, hashlib.sha256(b"Second\n").hexdigest())

    def test_stale_writer_cannot_overwrite(self) -> None:
        first = self.store.publish("knowledge/api", None, "First")
        self.store.publish("knowledge/api", first.token, "Second")
        with self.assertRaises(Conflict):
            self.store.publish("knowledge/api", first.token, "Lost update")

    def test_same_content_does_not_allow_aba(self) -> None:
        first = self.store.publish("knowledge/api", None, "A")
        second = self.store.publish("knowledge/api", first.token, "B")
        third = self.store.publish("knowledge/api", second.token, "A")
        self.assertNotEqual(first.token, third.token)
        with self.assertRaises(Conflict):
            self.store.publish("knowledge/api", first.token, "Stale")

    def test_two_writers_only_one_publishes(self) -> None:
        first = self.store.publish("knowledge/api", None, "Initial")
        ready = threading.Barrier(2)

        def write(value: str) -> str:
            ready.wait()
            try:
                return self.store.publish("knowledge/api", first.token, value).token
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ("A", "B")))
        self.assertEqual(results.count("conflict"), 1)
        self.assertIn(self.store.read("knowledge/api").path.read_text(), ("A", "B"))

    def test_tampered_revision_is_rejected(self) -> None:
        revision = self.store.publish("knowledge/api", None, "Original")
        revision.path.write_text("Tampered")
        with self.assertRaises(MemoryError):
            self.store.read("knowledge/api")

    def test_foreign_task_identity_rejected(self) -> None:
        with self.assertRaises(Conflict):
            self.store.initialize_task("OSM-130", "foreign-task", [])

    def test_unsafe_document_keys_rejected(self) -> None:
        for key in ("../outside", "knowledge/../escape", "/tmp/escape",
                    "tasks/foreign/plan", "knowledge/a/b"):
            with self.subTest(key=key), self.assertRaises(MemoryError):
                self.store.publish(key, None, "No")

    def test_document_symlink_rejected(self) -> None:
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (self.root / "appsome" / "knowledge" / "unsafe").symlink_to(external)
        with self.assertRaises(MemoryError):
            self.store.publish("knowledge/unsafe", None, "No")
        self.assertEqual(list(external.iterdir()), [])

    def test_revision_hardlink_rejected(self) -> None:
        revision = self.store.publish("knowledge/api", None, "Original")
        os.link(revision.path, Path(self.temp.name) / "alias")
        with self.assertRaises(MemoryError):
            self.store.read("knowledge/api")

    def test_private_permissions(self) -> None:
        revision = self.store.publish("knowledge/api", None, "Private")
        self.assertEqual(revision.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(revision.path.parent.stat().st_mode & 0o777, 0o700)

    def test_acceptance_does_not_follow_working_plan(self) -> None:
        key = "tasks/OSM-130/plan"
        first = self.store.publish(key, None, "- [ ] Deliver\n")
        self.assertEqual(self.store.plan_revision("OSM-130", str(first.path)), first)
        for wrong in (str(first.path).replace("/plan/", "/report/"),
                      str(first.path).replace("OSM-130", "OTHER"),
                      str(first.path.parent / ".." / first.path.parent.name / "content.md")):
            with self.assertRaises(ValueError):
                self.store.plan_revision("OSM-130", wrong)
        accepted = self.store.accept_plan(
            "OSM-130", first.token, None,
            {"work_kind": "feature", "authority_refs": ["original:user:1"]},
        )
        second = self.store.publish(key, first.token, "- [x] Deliver\n")
        status = self.store.check_plan("OSM-130")
        self.assertEqual(status["comparison"], "unchanged")
        self.assertEqual(status["working_revision"], second.token)
        self.assertEqual(status["accepted_snapshot"], accepted.token)
        self.assertEqual(status["accepted_path"], str(accepted.path))
        self.assertEqual(status["accepted"]["revision_token"], first.token)
        self.assertEqual(status["accepted"]["authority_refs"], ["original:user:1"])
        status["accepted"]["authority_refs"].append("untrusted:mutation")
        self.assertEqual(self.store.check_plan("OSM-130")["accepted"]["authority_refs"],
                         ["original:user:1"])
        self.store.publish(key, second.token, "- [ ] Different scope\n")
        self.assertEqual(self.store.check_plan("OSM-130")["comparison"], "changed")
        self.assertEqual(
            json.loads(accepted.path.read_text())["revision_token"], first.token
        )

    def test_accept_rejects_changed_working_head(self) -> None:
        key = "tasks/OSM-130/plan"
        first = self.store.publish(key, None, "First")
        self.store.publish(key, first.token, "Second")
        with self.assertRaises(Conflict):
            self.store.accept_plan(
                "OSM-130", first.token, None,
                {"work_kind": "feature", "authority_refs": ["original:user:1"]},
            )

    def test_crash_before_head_keeps_previous_revision(self) -> None:
        first = self.store.publish("knowledge/api", None, "First")

        def crash(phase: str) -> None:
            if phase == "revision_durable":
                raise OSError("simulated crash")

        with self.assertRaises(OSError):
            self.store.publish("knowledge/api", first.token, "Second", phase_hook=crash)
        self.assertEqual(self.store.read("knowledge/api").token, first.token)

    def test_publish_retry_settles_same_invocation_after_head_crash(self) -> None:
        operation = "publish-1"

        def crash(phase: str) -> None:
            if phase == "head_published":
                raise OSError("lost response")

        with self.assertRaises(OSError):
            self.store.publish("knowledge/api", None, "First",
                               operation_id=operation, phase_hook=crash)
        published = self.store.read("knowledge/api")
        settled = self.store.publish("knowledge/api", None, "First",
                                     operation_id=operation)
        self.assertEqual(settled.token, published.token)
        with self.assertRaises(Conflict):
            self.store.publish("knowledge/api", None, "Different",
                               operation_id=operation)

    def test_publish_retry_after_intervening_change_conflicts(self) -> None:
        first = self.store.publish("knowledge/api", None, "First", operation_id="first")
        self.store.publish("knowledge/api", first.token, "Second")
        with self.assertRaises(Conflict):
            self.store.publish("knowledge/api", None, "First", operation_id="first")

    def test_source_references_remain_immutable(self) -> None:
        intake = self.store.initialize_task("intake-a", "intake-uid", [])
        sources = [{"project_uid": "project-a", "task_uid": intake["task_uid"],
                    "sha256": hashlib.sha256(json.dumps(
                        intake, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest()}]
        task = self.store.initialize_task("OSM-131", "task-b", [], source_refs=sources)
        self.assertEqual(task["source_refs"], sources)
        with self.assertRaises(Conflict):
            self.store.initialize_task("OSM-131", "task-b", [], source_refs=[])

    def test_duplicate_identity_json_rejected(self) -> None:
        path = self.root / "appsome" / "tasks" / "OSM-130" / "task.json"
        path.write_text('{"schema":"r2-task-v1","schema":"r2-task-v1"}')
        with self.assertRaises(MemoryError):
            self.store.publish("tasks/OSM-130/plan", None, "No")

    def test_accept_rejects_stale_accepted_head(self) -> None:
        key = "tasks/OSM-130/plan"
        working = self.store.publish(key, None, "First")
        self.store.accept_plan(
            "OSM-130", working.token, None,
            {"work_kind": "feature", "authority_refs": ["original:user:1"]},
        )
        with self.assertRaises(Conflict):
            self.store.accept_plan(
                "OSM-130", working.token, None,
                {"work_kind": "feature", "authority_refs": ["original:user:2"]},
            )

    def test_accept_and_publish_share_one_lock(self) -> None:
        key = "tasks/OSM-130/plan"
        first = self.store.publish(key, None, "First")
        ready = threading.Barrier(2)

        def publish() -> None:
            ready.wait()
            self.store.publish(key, first.token, "Second")

        def accept() -> str:
            ready.wait()
            try:
                self.store.accept_plan(
                    "OSM-130", first.token, None,
                    {"work_kind": "feature", "authority_refs": ["original:user:1"]},
                )
                return "accepted-first"
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            publisher, accepter = pool.submit(publish), pool.submit(accept)
            publisher.result()
            outcome = accepter.result()
        if outcome == "accepted-first":
            self.assertEqual(self.store.check_plan("OSM-130")["comparison"], "changed")
        else:
            self.assertFalse((self.root / "appsome/tasks/OSM-130/accepted-head.json").exists())

    def cli(self, request: dict) -> subprocess.CompletedProcess[str]:
        script = Path(__file__).resolve().parents[1] / "scripts/project-memory"
        return subprocess.run(
            [sys.executable, str(script), "--root", str(self.root),
             "--project", "appsome", "--project-uid", "project-a"],
            input=json.dumps({"schema": "r2-request-v1", **request}),
            capture_output=True, text=True, check=False,
            env={"PATH": os.defpath, "HOME": self.temp.name},
        )

    def cli_frame(self, request: dict, body: bytes | None = None
                  ) -> subprocess.CompletedProcess[bytes]:
        payload = json.dumps(
            {"schema": "r2-request-v1", **request},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if body is not None:
            payload += b"\n" + body
        return self.cli_raw(payload)

    def cli_raw(self, payload: bytes) -> subprocess.CompletedProcess[bytes]:
        script = Path(__file__).resolve().parents[1] / "scripts/project-memory"
        return subprocess.run(
            [sys.executable, str(script), "--root", str(self.root),
             "--project", "appsome", "--project-uid", "project-a"],
            input=payload, capture_output=True, check=False,
            env={"PATH": os.defpath, "HOME": self.temp.name},
        )

    def test_pre_native_intake_retries_exactly_and_never_discovers_by_name(self) -> None:
        intent = {"source": "jira:APP-17", "summary": "Prepare invoices"}
        invocation = str(uuid.uuid4())
        first = self.store.initialize_intake(invocation, intent)
        self.assertEqual(first["task_key"], f"intake-{invocation}")
        self.assertEqual(first["task_uid"], invocation)
        self.assertEqual(first["external_refs"], [])
        self.assertEqual(self.store._task(first["task_key"])["external_refs"], [])
        self.assertEqual(self.store.initialize_intake(invocation, intent), first)
        with self.assertRaises(Conflict):
            self.store.initialize_intake(
                invocation, {**intent, "summary": "Different request"})

        separate = self.store.initialize_intake(str(uuid.uuid4()), intent)
        self.assertNotEqual(first["task_key"], separate["task_key"])
        self.assertNotEqual(first["task_uid"], separate["task_uid"])
        self.assertTrue((self.store.path / "tasks" / first["task_key"] / "intake.json").is_file())
        self.assertTrue((self.store.path / "tasks" / separate["task_key"] / "intake.json").is_file())

    def test_intake_uuid4_json_bounds_and_nonfinite_values_are_enforced(self) -> None:
        with self.assertRaisesRegex(MemoryError, "UUIDv4"):
            self.store.initialize_intake(str(uuid.uuid1()), {"summary": "No"})
        with self.assertRaisesRegex(MemoryError, "object"):
            self.store.initialize_intake(str(uuid.uuid4()), "not an object")
        with self.assertRaises(MemoryError):
            self.store.initialize_intake(str(uuid.uuid4()), {"score": float("nan")})
        with self.assertRaises(MemoryError):
            self.store.initialize_intake(str(uuid.uuid4()), {"oversized": "x" * MAX_BYTES})
        self.assertFalse(any((self.store.path / "tasks").glob("intake-*")))

    def test_concurrent_intakes_are_idempotent_per_invocation_and_distinct_between_starts(self) -> None:
        intent = {"source": "jira:APP-17", "summary": "Same visible name"}
        same_id = str(uuid.uuid4())
        other_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
        ready = threading.Barrier(4)

        def initialize(invocation_id: str) -> dict:
            ready.wait()
            return self.store.initialize_intake(invocation_id, intent)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(initialize, (same_id, same_id, *other_ids)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len({result["task_key"] for result in results}), 3)
        for result in results:
            self.assertEqual(
                json.loads((self.store.path / "tasks" / result["task_key"] / "task.json").read_bytes())
                ["external_refs"], [])

    def test_interrupted_intake_recovers_only_exact_intent(self) -> None:
        for phase in ("intake_durable", "task_durable"):
            with self.subTest(phase=phase):
                invocation = str(uuid.uuid4())
                intent = {"summary": "Exact retry", "source": "manual"}

                def interrupt(current: str) -> None:
                    if current == phase:
                        raise RuntimeError("simulated interruption")

                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    self.store.initialize_intake(invocation, intent, phase_hook=interrupt)
                with self.assertRaises(Conflict):
                    self.store.initialize_intake(invocation, {**intent, "summary": "Changed"})
                recovered = self.store.initialize_intake(invocation, intent)
                self.assertEqual(recovered["task_uid"], invocation)
                self.assertEqual(self.store._task(recovered["task_key"])["external_refs"], [])

    def test_process_death_during_intake_is_exactly_recoverable(self) -> None:
        program = """
import json, os, sys
from pathlib import Path
from workflowkit.project_memory import ProjectMemory
store = ProjectMemory(Path(sys.argv[1]), "appsome", "project-a")
store.initialize_intake(
    sys.argv[2], json.loads(sys.argv[3]),
    phase_hook=lambda phase: os._exit(77) if phase == sys.argv[4] else None)
"""
        for phase in ("intake_durable", "task_durable"):
            with self.subTest(phase=phase):
                invocation = str(uuid.uuid4())
                intent = {"source": "manual", "summary": "recover this exact intake"}
                process = subprocess.run(
                    [sys.executable, "-c", program, str(self.root), invocation,
                     json.dumps(intent), phase],
                    capture_output=True, timeout=15,
                )
                self.assertEqual(process.returncode, 77, process.stderr)
                with self.assertRaises(Conflict):
                    self.store.initialize_intake(
                        invocation, {**intent, "summary": "different request"})
                recovered = self.store.initialize_intake(invocation, intent)
                self.assertEqual(recovered["task_key"], f"intake-{invocation}")
                self.assertEqual(self.store._task(recovered["task_key"])["external_refs"], [])

    def test_intake_will_not_claim_existing_task_identity_or_unsafe_directory(self) -> None:
        intake = self.store.initialize_intake(str(uuid.uuid4()), {"summary": "separate identity"})
        with self.assertRaises(Conflict):
            self.store.initialize_task(intake["task_key"], None, [])

        invocation = str(uuid.uuid4())
        task_key = f"intake-{invocation}"
        task_dir = self.store.path / "tasks" / task_key
        task_dir.mkdir(mode=0o700)
        (task_dir / "task.json").write_text(json.dumps({
            "schema": "r2-task-v1", "project_uid": "project-a", "task_key": task_key,
            "task_uid": invocation, "external_refs": [],
        }))
        (task_dir / "task.json").chmod(0o600)
        with self.assertRaises(Conflict):
            self.store.initialize_intake(invocation, {"summary": "Not ours"})

        external = Path(self.temp.name) / "outside"
        external.mkdir()
        unsafe_id = str(uuid.uuid4())
        (self.store.path / "tasks" / f"intake-{unsafe_id}").symlink_to(
            external, target_is_directory=True)
        with self.assertRaises(MemoryError):
            self.store.initialize_intake(unsafe_id, {"summary": "Unsafe"})
        self.assertEqual(list(external.iterdir()), [])

    def test_attachments_are_binary_safe_content_addressed_and_immutable(self) -> None:
        content = b"\x00binary\xffattachment\n"
        expected = hashlib.sha256(content).hexdigest()
        first = self.store.put_attachment("OSM-130", content)
        retry = self.store.put_attachment("OSM-130", content)
        self.assertEqual(first, retry)
        self.assertEqual(first.token, expected)
        self.assertEqual(first.path, self.store.path /
                         f"tasks/OSM-130/artifacts/attachments/objects/{expected}")
        self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.read_attachment("OSM-130", expected), content)
        with self.assertRaises(MemoryError):
            self.store.read_attachment("OSM-130", "../outside")
        first.path.write_bytes(b"tampered")
        with self.assertRaisesRegex(MemoryError, "digest mismatch"):
            self.store.read_attachment("OSM-130", expected)

    def test_attachment_publication_is_bounded_private_and_concurrent(self) -> None:
        with self.assertRaises(MemoryError):
            self.store.put_attachment("OSM-130", b"x" * (MAX_BYTES + 1))
        with self.assertRaises(MemoryError):
            self.store.put_attachment("../outside", b"no")
        self.assertFalse((self.store.path / "tasks/OSM-130/artifacts").exists())

        payload = b"\x00" * 4096 + b"\xfe"
        ready = threading.Barrier(2)

        def write(_: int) -> str:
            ready.wait()
            return self.store.put_attachment("OSM-130", payload).sha256

        with ThreadPoolExecutor(max_workers=2) as pool:
            digests = list(pool.map(write, (1, 2)))
        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual(digests, [digest, digest])
        path = self.store.path / f"tasks/OSM-130/artifacts/attachments/objects/{digest}"
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_process_death_during_attachment_write_never_publishes_partial_bytes(self) -> None:
        payload = b"\x00partial attachment body\xff"
        digest = hashlib.sha256(payload).hexdigest()
        program = """
import os, sys
from pathlib import Path
from workflowkit.project_memory import ProjectMemory
raw = bytes((0,)) + b"partial attachment body" + bytes((255,))
original = os.write
def interrupted(fd, data):
    original(fd, data[:3])
    os._exit(77)
os.write = interrupted
ProjectMemory(Path(sys.argv[1]), "appsome", "project-a").put_attachment("OSM-130", raw)
"""
        process = subprocess.run(
            [sys.executable, "-c", program, str(self.root)],
            capture_output=True, timeout=15,
        )
        self.assertEqual(process.returncode, 77, process.stderr)
        object_path = self.store.path / f"tasks/OSM-130/artifacts/attachments/objects/{digest}"
        self.assertFalse(object_path.exists())
        published = self.store.put_attachment("OSM-130", payload)
        self.assertEqual(published.token, digest)
        self.assertEqual(self.store.read_attachment("OSM-130", digest), payload)

    def test_cli_intake_retry_and_binary_attachment_protocol_without_kent(self) -> None:
        intent = {"source": "jira:APP-18", "summary": "Binary request"}
        invocation = str(uuid.uuid4())
        initialize = {"operation": "initialize-intake", "invocation_id": invocation,
                      "intent": intent}
        first = self.cli(initialize)
        self.assertEqual(first.returncode, 0, first.stderr)
        binding = json.loads(first.stdout)["result"]
        retried = self.cli(initialize)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(json.loads(retried.stdout)["result"], binding)
        conflict = self.cli({**initialize, "intent": {**intent, "summary": "Changed"}})
        self.assertEqual(conflict.returncode, 3)
        self.assertEqual(conflict.stdout, "")
        self.assertFalse((Path(self.temp.name) / ".kent").exists())
        self.assertFalse((self.store.path / ".todo").exists())
        for malformed in (
                b'{"schema":"r2-request-v1","operation":"initialize-intake",'
                b'"invocation_id":"' + str(uuid.uuid4()).encode() +
                b'","intent":{"summary":"one","summary":"two"}}',
                b'{"schema":"r2-request-v1","operation":"initialize-intake",'
                b'"invocation_id":"' + str(uuid.uuid4()).encode() +
                b'","intent":{"score":NaN}}'):
            result = self.cli_raw(malformed)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")

        content = b"\x00\xffattachment\r\n\x80"
        digest = hashlib.sha256(content).hexdigest()
        write = self.cli_frame({
            "operation": "artifact-write", "task_key": binding["task_key"],
            "sha256": digest, "byte_size": len(content),
        }, content)
        self.assertEqual(write.returncode, 0, write.stderr)
        response_line, separator, response_body = write.stdout.partition(b"\n")
        self.assertTrue(separator)
        write_response = json.loads(response_line)
        self.assertEqual(write_response, {
            "schema": "r2-artifact-response-v1", "operation": "artifact-write",
            "object_sha256": digest, "byte_size": len(content),
            "object_path": str(self.store.path /
                               f"tasks/{binding['task_key']}/artifacts/attachments/objects/{digest}"),
        })
        self.assertEqual(response_body, b"")
        read = self.cli_frame({
            "operation": "artifact-read", "task_key": binding["task_key"], "sha256": digest,
        })
        self.assertEqual(read.returncode, 0, read.stderr)
        read_line, separator, read_body = read.stdout.partition(b"\n")
        self.assertTrue(separator)
        self.assertEqual(json.loads(read_line), {
            "schema": "r2-artifact-response-v1", "operation": "artifact-read",
            "object_sha256": digest, "byte_size": len(content),
            "object_path": str(self.store.path /
                               f"tasks/{binding['task_key']}/artifacts/attachments/objects/{digest}"),
        })
        self.assertEqual(read_body, content)

    def test_cli_artifact_write_rejects_bad_digest_size_and_framing_before_publication(self) -> None:
        content = b"\x00raw"
        valid_digest = hashlib.sha256(content).hexdigest()
        cases = (
            (valid_digest, len(content) - 1, content),
            ("0" * 64, len(content), content),
            (valid_digest, len(content), content[:-1]),
            (valid_digest, len(content), content + b"trailing"),
        )
        for digest, size, body in cases:
            with self.subTest(digest=digest[:8], size=size, body_length=len(body)):
                result = self.cli_frame({
                    "operation": "artifact-write", "task_key": "OSM-130",
                    "sha256": digest, "byte_size": size,
                }, body)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
        self.assertFalse((self.store.path / "tasks/OSM-130/artifacts").exists())

    def test_standalone_cli_roundtrip_without_kent(self) -> None:
        published = self.cli({"operation": "publish", "document_key": "knowledge/api",
                              "expected_revision": None, "content": "Independent",
                              "operation_id": "cli-publication"})
        self.assertEqual(published.returncode, 0, published.stderr)
        result = json.loads(published.stdout)
        readback = self.cli({"operation": "read", "document_key": "knowledge/api"})
        self.assertEqual(readback.returncode, 0, readback.stderr)
        self.assertEqual(json.loads(readback.stdout)["result"]["token"],
                         result["result"]["token"])
        self.assertEqual(Path(result["result"]["path"]).read_text(), "Independent")
        self.assertFalse((Path(self.temp.name) / ".kent").exists())

    def test_cli_unknown_fields_fail_before_writes(self) -> None:
        response = self.cli({"operation": "publish", "document_key": "knowledge/api",
                             "expected_revision": None, "content": "No",
                             "operation_id": "bad", "force": True})
        self.assertNotEqual(response.returncode, 0)
        self.assertEqual(response.stdout, "")
        self.assertFalse((self.root / "appsome/knowledge/api").exists())

    def test_invalid_document_type_is_a_controlled_error(self) -> None:
        response = self.cli({"operation": "read", "document_key": ["knowledge/api"]})
        self.assertNotEqual(response.returncode, 0)
        self.assertNotIn("Traceback", response.stderr)

    def test_external_reference_read_validation(self) -> None:
        path = self.root / "appsome/tasks/OSM-130/task.json"
        value = json.loads(path.read_text())
        value["external_refs"] = [{"namespace": "kent", "force": True}]
        path.write_text(json.dumps(value))
        with self.assertRaises(MemoryError):
            self.store.publish("tasks/OSM-130/plan", None, "No")

    def test_provenance_is_bound_to_revision(self) -> None:
        revision = self.store.publish(
            "knowledge/api", None, "Observed fact",
            provenance=["source:commit:file:12"],
        )
        metadata = json.loads((revision.path.parent / "revision.json").read_text())
        self.assertEqual(metadata["provenance"], ["source:commit:file:12"])

    def test_unknown_acceptance_fields_rejected(self) -> None:
        working = self.store.publish("tasks/OSM-130/plan", None, "Plan")
        with self.assertRaises(MemoryError):
            self.store.accept_plan(
                "OSM-130", working.token, None,
                {"work_kind": "feature", "authority_refs": ["source:1"],
                 "authorized": True},
            )

    def test_revision_size_limit_has_no_published_head(self) -> None:
        with self.assertRaises(MemoryError):
            self.store.publish("knowledge/api", None, "a" * (8 * 1024 * 1024 + 1))
        self.assertFalse((self.root / "appsome/knowledge/api/head.json").exists())

    def test_invocation_token_is_known_before_start(self) -> None:
        invocation = self.store.new_invocation("OSM-130")
        self.assertFalse(Path(invocation["started_path"]).exists())
        first = self.store.begin_invocation(
            "OSM-130", invocation["invocation_id"], "a" * 64
        )
        self.assertEqual(first["state"], "started")
        repeated = self.store.begin_invocation(
            "OSM-130", invocation["invocation_id"], "a" * 64
        )
        self.assertEqual(repeated["state"], "unknown")
        self.assertNotIn("owner_token", repeated)
        with self.assertRaises(Conflict):
            self.store.begin_invocation(
                "OSM-130", invocation["invocation_id"], "b" * 64
            )

    def test_concurrent_invocation_claim_only_one_owner(self) -> None:
        invocation = self.store.new_invocation("OSM-130")
        ready = threading.Barrier(2)

        def begin(_: int) -> str:
            ready.wait()
            return self.store.begin_invocation(
                "OSM-130", invocation["invocation_id"], "a" * 64
            )["state"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(begin, (1, 2)))
        self.assertEqual(sorted(states), ["started", "unknown"])

    def test_invocation_receipt_references_only_published_object(self) -> None:
        invocation = self.store.new_invocation("OSM-130")
        started = self.store.begin_invocation(
            "OSM-130", invocation["invocation_id"], "a" * 64
        )
        raw = self.store.put_artifact("OSM-130", b'{"result":"exact"}')
        receipt = self.store.finish_invocation(
            "OSM-130", invocation["invocation_id"], started["owner_token"],
            exit_code=0, error_code="", object_sha256=raw.sha256,
        )
        self.assertEqual(receipt["object_sha256"], raw.sha256)
        self.assertEqual(self.store.read_invocation(
            "OSM-130", invocation["invocation_id"]
        )["state"], "finished")
        self.assertEqual(self.store.begin_invocation(
            "OSM-130", invocation["invocation_id"], "a" * 64
        )["state"], "finished")

    def test_invocation_finish_rejects_wrong_owner_and_missing_object(self) -> None:
        invocation = self.store.new_invocation("OSM-130")
        started = self.store.begin_invocation(
            "OSM-130", invocation["invocation_id"], "a" * 64
        )
        with self.assertRaises(Conflict):
            self.store.finish_invocation(
                "OSM-130", invocation["invocation_id"], "b" * 64,
                exit_code=0, error_code="", object_sha256=None,
            )
        with self.assertRaises(MemoryError):
            self.store.finish_invocation(
                "OSM-130", invocation["invocation_id"], started["owner_token"],
                exit_code=0, error_code="", object_sha256="c" * 64,
            )
        self.assertFalse(Path(invocation["finished_path"]).exists())

    def test_concurrent_identical_objects_are_idempotent(self) -> None:
        ready = threading.Barrier(2)
        data = b"artifact" * 100000

        def write(_: int) -> str:
            ready.wait()
            return self.store.put_artifact("OSM-130", data).sha256

        with ThreadPoolExecutor(max_workers=2) as pool:
            hashes = list(pool.map(write, (1, 2)))
        self.assertEqual(hashes, [hashlib.sha256(data).hexdigest()] * 2)

    def test_read_before_invocation_start_does_not_write(self) -> None:
        invocation = self.store.new_invocation("OSM-130")
        self.assertEqual(self.store.read_invocation(
            "OSM-130", invocation["invocation_id"]
        )["state"], "not-started")
        self.assertFalse(Path(invocation["started_path"]).parent.exists())

    def test_cli_invocation_receipt_correlation(self) -> None:
        allocated = self.cli({"operation": "new-invocation", "task_key": "OSM-130"})
        self.assertEqual(allocated.returncode, 0, allocated.stderr)
        invocation = json.loads(allocated.stdout)["result"]
        begun = self.cli({"operation": "begin-invocation", "task_key": "OSM-130",
                          "invocation_id": invocation["invocation_id"],
                          "request_sha256": "a" * 64})
        self.assertEqual(begun.returncode, 0, begun.stderr)
        start = json.loads(begun.stdout)["result"]
        finished = self.cli({"operation": "finish-invocation", "task_key": "OSM-130",
                             "invocation_id": invocation["invocation_id"],
                             "owner_token": start["owner_token"], "exit_code": 0,
                             "error_code": "", "object_sha256": None})
        self.assertEqual(finished.returncode, 0, finished.stderr)
        readback = self.cli({"operation": "read-invocation", "task_key": "OSM-130",
                             "invocation_id": invocation["invocation_id"]})
        self.assertEqual(readback.returncode, 0, readback.stderr)
        self.assertEqual(json.loads(readback.stdout)["result"]["state"], "finished")


if __name__ == "__main__":
    unittest.main()
