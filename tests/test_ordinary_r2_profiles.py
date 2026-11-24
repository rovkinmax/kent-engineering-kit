from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "adapters/ordinary-r2/profile_source.py"
INSTALLER = ROOT / "scripts/install-ordinary-r2"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import test_project_memory as _test_project_memory
from workflowkit.project_memory import Conflict, ProjectMemory

loader = importlib.machinery.SourceFileLoader("ordinary_r2_profile_source", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
profile_source = importlib.util.module_from_spec(spec)
loader.exec_module(profile_source)


def built_facade():
    installer = runpy.run_path(str(INSTALLER))
    outputs, _inputs = installer["build_files"](ROOT)
    module = types.ModuleType("ordinary_r2_facade_fixture")
    module.__file__ = str(ROOT / "adapters/ordinary-r2/runtime/kent-project-memory")
    exec(compile(outputs["bin/kent-project-memory"], module.__file__, "exec"),
         module.__dict__)
    return module


class ProfileSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.primary = {}
        self.settings = {}
        self.memory = self.root / "R2"
        self.memory.mkdir(mode=0o700)
        self.records = {}
        self.reads = []
        self.records_by_slug = {}
        for slug, project_key, project_uid in (
            ("kit", "kent-engineering-kit", "kit-uid"),
            ("appsome", "appsome", "appsome-uid"),
        ):
            primary = self.root / f"primary-{slug}"
            settings = self.root / f"settings-{slug}"
            primary.mkdir()
            (settings / ".kent").mkdir(parents=True)
            self.primary[slug] = primary
            self.settings[slug] = settings
            self.records_by_slug[slug] = self.enrollment(
                slug, project_key, project_uid, primary, settings,
            )

    def enrollment(self, slug, project_key, project_uid, primary, settings):
        project_id = f"project-{slug}"
        workspace_id = f"workspace-{slug}"
        binding_sha = hashlib.sha256(f"binding-{slug}".encode()).hexdigest()
        memory = {
            "root": str(self.memory),
            "project_key": project_key,
            "project_uid": project_uid,
            "legacy_task_ids": [f"legacy-{slug}"],
        }
        enrollment = {
            "schema": profile_source.ENROLLMENT_SCHEMA,
            "project_id": project_id,
            "primary_workspace_id": workspace_id,
            "primary_root": str(primary),
            "settings_root": str(settings),
            "v2_binding_sha256": binding_sha,
        }
        raw_enrollment = json.dumps(
            enrollment, sort_keys=True, separators=(",", ":"),
        ).encode()
        profile = (
            "[project_memory]\n"
            f'root = "{self.memory}"\n'
            f'project_key = "{project_key}"\n'
            f'project_uid = "{project_uid}"\n'
            'mode = "r2"\n\n'
            '[commands]\n'
            'plan_contract_accept = ".kent/scripts/accept-plan"\n'
        ).encode()
        enrollment_path = (
            self.home / ".kent/ordinary-r2/enrollments" / f"{project_id}.json"
        )
        profile_path = settings / ".kent/workflow-profile.toml"
        self.records[enrollment_path] = raw_enrollment
        self.records[profile_path] = profile
        return project_id, workspace_id, binding_sha, memory, raw_enrollment

    def reader(self, _core, path, *, private, maximum):
        self.reads.append((path, private, maximum))
        raw = self.records[path]
        if len(raw) > maximum:
            raise ValueError("fixture record too large")
        return raw

    def resolve(self, slug):
        project_id, workspace_id, binding_sha, memory, _raw = self.records_by_slug[slug]
        return profile_source.read_bound_profile(
            project_id, self.primary[slug], workspace_id, binding_sha, memory,
            reader=self.reader, home=self.home,
        )

    def test_shared_root_resolves_to_distinct_project_and_task_namespaces(self):
        kit = self.resolve("kit")
        appsome = self.resolve("appsome")
        self.assertEqual(kit["config"]["root"], appsome["config"]["root"])
        self.assertEqual(kit["config"]["project_key"], "kent-engineering-kit")
        self.assertEqual(appsome["config"]["project_key"], "appsome")
        self.assertEqual(kit["config"]["project_uid"], "kit-uid")
        self.assertEqual(appsome["config"]["project_uid"], "appsome-uid")
        self.assertEqual(kit["settings_root"], str(self.settings["kit"]))
        self.assertEqual(appsome["settings_root"], str(self.settings["appsome"]))
        self.assertEqual(kit["profile"]["commands"]["plan_contract_accept"],
                         ".kent/scripts/accept-plan")

        stores = []
        for result, slug, key, uid in (
            (kit, "kit", "kent-engineering-kit", "kit-uid"),
            (appsome, "appsome", "appsome", "appsome-uid"),
        ):
            self.assertEqual(result["config"]["project_key"], key)
            store = ProjectMemory.initialize(
                Path(result["config"]["root"]), key, uid,
            )
            task_key = f"task-{slug}"
            store.initialize_task(task_key, f"uid-{slug}", [])
            first = store.publish(
                f"tasks/{task_key}/plan", None, "before\n",
                operation_id=f"first-{slug}",
            )
            second = store.publish(
                f"tasks/{task_key}/plan", first.token, "after\n",
                operation_id=f"second-{slug}",
            )
            with self.assertRaises(Conflict):
                store.publish(
                    f"tasks/{task_key}/plan", first.token, "stale\n",
                    operation_id=f"stale-{slug}",
                )
            self.assertEqual(first.path.read_text(), "before\n")
            self.assertEqual(second.path.read_text(), "after\n")
            self.assertEqual(store.read(f"tasks/{task_key}/plan").token, second.token)
            stores.append((store, task_key, second.token))

        self.assertNotEqual(stores[0][0].project_uid, stores[1][0].project_uid)
        self.assertEqual(stores[0][0].read(f"tasks/{stores[0][1]}/plan").token,
                         stores[0][2])
        self.assertEqual(stores[1][0].read(f"tasks/{stores[1][1]}/plan").token,
                         stores[1][2])

    def test_wrong_native_identity_or_historical_binding_fails_before_profile_read(self):
        enrollment_path = self.home / ".kent/ordinary-r2/enrollments/project-kit.json"
        raw = self.records[enrollment_path]
        expected = self.records_by_slug["kit"]
        for project_id, primary, workspace, binding in (
            ("project-appsome", self.primary["kit"], "workspace-kit", expected[2]),
            ("project-kit", self.primary["appsome"], "workspace-kit", expected[2]),
            ("project-kit", self.primary["kit"], "workspace-appsome", expected[2]),
            ("project-kit", self.primary["kit"], "workspace-kit", "0" * 64),
        ):
            self.records[enrollment_path] = raw
            self.reads.clear()
            with self.assertRaises(ValueError):
                profile_source.read_bound_profile(
                    project_id, primary, workspace, binding, expected[3],
                    reader=self.reader, home=self.home,
                )
            self.assertEqual(len(self.reads), 1)

    def test_swapped_profile_namespace_and_invalid_enrollment_are_rejected(self):
        project_id, workspace_id, binding_sha, memory, _raw = (
            self.records_by_slug["kit"]
        )
        enrollment_path = self.home / ".kent/ordinary-r2/enrollments/project-kit.json"
        profile_path = self.settings["kit"] / ".kent/workflow-profile.toml"
        original = self.records[profile_path]
        self.records[profile_path] = original.replace(b"kit-uid", b"appsome-uid")
        with self.assertRaisesRegex(ValueError, "differs from installation"):
            profile_source.read_bound_profile(
                project_id, self.primary["kit"], workspace_id, binding_sha, memory,
                reader=self.reader, home=self.home,
            )

        self.records[profile_path] = original
        self.records[enrollment_path] = (
            b'{"schema":"ordinary-r2-enrollment-v1","schema":"ordinary-r2-enrollment-v1"}'
        )
        with self.assertRaisesRegex(ValueError, "enrollment"):
            profile_source.read_bound_profile(
                project_id, self.primary["kit"], workspace_id, binding_sha, memory,
                reader=self.reader, home=self.home,
            )

    def test_profile_roots_must_be_canonical_and_disjoint(self):
        enrollment_path = self.home / ".kent/ordinary-r2/enrollments/project-kit.json"
        profile_path = self.settings["kit"] / ".kent/workflow-profile.toml"
        (self.primary["kit"] / "nested").mkdir()
        raw = self.records[enrollment_path]
        enrollment = json.loads(raw)
        for settings_root in (
            str(self.primary["kit"]),
            str(self.primary["kit"] / "nested"),
            str(self.settings["appsome"] / ".." / "settings-kit"),
        ):
            changed = {**enrollment, "settings_root": settings_root}
            self.records[enrollment_path] = json.dumps(changed).encode()
            with self.assertRaises(ValueError):
                profile_source.read_bound_profile(
                    "project-kit", self.primary["kit"], "workspace-kit",
                    hashlib.sha256(b"binding-kit").hexdigest(),
                    self.records_by_slug["kit"][3], reader=self.reader, home=self.home,
                )
        self.records[enrollment_path] = raw
        self.assertIn(profile_path, self.records)


class OrdinaryFacadeTests(unittest.TestCase):
    def test_session_workspace_is_checked_before_standalone_project_selection(self):
        facade = built_facade()
        workspace = Path("/synthetic/ordinary-r2-project-b")
        request = {
            "schema": "r2-mcp-request-v1",
            "operation": "resolve-mcp",
            "workspace_path": str(workspace),
            "standalone_binding": {
                "project_path": "/synthetic/R2/project-b",
                "project_uid": "project-b",
                "task_key": "standalone-b",
                "task_uid": "task-b",
                "task_binding_sha256": "b" * 64,
            },
        }

        def foreign_session(*_args, **_kwargs):
            raise ValueError("native Session workspace differs")

        project_lookup = Mock(side_effect=AssertionError(
            "project identity was selected before Session ownership"))
        with patch.dict(os.environ, {"KENT_SESSION_ID": "session-a"}, clear=True), \
                patch.dict(facade.__dict__, {
                    "canonical_root": lambda _path: workspace,
                    "git_root": lambda _path: workspace,
                    "native_worktree_task_hint": foreign_session,
                    "native_project_id": project_lookup,
                }):
            with self.assertRaisesRegex(ValueError, "Session workspace differs"):
                facade.preflight_source_admission("mcp", request)
        project_lookup.assert_not_called()

        no_worktree_status = {
            "problems": [],
            "target": {
                "worktree": None,
                "workspace_availability": "available",
                "workspace_id": "workspace-a",
                "workspace_root": "/synthetic/ordinary-r2-project-a",
                "effective_workdir": "/synthetic/ordinary-r2-project-a",
            },
            "worktree": {
                "recorded_root": "/synthetic/ordinary-r2-project-a",
                "observed_root": "/synthetic/ordinary-r2-project-a",
            },
        }
        with patch.dict(facade.__dict__, {
                "canonical_root": lambda value: Path(value),
                "native_json": lambda *_args: no_worktree_status,
        }):
            with self.assertRaisesRegex(ValueError, "Session workspace differs"):
                facade.native_worktree_task_hint(
                    workspace, "session-a", strict_workspace=True,
                )

    def test_resolve_task_and_legacy_mcp_preserve_authenticated_settings_root(self):
        facade = built_facade()
        external = "/synthetic/settings/appsome"
        facade.PREADMISSION = {"mode": "legacy", "settings_root": external}
        workflow_request = {
            "schema": "r2-workflow-request-v1",
            "operation": "resolve-task",
            "workspace_path": "/synthetic/appsome",
        }
        core = sys.modules[ProjectMemory.__module__]
        with patch.dict(facade.__dict__, {
                "resolve_native": lambda *_args, **_kwargs: (None, None, None)}):
            self.assertEqual(
                facade.workflow_dispatch(core, workflow_request),
                {"mode": "legacy", "binding_ref": None, "settings_root": external},
            )
        class Store:
            project_uid = "project-a"
            path = Path("/synthetic/R2/appsome")

            @staticmethod
            def _task(_task):
                return {"task_uid": "task-a"}

        facade.PREADMISSION = {
            "mode": "r2", "settings_root": external,
            "config": {"project_key": "appsome"},
        }
        with patch.dict(facade.__dict__, {
                "resolve_native": lambda *_args, **_kwargs: (
                    Store(), "task-a", {"path": "/synthetic/task.json",
                                        "sha256": "e" * 64})}):
            resolved = facade.workflow_dispatch(core, workflow_request)
        self.assertEqual(resolved["settings_root"], external)
        self.assertEqual(resolved["settings_project_key"], "appsome")

        mcp_request = {
            "schema": "r2-mcp-request-v1",
            "operation": "resolve-mcp",
            "workspace_path": "/synthetic/appsome",
        }
        self.assertEqual(
            facade.legacy_response("mcp", mcp_request),
            {"mode": "legacy", "binding_ref": None, "settings_root": external,
             "settings_project_key": "appsome"},
        )

    def test_acceptance_authority_does_not_follow_mutable_profile_selector(self):
        facade = built_facade()
        core = sys.modules[ProjectMemory.__module__]
        primary = Path("/synthetic/appsome")
        workspace = primary / "worktree"
        task = {
            "summary": {"id": "task-a", "workflow_id": "workflow-a"},
            "source_workspace": {
                "root_path": str(primary),
                "is_primary": True,
                "workspace_id": "workspace-a",
            },
            "project": {"default_workspace_id": "workspace-a"},
            "worktree_path": str(workspace),
        }
        request = {
            "workspace_path": str(workspace),
            "native_context": {"task_id": "task-a", "node_id": "accept-node"},
        }
        config = {
            "root": "/synthetic/R2",
            "project_key": "appsome",
            "project_uid": "project-a",
            "mode": "r2",
        }
        facade.PREADMISSION = {
            "mode": "r2",
            "standalone": False,
            "project_id": "project-a",
            "workspace": str(workspace),
            "primary_root": str(primary),
            "task_id": "task-a",
            "primary_workspace_id": "workspace-a",
            "config": config,
            "settings_root": "/synthetic/settings/appsome",
            "enrollment_sha256": "c" * 64,
            "binding_sha256": "d" * 64,
            "receipt": {"project_memory": config},
        }
        profile_selector = ".kent/scripts/unrelated-current-script"
        bound_profile = Mock(return_value={
            "config": config,
            "settings_root": facade.PREADMISSION["settings_root"],
            "enrollment_sha256": facade.PREADMISSION["enrollment_sha256"],
            "profile": {"commands": {"plan_contract_accept": profile_selector}},
        })
        graph_path = [profile_selector]

        def native_read(arguments, _workspace):
            if arguments[0] == "project":
                return b"project-a\n"
            return json.dumps({
                "workflow_id": "workflow-a",
                "graph": {"nodes": [{
                    "id": "accept-node",
                    "kind": "script",
                    "script_path": graph_path[0],
                }]},
            }).encode()

        overrides = {
            "git_root": lambda value: Path(value),
            "native_project_id": lambda _path: "project-a",
            "native_task_context": lambda _request, _workspace, _project: (
                task, request["native_context"], None),
            "native_read": native_read,
            "assert_preadmission": lambda *_args, **_kwargs: None,
            "confirm_source_binding": lambda *_args, **_kwargs: None,
            "bound_profile": bound_profile,
        }
        with patch.dict(os.environ, {}, clear=True), \
                patch.dict(facade.__dict__, overrides):
            with self.assertRaisesRegex(ValueError, "does not own acceptance"):
                facade.require_acceptance_script(core, request)
            graph_path[0] = ".kent/scripts/workflow-plan-contract-accept"
            facade.require_acceptance_script(core, request)
        self.assertEqual(bound_profile.call_count, 3)


class InstallerTests(unittest.TestCase):
    def test_installs_idempotently_only_in_disposable_home_and_refuses_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            home.mkdir(mode=0o700)
            kent = home / ".kent"
            kent.mkdir(mode=0o700)
            env = {**os.environ, "HOME": str(home)}

            checked = subprocess.run(
                [sys.executable, str(INSTALLER), "--check"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(json.loads(checked.stdout)["verified"])
            self.assertFalse((kent / "ordinary-r2").exists())

            first = subprocess.run(
                [sys.executable, str(INSTALLER), "--apply"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            manifest = json.loads(first.stdout)
            destination = kent / "ordinary-r2"
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual(sorted(path.name for path in (destination / "bin").iterdir()),
                             ["kent-mcp-call", "kent-mcp-list",
                              "kent-project-memory", "mcp-common.sh", "profile_source.py"])
            self.assertEqual(list((destination / "enrollments").iterdir()), [])
            self.assertFalse((kent / "bin").exists())
            self.assertEqual(manifest["effects"], {
                "public_aliases_changed": False,
                "primary_workspaces_changed": False,
                "project_profiles_changed": False,
                "r2_data_changed": False,
            })

            second = subprocess.run(
                [sys.executable, str(INSTALLER), "--apply"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["files"], manifest["files"])

            target = destination / "bin/kent-project-memory"
            original = target.read_bytes()
            target.write_bytes(original + b"\n# unexpected local change\n")
            refused = subprocess.run(
                [sys.executable, str(INSTALLER), "--apply"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("destination differs", refused.stderr)
            self.assertEqual(target.read_bytes(), original + b"\n# unexpected local change\n")


class KitWorkflowEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _test_project_memory.ProjectMemoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_actual_kit_workflow_entry_uses_external_profile_and_protected_cas(self):
        fixture = self.fixture
        _installed, primary, native_environment = fixture.native_adapter_fixture()
        memory = {
            "root": str(fixture.root),
            "project_key": "kent-engineering-kit",
            "project_uid": "bd9ed394-c5df-4eff-9363-6e0616a54f9a",
            "legacy_task_ids": [],
        }
        fixture.receipt["project_memory"] = memory
        fixture.pin_adapter_receipt()
        fixture.write_adapter_binding()

        install = subprocess.run(
            [sys.executable, str(INSTALLER), "--apply"],
            env={**os.environ, "HOME": str(fixture.home)},
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        sidecar = fixture.home / ".kent/ordinary-r2/bin/kent-project-memory"
        settings = fixture.root.parent / "kit-settings"
        (settings / ".kent").mkdir(parents=True)
        profile_path = settings / ".kent/workflow-profile.toml"
        profile_path.write_text(
            "[project_memory]\n"
            f'root = "{fixture.root}"\n'
            'project_key = "kent-engineering-kit"\n'
            'project_uid = "bd9ed394-c5df-4eff-9363-6e0616a54f9a"\n'
            'mode = "r2"\n\n'
            "[commands]\n"
            'plan_contract_accept = ".kent/scripts/workflow-plan-contract-accept"\n'
        )
        enrollment_directory = sidecar.parent.parent / "enrollments"
        binding_sha = hashlib.sha256(fixture.installation_binding_path.read_bytes()).hexdigest()
        enrollment = {
            "schema": profile_source.ENROLLMENT_SCHEMA,
            "project_id": "project-test",
            "primary_workspace_id": "workspace-test",
            "primary_root": str(primary),
            "settings_root": str(settings),
            "v2_binding_sha256": binding_sha,
        }
        enrollment_path = enrollment_directory / "project-test.json"
        enrollment_path.write_text(json.dumps(enrollment, sort_keys=True, separators=(",", ":")))
        enrollment_path.chmod(0o600)

        original_kent = fixture.bin / "kent"
        delegated_kent = fixture.bin / "kent-fixture"
        delegated_kent.write_text(original_kent.read_text())
        delegated_kent.chmod(0o700)
        original_kent.write_text(
            f"#!{sys.executable}\n"
            "import json, subprocess, sys\n"
            f"delegated = {str(delegated_kent)!r}\n"
            f"primary = {str(primary)!r}\n"
            "args = sys.argv[1:]\n"
            "if args == ['project', 'workspaces', '--project', 'project-test', "
            "'--offset', '0', '--limit', '100', '--json']:\n"
            "    print(json.dumps({'project_id':'project-test','offset':0,"
            "      'workspaces':[{'workspace_id':'workspace-test',"
            "      'root_path':primary,'is_default':True}]}))\n"
            "else:\n"
            "    raise SystemExit(subprocess.run([sys.executable, delegated, *args]).returncode)\n"
        )
        original_kent.chmod(0o700)

        # The native primary profile is deliberately irrelevant to ordinary
        # storage selection after the private enrollment binds external settings.
        primary_profile = primary / ".kent/workflow-profile.toml"
        primary_profile.write_text("[product]\nordinary_memory = false\n")
        store = ProjectMemory.initialize(
            fixture.root, memory["project_key"], memory["project_uid"],
        )

        def call(operation, *, native=False, environment=None, **fields):
            request = {
                "schema": "r2-workflow-request-v1",
                "operation": operation,
                "workspace_path": str(primary),
                **fields,
            }
            env = {
                **native_environment,
                "HOME": str(fixture.home),
            }
            if native:
                env.pop("KENT_TASK_ID", None)
                env.pop("KENT_SESSION_ID", None)
                request["native_context"] = {
                    "task_id": "task-native",
                    "node_id": "node-accept",
                }
            if environment is not None:
                env.update(environment)
            return subprocess.run(
                [sys.executable, str(sidecar), "workflow"],
                input=json.dumps(request), text=True, capture_output=True,
                cwd=primary, env=env, timeout=120,
            )

        wrong_profile = profile_path.read_text().replace(
            'project_uid = "bd9ed394-c5df-4eff-9363-6e0616a54f9a"',
            'project_uid = "519462bf-a544-4a83-a9aa-0f3265bc42c8"',
        )
        profile_path.write_text(wrong_profile)
        refused = call("initialize-task")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertFalse((store.path / "tasks/OSM-130").exists())

        profile_path.write_text(wrong_profile.replace(
            'project_uid = "519462bf-a544-4a83-a9aa-0f3265bc42c8"',
            'project_uid = "bd9ed394-c5df-4eff-9363-6e0616a54f9a"',
        ))
        initialized = call("initialize-task")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

        first = call(
            "publish", document_key="tasks/OSM-130/plan",
            expected_revision=None, content="R0\n", operation_id="kit-r0",
            provenance=[],
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        r0 = json.loads(first.stdout)["result"]
        second = call(
            "publish", document_key="tasks/OSM-130/plan",
            expected_revision=r0["token"], content="R1\n", operation_id="kit-r1",
            provenance=[],
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        r1 = json.loads(second.stdout)["result"]
        stale = call(
            "publish", document_key="tasks/OSM-130/plan",
            expected_revision=r0["token"], content="stale\n", operation_id="kit-stale",
            provenance=[],
        )
        self.assertEqual(stale.returncode, 2)
        self.assertEqual(Path(r0["path"]).read_text(), "R0\n")
        self.assertEqual(Path(r1["path"]).read_text(), "R1\n")
        head = call("read", document_key="tasks/OSM-130/plan")
        self.assertEqual(head.returncode, 0, head.stderr)
        self.assertEqual(json.loads(head.stdout)["result"], r1)

        profile = profile_path.read_text()
        profile_path.write_text(profile.replace(
            ".kent/scripts/workflow-plan-contract-accept",
            ".kent/scripts/unrelated-script",
        ))
        mutable_selector = call(
            "accept-plan", native=True,
            expected_revision=r1["token"], expected_accepted=None,
            metadata={
                "work_kind": "feature", "authority_refs": ["original:user:1"],
                "plan_kind": "markdown",
            },
        )
        self.assertEqual(mutable_selector.returncode, 0, mutable_selector.stderr)
        self.assertIsNotNone(store.accepted_head("OSM-130"))
        profile_path.write_text(profile)

        control = fixture.store.publish(
            "knowledge/control", None, "unrelated project remains\n",
            operation_id="control",
        )
        self.assertEqual(fixture.store.read("knowledge/control").token, control.token)


if __name__ == "__main__":
    unittest.main()
