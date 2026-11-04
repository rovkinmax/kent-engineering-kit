"""Fresh installer controller tests; native effects are always modeled."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest import mock

from workflowkit import operations as op
from workflowkit import project_install as install


class ProjectInstallationTests(unittest.TestCase):
    def setUp(self):
        # A caller can confine every test write to its authorized fixture root.
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("INSTALL_TEST_ROOT"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)

    def plan(self):
        return {
            "schema": install.SCHEMA, "project": str(self.root / "primary"),
            "workflow": "22222222-2222-4222-8222-222222222222",
            "source_ref": "refs/remotes/origin/main", "kit_ref": "refs/remotes/origin/main",
            "receipt": {"project_id": "project-fixture", "project_memory": {
                "root": str(self.root / "memory"), "project_key": "fixture", "project_uid": "uid",
            }},
            "binding": {"receipt_path": str(self.root / "installations/project-fixture/receipt.json")},
            "binding_path": str(self.root / "bindings/project-fixture.json"),
        }

    def apply(self, plan, **kwargs):
        return install.apply(plan, op.sha256_bytes(op.canonical_bytes(plan)),
                             plan["receipt"]["project_id"], **kwargs)

    @unittest.skipUnless(hasattr(op, "__file__"), "requires real production dependencies")
    def test_real_first_publication_from_disposable_git_has_no_predecessor(self):
        from workflowkit import kit_runtime

        source = Path(kit_runtime.__file__).resolve().parents[1]
        kit, primary, home = (self.root / name for name in ("kit", "primary", "home"))
        home.mkdir(mode=0o700)
        for root in (kit, primary):
            root.mkdir(mode=0o700)
            (root / ".kent/scripts").mkdir(parents=True)
            (root / ".gitignore").write_text("__pycache__/\n")
        (kit / "workflowkit").mkdir()
        for filename in (source / "workflowkit").glob("*.py"):
            shutil.copyfile(filename, kit / "workflowkit" / filename.name)
        shutil.copyfile(op.__file__, kit / "workflowkit/operations.py")
        shutil.copyfile(install.__file__, kit / "workflowkit/project_install.py")
        (kit / "scripts").mkdir()
        shutil.copyfile(Path(install.__file__).resolve().parents[1] / "scripts/install",
                        kit / "scripts/install")
        (kit / "scripts/install").chmod(0o755)
        for name in ("scripts/inspect-delivery-source", "scripts/prepare-github-ci",
                     "templates/project/workflow-project-memory"):
            destination = kit / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, destination)
            destination.chmod(0o755)
        for root in (kit, primary):
            adapter = root / ".kent/scripts/workflow-project-memory"
            shutil.copyfile(source / "templates/project/workflow-project-memory", adapter)
            adapter.chmod(0o755)
            shutil.copyfile(source / "workflowkit/project_memory.py",
                            root / ".kent/scripts/workflow_project_memory.py")
        (primary / "AGENTS.md").write_text("Offline project fixture.\n")
        (primary / ".kent/project-contract.md").write_text("Offline project contract.\n")
        memory = {"root": str(self.root / "storage"), "project_key": "fixture", "project_uid": "fixture-uid"}
        (primary / ".kent/workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
            '[commands]\nproject_memory = ".kent/scripts/workflow-project-memory"\n'
            '[project_memory]\nmode = "r2"\n'
            + "".join(f"{key} = {json.dumps(value)}\n" for key, value in memory.items()))
        project_id = "project-22222222-2222-4222-8222-222222222222"
        workflow = "22222222-2222-4222-8222-222222222222"
        binary = home / ".kent/bin"
        binary.mkdir(parents=True, mode=0o700)
        storage_alias, ci_alias = binary / "kent-project-memory", binary / "kent-prepare-github-ci"
        storage_alias.symlink_to(kit / ".kent/scripts/workflow-project-memory")
        retained_ci = self.root / "retained/scripts/prepare-github-ci"
        retained_ci.parent.mkdir(parents=True)
        retained_ci.write_text("#!/bin/sh\nexit 99\n")
        retained_ci.chmod(0o755)
        ci_alias.symlink_to(retained_ci)
        native = binary / "kent"
        native.write_text(
            f"#!{sys.executable} -B\n"
            "import json, sys\n"
            f"project, root, workflow = {project_id!r}, {str(primary)!r}, {workflow!r}\n"
            "args = sys.argv[1:]\n"
            "if args == ['project', 'list']: print(project + '\\tFixture\\t' + root)\n"
            "elif args == ['project', root]: print(project)\n"
            "elif args[:2] == ['project', 'workspaces']:\n"
            " print(json.dumps({'project_id': project, 'offset': 0, 'workspaces': "
            "[{'workspace_id': 'workspace-fixture', 'root_path': root, 'is_default': True}]}))\n"
            "elif args[:2] == ['task', 'list']: print(json.dumps({'scope': {'project_id': project}, 'tasks': []}))\n"
            "elif args[:2] == ['workflow', 'list']:\n"
            " print(json.dumps({'workflows': [{'id': workflow, 'version': 1, 'project_link': {'default': False}}]}))\n"
            "elif args[:3] == ['workflow', 'validate', workflow]: print(json.dumps({'valid': True}))\n"
            "else: raise SystemExit('unexpected offline native command: ' + repr(args))\n")
        native.chmod(0o700)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GIT_", "KENT_", "PYTHON"))}
        environment.update(
            HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), TMPDIR=str(self.root),
            PATH=str(binary) + os.pathsep + os.environ.get("PATH", ""),
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_OPTIONAL_LOCKS="0",
            GIT_AUTHOR_NAME="Offline Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Offline Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
            GIT_AUTHOR_DATE="2026-11-04T00:00:00Z", GIT_COMMITTER_DATE="2026-11-04T00:00:00Z",
        )

        def git(root, *args):
            return subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "-C", str(root), *args], env=environment, capture_output=True,
                text=True, check=True, timeout=30,
            ).stdout.strip()

        for root in (kit, primary):
            git(root, "init", "-q")
            git(root, "add", "--all")
            git(root, "commit", "-qm", "Offline first-publication fixture")
            git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        arguments = {
            "project_id": project_id, "receipt_workflow_id": workflow,
            "primary_root": str(primary), "primary_workspace_id": "workspace-fixture",
            "source_commit": git(primary, "rev-parse", "HEAD"), "source_ref": "refs/remotes/origin/main",
            "kit_root": str(kit), "kit_commit": git(kit, "rev-parse", "HEAD"),
            "kit_ref": "refs/remotes/origin/main", "ci_helper": str(ci_alias),
            "helper_bindings": [{"link": str(storage_alias), "target": str(storage_alias.resolve())}],
            "project_memory": {**memory, "legacy_task_ids": []},
        }
        # This process imports the committed candidate from its real fixture
        # root. No producer, source/Git validation, inspector or reader is mocked.
        script = """
import importlib.machinery, importlib.util, json, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from workflowkit import operations as op, project_install as install
args = json.loads(sys.argv[2])
home, primary, kit = Path.home(), Path(args["primary_root"]), Path(args["kit_root"])
records = [home / ".kent/project-memory-bindings", home / ".kent/project-memory-installations"]
assert not any(path.exists() for path in records) and not Path(args["project_memory"]["root"]).exists()
receipt = op.prepare_initial_runtime_publication_v3(**args)
op.validate_runtime_v3_receipt(receipt, primary, kit)
assert receipt["schema"] == "delivery-runtime-publication-v3"
assert receipt["project_memory"] == args["project_memory"]
assert receipt["source_commit"] == args["source_commit"] and receipt["kit_commit"] == args["kit_commit"]
assert not any(path.exists() for path in records) and not Path(args["project_memory"]["root"]).exists()
assert op._git(primary, "status", "--porcelain") == op._git(kit, "status", "--porcelain") == ""
def public(*arguments):
    result = subprocess.run(["bash", str(kit / "scripts/install"), "project", *arguments],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)
plan = public("preview", "--project", str(primary), "--workflow", args["receipt_workflow_id"],
              "--source-ref", args["source_ref"], "--kit-ref", args["kit_ref"])
assert plan["receipt"] == receipt and not plan["ci_helper_observation"]["matches_publication"]
aliases = [Path(args["ci_helper"]), Path(args["helper_bindings"][0]["link"])]
before = [path.readlink() for path in aliases]
digest = op.sha256_bytes(op.canonical_bytes(plan))
plan_path = home / "reviewed-plan.json"
plan_path.write_bytes(op.canonical_bytes(plan))
plan_path.chmod(0o600)
result = public("apply", "--plan", str(plan_path), "--expect-plan-sha256", digest,
                "--confirm", args["project_id"])
assert result == {"status": "installed", "storage_registered": True, "source_admitted": False, "qualified": False}
install.readback(plan)
loader = importlib.machinery.SourceFileLoader("fixture_strict_inspector", str(kit / "scripts/inspect-delivery-source"))
spec = importlib.util.spec_from_loader(loader.name, loader)
inspector = importlib.util.module_from_spec(spec)
loader.exec_module(inspector)
try:
    inspector.inspect_source(
        receipt_path=Path(plan["binding"]["receipt_path"]), receipt_sha256=plan["binding"]["receipt_sha256"],
        project_id=args["project_id"], workflow_id=args["receipt_workflow_id"], project_root=primary,
        kit_root=kit, primary_root=primary, workspace_path=primary, source_ref=args["source_ref"],
        kit_ref=args["kit_ref"], ci_helper=args["ci_helper"],
        helper_bindings={row["link"]: row["target"] for row in args["helper_bindings"]})
except ValueError as error:
    assert str(error) == "installed helper target differs", str(error)
else:
    raise AssertionError("strict source admission must reject the retained CI alias")
assert [path.readlink() for path in aliases] == before
assert op._git(primary, "status", "--porcelain") == op._git(kit, "status", "--porcelain") == ""
print(json.dumps({"schema": receipt["schema"], "legacy_task_ids": receipt["project_memory"]["legacy_task_ids"],
                  "source_commit": receipt["source_commit"], "kit_commit": receipt["kit_commit"],
                  "file_count": len(receipt["files"]), "first_prepare_wrote_records": False,
                  "result": result, "strict_source_rejected": True}))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(kit), json.dumps(arguments)],
            env=environment, cwd=self.root, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["schema"], "delivery-runtime-publication-v3")
        self.assertEqual(summary["legacy_task_ids"], [])
        self.assertFalse(summary["first_prepare_wrote_records"])
        self.assertEqual(summary["source_commit"], arguments["source_commit"])
        self.assertEqual(summary["kit_commit"], arguments["kit_commit"])
        self.assertTrue(summary["strict_source_rejected"])
        self.assertFalse(summary["result"]["source_admitted"])
        print("Real disposable-Git initial producer: no predecessor/read-only PASS; "
              "REAL PUBLIC scripts/install project preview/apply and readback PASS; "
              "unchanged strict admission rejection PASS.")
        print("Fixture published refs:", summary["source_commit"], summary["kit_commit"],
              "receipt files:", summary["file_count"])

    def test_publish_is_exclusive_complete_private_and_idempotent(self):
        filename = self.root / "record.json"
        raw = b'{"complete":true}'
        self.assertTrue(install.publish(filename, raw))
        self.assertEqual(filename.stat().st_mode & 0o777, 0o600)
        self.assertEqual(filename.stat().st_nlink, 1)
        self.assertFalse(install.publish(filename, raw))
        with self.assertRaises(op.PlanValidationError):
            install.publish(filename, b"foreign")
        self.assertEqual(filename.read_bytes(), raw)

    def test_symlink_unsafe_modes_and_hardlinks_are_rejected(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real)
        with self.assertRaises(OSError), install.private_directory(link):
            pass
        real.chmod(0o755)
        with self.assertRaises(op.PlanValidationError), install.private_directory(real):
            pass
        real.chmod(0o700)
        record = real / "record"
        install.publish(record, b"test")
        os.link(record, real / "other")
        with self.assertRaises(op.PlanValidationError):
            install.publish(record, b"test")

    def test_interruption_before_link_never_publishes_partial_record(self):
        target = self.root / "record"
        with mock.patch.object(install.os, "link", side_effect=OSError("crash before link")):
            with self.assertRaises(OSError):
                install.publish(target, b"complete bytes")
        self.assertFalse(target.exists())
        staging = list(self.root.glob(".publish-*"))
        self.assertEqual(len(staging), 1)
        self.assertEqual(staging[0].read_bytes(), b"complete bytes")

    def test_failed_write_never_publishes_partial_record(self):
        target = self.root / "record"
        with mock.patch.object(install.os, "write", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                install.publish(target, b"record")
        self.assertFalse(target.exists())

    def test_fsync_failure_before_publication_never_exposes_record(self):
        target = self.root / "record"
        with mock.patch.object(install.os, "fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                install.publish(target, b"record")
        self.assertFalse(target.exists())

    def test_plan_hash_and_confirmation_block_before_any_effect(self):
        plan = self.plan()
        with mock.patch.object(install, "build_plan") as build:
            with self.assertRaises(op.PlanValidationError):
                install.apply(plan, "0" * 64, plan["receipt"]["project_id"])
            with self.assertRaises(op.PlanValidationError):
                install.apply(plan, op.sha256_bytes(op.canonical_bytes(plan)), "foreign")
        build.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_drift_blocks_before_any_effect(self):
        plan = self.plan()
        changed = {**plan, "native": {"task_ids": ["new-task"]}}
        with mock.patch.object(install, "build_plan", return_value=changed):
            with self.assertRaises(op.PlanValidationError):
                self.apply(plan)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_binding_is_last_and_exact_retry_has_no_write_effect(self):
        plan = self.plan()
        effects = []
        actual_publish = install.publish

        def publish(filename, raw):
            effects.append(filename)
            return actual_publish(filename, raw)

        with mock.patch.object(install, "build_plan", return_value=plan), mock.patch.object(
            install.ProjectMemory, "initialize", side_effect=lambda *args: effects.append("initialize"),
        ) as initialize, mock.patch.object(install, "readback") as readback, mock.patch.object(
            install, "publish", side_effect=publish,
        ):
            self.assertEqual(self.apply(plan), {
                "status": "installed", "storage_registered": True, "source_admitted": False, "qualified": False,
            })
            self.assertEqual(effects, [
                "initialize", Path(plan["binding"]["receipt_path"]), Path(plan["binding_path"]),
            ])
            initialize.reset_mock()
            effects.clear()
            self.assertEqual(self.apply(plan), {
                "status": "already-installed", "storage_registered": True,
                "source_admitted": False, "qualified": False,
            })
            initialize.assert_not_called()
            self.assertEqual(effects, [])
            self.assertEqual(readback.call_count, 2)

    def test_post_binding_failure_preserves_unqualified_installation(self):
        plan = self.plan()
        with mock.patch.object(install, "build_plan", return_value=plan), mock.patch.object(
            install.ProjectMemory, "initialize",
        ), mock.patch.object(install, "readback", side_effect=ValueError("reader rejected")):
            with self.assertRaisesRegex(op.EffectBlocked, "UNQUALIFIED"):
                self.apply(plan)
        self.assertTrue(Path(plan["binding_path"]).exists())
        self.assertTrue(Path(plan["binding"]["receipt_path"]).exists())

    def test_receipt_failure_never_publishes_binding(self):
        plan = self.plan()
        with mock.patch.object(install, "build_plan", return_value=plan), mock.patch.object(
            install.ProjectMemory, "initialize",
        ), mock.patch.object(install, "publish", side_effect=OSError("receipt failure")):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertFalse(Path(plan["binding_path"]).exists())

    def test_native_drift_after_receipt_blocks_binding(self):
        plan = self.plan()
        changed = copy.deepcopy(plan)
        changed["native"] = {"task_ids": ["racing-task"]}
        with mock.patch.object(install, "build_plan", side_effect=[plan, plan, changed]), mock.patch.object(
            install.ProjectMemory, "initialize",
        ):
            with self.assertRaises(op.PlanValidationError):
                self.apply(plan)
        self.assertTrue(Path(plan["binding"]["receipt_path"]).exists())
        self.assertFalse(Path(plan["binding_path"]).exists())

    def test_custom_persistence_root_blocks_before_native_read(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root),
                                         "KENT_PERSISTENCE_ROOT": str(self.root / "foreign")}), \
                mock.patch.object(install.shutil, "which") as which:
            with self.assertRaisesRegex(op.PlanValidationError, "custom persistence"):
                install.environment()
        which.assert_not_called()

    def test_native_empty_inventory_and_explicit_nondefault_workflow(self):
        native = self.root / "kent"
        project_id = "project-fixture"
        workflow_id = "workflow-fixture"
        empty = [{"scope": {"project_id": project_id}, "tasks": []}]
        workflows = [{"workflows": [{"id": workflow_id, "version": 1,
                                    "project_link": {"default": False}}]}]
        for tasks, default, valid, accepted in (
            (empty, False, True, True),
            ([{"scope": {"project_id": project_id}, "tasks": [{"id": "old-task"}]}], False, True, False),
            ([{"tasks": []}], False, True, False),
            ([{"scope": {"project_id": "foreign"}, "tasks": []}], False, True, False),
            (empty, True, True, False),
            (empty, False, False, False),
        ):
            rows = copy.deepcopy(workflows)
            rows[0]["workflows"][0]["project_link"]["default"] = default
            with self.subTest(tasks=tasks, default=default, valid=valid), mock.patch.object(
                op, "_source_native_identity", return_value="workspace-primary",
            ), mock.patch.object(op, "_kent_pages", side_effect=[tasks, rows]), mock.patch.object(
                op, "_kent_json", return_value={"valid": valid},
            ):
                if accepted:
                    self.assertEqual(install.native_snapshot(native, self.root, project_id, workflow_id),
                                     {"primary_workspace_id": "workspace-primary",
                                      "workflow_version": 1, "task_ids": []})
                else:
                    with self.assertRaises(op.OperationError):
                        install.native_snapshot(native, self.root, project_id, workflow_id)

    def test_concurrent_installers_share_project_lock(self):
        plan = self.plan()
        rendezvous = threading.Barrier(2)
        local = threading.local()

        def build(**kwargs):
            if not getattr(local, "entered", False):
                local.entered = True
                rendezvous.wait(timeout=10)
            return plan

        with mock.patch.object(install, "build_plan", side_effect=build), mock.patch.object(
            install.ProjectMemory, "initialize",
        ) as initialize, mock.patch.object(install, "readback"), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.apply(plan), range(2)))
        self.assertEqual(sorted(result["status"] for result in results), ["already-installed", "installed"])
        self.assertEqual(initialize.call_count, 1)
        self.assertEqual(install.read_private(Path(plan["binding_path"])),
                         op.canonical_bytes(plan["binding"]))

    def test_unknown_post_binding_publication_failure_is_unqualified(self):
        plan = self.plan()
        actual = install.publish

        def publish(filename, raw):
            result = actual(filename, raw)
            if filename == Path(plan["binding_path"]):
                raise OSError("post-link fsync/readback failure")
            return result

        with mock.patch.object(install, "build_plan", return_value=plan), mock.patch.object(
            install.ProjectMemory, "initialize",
        ), mock.patch.object(install, "publish", side_effect=publish):
            with self.assertRaisesRegex(op.EffectBlocked, "UNQUALIFIED"):
                self.apply(plan)
        self.assertTrue(Path(plan["binding_path"]).exists())

    def test_fresh_producer_has_no_predecessor_and_no_output_effect(self):
        values = {name: name for name in (
            "project_id", "receipt_workflow_id", "primary_root", "primary_workspace_id",
            "source_commit", "source_ref", "kit_root", "kit_commit", "kit_ref",
            "ci_helper", "helper_bindings",
        )}
        values["project_memory"] = {
            "root": "/memory", "project_key": "key", "project_uid": "uid", "legacy_task_ids": [],
        }
        result = object()
        with mock.patch.object(op, "_prepare_runtime_publication_v3", return_value=result) as prepare:
            self.assertIs(op.prepare_initial_runtime_publication_v3(**values), result)
        self.assertIsNone(prepare.call_args.kwargs["output_path"])
        self.assertIsNone(prepare.call_args.kwargs["predecessor_binding_ref"])
        self.assertIsNone(prepare.call_args.kwargs["predecessor_receipt_ref"])
        self.assertEqual(prepare.call_args.kwargs["initial_memory"], values["project_memory"])
        values["project_memory"]["legacy_task_ids"] = ["old"]
        with mock.patch.object(op, "_prepare_runtime_publication_v3") as prepare:
            with self.assertRaises(op.PlanValidationError):
                op.prepare_initial_runtime_publication_v3(**values)
            prepare.assert_not_called()

    def test_update_producer_preserves_required_predecessor_api(self):
        import inspect
        signature = inspect.signature(op.prepare_runtime_publication_v3)
        self.assertTrue(all(parameter.default is inspect.Parameter.empty
                            for parameter in signature.parameters.values()))
        values = {name: name for name in signature.parameters}
        with mock.patch.object(op, "_prepare_runtime_publication_v3", return_value="result") as prepare:
            self.assertEqual(op.prepare_runtime_publication_v3(**values), "result")
        self.assertEqual(prepare.call_args.kwargs, values)

    def test_update_producer_rejects_none_output_before_shared_preparation(self):
        import inspect
        values = {name: name for name in inspect.signature(op.prepare_runtime_publication_v3).parameters}
        values["output_path"] = None
        with mock.patch.object(op, "_prepare_runtime_publication_v3", return_value={"receipt": "unexpected"}) as prepare:
            with self.assertRaisesRegex(ValueError, "invalid absolute path"):
                op.prepare_runtime_publication_v3(**values)
            prepare.assert_not_called()

    def test_entrypoint_ignores_hostile_cwd_package_module_and_pythonpath(self):
        kit = self.root / "trusted-kit"
        (kit / "scripts").mkdir(parents=True)
        installer = kit / "scripts/install"
        shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/install", installer)
        package = kit / "workflowkit"
        package.mkdir()
        (package / "__init__.py").write_text("")
        entrypoint = package / "project_install.py"
        entrypoint.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'file': __file__, 'cwd': os.getcwd(), 'args': sys.argv[1:]}))\n")
        injected = self.root / "hostile-pythonpath"
        injected.mkdir()
        (injected / "sitecustomize.py").write_text("print('HOSTILE_STARTUP_EXECUTED')\n")
        (injected / "workflowkit.py").write_text("raise RuntimeError('hostile PYTHONPATH module')\n")
        for kind in ("module", "package"):
            caller = self.root / ("hostile-cwd-" + kind)
            caller.mkdir()
            if kind == "module":
                (caller / "workflowkit.py").write_text("raise RuntimeError('hostile cwd module')\n")
            else:
                (caller / "workflowkit").mkdir()
                (caller / "workflowkit/__init__.py").write_text("raise RuntimeError('hostile cwd package')\n")
            environment = dict(os.environ, PYTHONPATH=str(injected))
            with self.subTest(kind=kind):
                result = subprocess.run(
                    ["bash", str(installer), "project", "--probe"], cwd=caller,
                    env=environment, capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {
                    "file": str(entrypoint), "cwd": str(caller), "args": ["--probe"],
                })
        self.assertFalse(list(self.root.rglob("__pycache__")))

    def test_preview_is_read_only_and_binds_exact_receipt_bytes(self):
        project_id = "project-22222222-2222-4222-8222-222222222222"
        primary = self.root / "primary"
        profile = primary / ".kent/workflow-profile.toml"
        profile.parent.mkdir(parents=True, mode=0o700)
        profile.write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
            '[project_memory]\nmode = "r2"\nproject_key = "fixture"\nproject_uid = "uid"\n'
            f'root = "{self.root}/memory"\n')
        profile.chmod(0o600)
        kent = self.root / "kent"
        kent.write_text("fixture Kent bytes\n")
        ci = self.root / "home/.kent/bin/kent-prepare-github-ci"
        native = {"primary_workspace_id": "workspace-primary", "workflow_version": 1, "task_ids": []}
        receipt = {"schema": "modeled-publication-result", "project_id": project_id}
        before = {str(filename): filename.read_bytes() for filename in self.root.rglob("*")
                  if filename.is_file()}

        def git(root, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return str(primary)
            if args == ("rev-parse", "--absolute-git-dir"):
                return str(primary / ".git")
            if args == ("rev-parse", "--git-common-dir"):
                return ".git"
            return "a" * 40

        with mock.patch.object(install, "environment", return_value=(ci.parent.parent, kent)), \
                mock.patch.object(op, "_git", side_effect=git, create=True), \
                mock.patch.object(op, "_project_rows", return_value=[
                    {"id": project_id, "path": str(primary)}], create=True), \
                mock.patch.object(install, "native_snapshot", return_value=native), \
                mock.patch.object(install, "empty_store"), \
                mock.patch.object(install, "storage_bindings", return_value=(ci, [], {
                    "link": str(ci), "target": None, "matches_publication": False})), \
                mock.patch.object(install, "qualify_storage"), \
                mock.patch.object(op, "prepare_initial_runtime_publication_v3", return_value=receipt):
            plan = install.build_plan(project=str(primary),
                                      workflow="22222222-2222-4222-8222-222222222222",
                                      source_ref="refs/remotes/origin/main",
                                      kit_ref="refs/remotes/origin/main")
            self.assertEqual(plan["binding"]["receipt_sha256"],
                             op.sha256_bytes(op.canonical_bytes(receipt)))
        after = {str(filename): filename.read_bytes() for filename in self.root.rglob("*")
                 if filename.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((ci.parent.parent / "project-memory-installations").exists())

    def test_storage_binding_accepts_mixed_ci_without_changing_aliases(self):
        kent_root = self.root / "home/.kent"
        (kent_root / "bin").mkdir(parents=True, mode=0o700)
        kit, retained = self.root / "kit", self.root / "retained"
        (kit / ".kent/scripts").mkdir(parents=True)
        (retained / "scripts").mkdir(parents=True)
        target = kit / ".kent/scripts/workflow-project-memory"
        target.write_text("# published adapter fixture\n")
        old_ci = retained / "scripts/prepare-github-ci"
        old_ci.write_text("# retained CI fixture\n")
        storage = kent_root / "bin/kent-project-memory"
        ci = kent_root / "bin/kent-prepare-github-ci"
        storage.symlink_to(target)
        ci.symlink_to(old_ci)
        before = storage.readlink(), ci.readlink()
        self.assertEqual(install.storage_bindings(kent_root, kit), (
            ci, [{"link": str(storage), "target": str(target)}],
            {"link": str(ci), "target": str(old_ci), "matches_publication": False},
        ))
        self.assertEqual((storage.readlink(), ci.readlink()), before)
        storage.unlink()
        storage.symlink_to(old_ci)
        with self.assertRaisesRegex(op.PlanValidationError, "storage alias differs"):
            install.storage_bindings(kent_root, kit)

    def test_positive_storage_publication_requires_complete_matching_pairs(self):
        kit, primary = self.root / "kit", self.root / "primary"
        primary.mkdir()
        sources = {
            ".kent/scripts/workflow-project-memory": ("#!/bin/sh\nexit 0\n", 0o755),
            "templates/project/workflow-project-memory": ("#!/bin/sh\nexit 0\n", 0o755),
            ".kent/scripts/workflow_project_memory.py": ("# core fixture\n", 0o644),
            "workflowkit/project_memory.py": ("# core fixture\n", 0o644),
        }
        rows = []
        for name, (content, mode) in sources.items():
            filename = kit / name
            filename.parent.mkdir(parents=True, exist_ok=True)
            filename.write_text(content)
            filename.chmod(mode)
            rows.append({"root": str(kit), "relative_path": name,
                         "sha256": op.sha256_bytes(filename.read_bytes()),
                         "byte_size": filename.stat().st_size, "mode": mode})
        receipt = {
            "schema": "delivery-runtime-publication-v3",
            "policy": "delivery_reliability_project_content_v1",
            "project_id": "project-fixture", "workflow_id": "workflow-fixture",
            "source_commit": "a" * 40, "kit_commit": "b" * 40,
            "runtime_roots": [str(primary), str(kit)], "files": rows, "local_inputs": [],
            "project_memory": {"root": str(self.root / "memory"), "project_key": "key",
                               "project_uid": "uid", "legacy_task_ids": []},
        }
        install.qualify_storage(receipt, primary, kit)
        for mutation in ("missing", "digest", "mode", "copy"):
            changed = copy.deepcopy(receipt)
            if mutation == "missing":
                changed["files"].pop()
            elif mutation == "digest":
                changed["files"][0]["sha256"] = "0" * 64
            elif mutation == "mode":
                changed["files"][0]["mode"] = 0o644
            else:
                copied = kit / ".kent/scripts/workflow_project_memory.py"
                copied.write_text("# different published core fixture\n")
                changed["files"][2]["sha256"] = op.sha256_bytes(copied.read_bytes())
                changed["files"][2]["byte_size"] = copied.stat().st_size
            with self.subTest(mutation=mutation), self.assertRaises(op.PlanValidationError):
                install.qualify_storage(changed, primary, kit)


if __name__ == "__main__":
    unittest.main()
