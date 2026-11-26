from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_project_memory as fixtures


KIT = Path(__file__).resolve().parents[1]
CLI = KIT / "scripts/kent-memory"

# Keep the native control-plane trust seam at the OS boundary: use the actual
# qualification/recheck and subprocess runner, substitute fixture listener and
# process observations, and run only the disposable Kent executable.
MOCK_LOCAL_SERVICE = """
import os, runpy, sys
from pathlib import Path
from unittest.mock import patch

script = Path(sys.argv.pop(1))
sys.path.insert(0, str(script.parents[1]))
from workflowkit import operations

def status(kent, _workspace, environment):
    return {
        "endpoint": "http://127.0.0.1:30001",
        "health_status": "ok", "health_pid": 123,
        "command": [str(kent), "serve", "--persistence-root", environment["KENT_PERSISTENCE_ROOT"]],
    }

with patch.object(operations, "_native_comments_status", status), \\
        patch.object(operations, "_native_process_evidence", return_value={"pid": 123}), \\
        patch.object(operations, "_native_listener_evidence", return_value={"pid": 123}):
    runpy.run_path(str(script), run_name="__main__")
"""


class KentMemoryTests(unittest.TestCase):
    def prepare(self, *, external=False, legacy=False, adopted=True, foreign=False):
        f = self.f = fixtures.ProjectMemoryTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.adapter_fixture(seed_standalone=False, legacy_task_ids=("task-native",) if legacy else ())
        f.native_task.update(current_nodes=[], current_scripts=[], live_sessions=[],
                             worktree_path=str(f.project / "nonexistent-former-worktree"))
        f.native_task["summary"]["state"] = "interrupted"
        f._write_adapter_fake_kent()
        database = f.kent_home / "db/main.sqlite3"
        database.parent.mkdir()
        database.touch(mode=0o600)
        if adopted:
            f.store.initialize_task("OSM-130", "task-native-uid", [{
                "namespace": "kent", "project_id": "project-test",
                "task_id": "task-foreign" if foreign else "task-native",
            }])
        self.env = {"HOME": str(f.home), "PATH": str(f.bin) + os.pathsep + os.environ["PATH"]}
        if external:
            result = subprocess.run(
                [sys.executable, str(KIT / "scripts/install-ordinary-r2"), "--apply"],
                env=self.env, capture_output=True, check=True,
            )
            self.sidecar = Path(json.loads(result.stdout)["destination"])
            external_settings = f.home / "external-settings"
            profile = external_settings / ".kent/workflow-profile.toml"
            profile.parent.mkdir(parents=True)
            primary = f.project / ".kent/workflow-profile.toml"
            primary.rename(profile)
            self.enrollment = self.sidecar / "enrollments/project-test.json"
            self.enrollment.write_text(json.dumps({
                "schema": "ordinary-r2-enrollment-v1", "project_id": "project-test",
                "primary_workspace_id": "workspace-test", "primary_root": str(f.project),
                "settings_root": str(external_settings),
                "v2_binding_sha256": hashlib.sha256(f.installation_binding_path.read_bytes()).hexdigest(),
            }))
            self.enrollment.chmod(0o600)
        return f.store

    def call(self, *args, body=b"", cwd=None):
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", MOCK_LOCAL_SERVICE, str(CLI), *args],
            env=self.env, cwd=cwd or self.f.project, input=body, capture_output=True, timeout=30,
        )

    def success(self, *args, **kwargs):
        result = self.call(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def refused(self, expected, *args, **kwargs):
        result = self.call(*args, **kwargs)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertIn(expected, result.stderr)

    def round_trip(self, *, external):
        store = self.prepare(external=external)
        # The existing machine UI cannot perform these calls. Keep both machine
        # executables intact and make the ownerless baseline deterministic.
        machine = self.sidecar / "bin/kent-project-memory" if external else self.f.bin / "kent-project-memory"
        rejected = subprocess.run(
            [machine, "read", "notes"], env=self.env, cwd=self.f.project, capture_output=True, timeout=30,
        )
        self.assertEqual((rejected.returncode, rejected.stderr), (2, b"project-memory: invalid-memory-request\n"))

        text = "Facts — café\r\n".encode()
        revision = self.success("write", "notes", body=text).strip().decode()
        self.assertEqual(store.read("knowledge/notes").token, revision)
        self.assertEqual(self.success("read", "notes", cwd=self.f.project / ".kent"), text)
        self.assertEqual(json.loads(self.success("read", "--project", "Fixture", "notes", "--json",
                                                cwd=Path("/"))),
                         {"revision": revision, "content": text.decode()})

        self.env.update(KENT_TASK_ID="task-different", KENT_SESSION_ID="session-different",
                        KENT_STEP_ID="step-different")
        token = self.success("write", "--task", "OSM-130", "notes", body=b"First\n").strip().decode()
        self.assertEqual(store.read("tasks/OSM-130/notes").token, token)
        self.refused(b"revision conflict", "write", "--task", "OSM-130", "notes", body=b"Blind")
        self.success("write", "--task", "OSM-130", "notes", "--if-revision", token, body=b"Next\n")
        self.refused(b"revision conflict", "write", "--task", "OSM-130", "notes",
                     "--if-revision", token, body=b"Stale")
        self.assertEqual(self.success("read", "--task", "task-native", "notes"), b"Next\n")
        self.assertEqual(self.success("read", "notes"), text)

        data = b"\x00\xffcleanup inventory\n"
        digest = store.put_attachment("OSM-130", data).sha256
        command = ("attachment", "--project", "project-test", "--task", "OSM-130", digest)
        self.assertEqual(self.success(*command, cwd=self.f.home), data)
        store.read_attachment("OSM-130", digest)
        attachment = store.path / "tasks/OSM-130/artifacts/attachments/objects" / digest
        attachment.write_bytes(b"different data")
        self.refused(b"digest mismatch", *command, cwd=self.f.home)
        self.refused(b"invalid revision digest", *command[:-1], "../object")
        native = [json.loads(line) for line in self.f.native_command_log.read_text().splitlines()]
        self.assertTrue(all(call[0] in {"task", "project"} for call in native))
        self.assertTrue(all(call[:2] == ["task", "show"] for call in native if call[0] == "task"))

    def test_inline_round_trip(self):
        self.round_trip(external=False)

    def test_external_round_trip(self):
        self.round_trip(external=True)

    def test_plan_and_operational_state_remain_owned_by_the_workflow(self):
        store = self.prepare()
        task = "OSM-130"
        plan = store.publish(f"tasks/{task}/plan", None, "Reviewed")
        accepted = store.accept_plan(task, plan.token, None, {
            "work_kind": "implementation", "authority_refs": ["decision:fixture"],
        })
        inventory = store.put_attachment(task, b"Inventory")
        pending = store.new_invocation(task)["invocation_id"]
        store.begin_invocation(task, pending, "a" * 64)
        self.assertEqual(self.success("read", "--task", task, "plan"), b"Reviewed")
        for name in ("plan", "PLAN", "accepted-head.json", "accepted", "task.json", "artifacts", "intake.json"):
            self.refused(b"workflow-owned", "write", "--task", task, name, "--if-revision", plan.token, body=b"Other")
        for name in ("../notes", "artifacts/attachments/objects", "artifacts/mcp/calls/" + pending):
            self.refused(b"invalid memory key", "write", "--task", task, name, body=b"Other")
        self.assertEqual(store.read(f"tasks/{task}/plan"), plan)
        self.assertEqual(store.accepted_head(task), accepted.token)
        self.assertEqual(store.read_attachment(task, inventory.sha256), b"Inventory")
        self.assertEqual(store.read_invocation(task, pending)["state"], "unknown")

    def test_legacy_never_falls_back_even_with_existing_r2_task_data(self):
        store = self.prepare(legacy=True)
        self.refused(b"fixed legacy", "write", "--task", "OSM-130", "notes", body=b"Other")
        self.assertFalse((store.path / "tasks/OSM-130/notes").exists())
        self.success("write", "notes", body=b"R2 knowledge remains available")

    def test_unadopted_task_does_not_get_initialized(self):
        store = self.prepare(adopted=False)
        self.refused(b"no adopted R2 binding", "write", "--task", "OSM-130", "notes", body=b"Other")
        self.assertFalse((store.path / "tasks/OSM-130").exists())

    def test_wrong_project_task_and_foreign_binding_fail_closed(self):
        self.prepare(foreign=True)
        self.refused(b"project not uniquely registered", "read", "--project", "unknown", "notes")
        self.refused(b"Task not found", "read", "--task", "OSM-999", "notes")
        self.refused(b"identity differs", "write", "--task", "OSM-130", "notes", body=b"Other")

    def test_invalid_enrollment_cannot_switch_to_inline(self):
        store = self.prepare(external=True)
        settings = json.loads(self.enrollment.read_bytes())["settings_root"]
        (Path(settings) / ".kent/workflow-profile.toml").rename(self.f.project / ".kent/workflow-profile.toml")
        self.enrollment.write_bytes(b"{}")
        self.refused(b"enrollment schema differs", "write", "notes", body=b"Other")
        self.assertFalse((store.path / "knowledge/notes").exists())

    def test_unsafe_content_is_not_read_or_replaced(self):
        store = self.prepare()
        rev = store.publish("knowledge/notes", None, "Original")
        target = self.f.home / "outside"
        target.write_text("Outside")
        rev.path.unlink()
        rev.path.symlink_to(target)
        self.refused(b"unsafe memory file", "read", "notes")
        self.refused(b"unsafe memory file", "write", "notes", "--if-revision", rev.token, body=b"Other")
        self.assertEqual(target.read_text(), "Outside")

    def test_server_overrides_never_reach_storage_or_make_native_requests(self):
        store = self.prepare()
        for key, value in (("KENT_SERVER_HOST", "example.invalid"), ("KENT_SERVER_PORT", "32222")):
            self.env[key] = value
            self.refused(b"unset Kent server overrides", "write", "notes", body=b"Other")
            del self.env[key]
        self.assertFalse((store.path / "knowledge/notes").exists())
        self.assertFalse(self.f.native_command_log.exists())

    def test_default_selection_uses_physical_checkout_not_git_overrides(self):
        store = self.prepare()
        self.env.update(GIT_DIR=str(self.f.kit / ".git"), GIT_WORK_TREE=str(self.f.kit))
        self.success("write", "notes", body=b"Selected from physical checkout")
        self.assertEqual(store.read("knowledge/notes").path.read_bytes(), b"Selected from physical checkout")

    def test_targeted_install_leaves_existing_files_untouched(self):
        self.prepare()
        initial = sorted(self.f.bin.iterdir())

        def install(**overrides):
            return subprocess.run([str(KIT / "scripts/install"), "memory"],
                                  env={**self.env, **overrides}, capture_output=True, timeout=30)

        alternative = self.f.home / "other"
        self.assertNotEqual(install(KENT_PERSISTENCE_ROOT=str(alternative)).returncode, 0)
        self.assertFalse(alternative.exists())
        result = install()
        self.assertEqual(result.returncode, 0, result.stderr)
        link = self.f.bin / "kent-memory"
        self.assertEqual(link.readlink(), CLI)
        self.assertEqual(install().returncode, 0)
        self.assertEqual(install(KENT_PERSISTENCE_ROOT="").returncode, 0)
        self.assertEqual(sorted(item for item in self.f.bin.iterdir() if item != link), initial)
        help_result = subprocess.run([link, "--help"], env=self.env, capture_output=True, timeout=30)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)

        link.unlink()
        link.symlink_to(self.f.bin / "kent-project-memory")
        refused = install()
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(link.readlink(), self.f.bin / "kent-project-memory")

        link.unlink()
        link.write_bytes(CLI.read_bytes())
        self.assertNotEqual(install().returncode, 0)
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_bytes(), CLI.read_bytes())
        self.assertFalse(link.with_suffix(".pre-kent-engineering-kit").exists())

        other_bin = self.f.home / "other-bin"
        other_bin.mkdir()
        self.f.bin.rename(self.f.home / "original-bin")
        self.f.bin.symlink_to(other_bin)
        self.assertNotEqual(install().returncode, 0)
        self.assertEqual(list(other_bin.iterdir()), [])

    def test_existing_global_install_with_custom_root_does_not_require_home(self):
        self.prepare()
        persistence = self.f.home / "custom"
        result = subprocess.run(
            [str(KIT / "scripts/install")],
            env={"KENT_PERSISTENCE_ROOT": str(persistence), "PATH": self.env["PATH"]},
            capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((persistence / "bin/kent-project-memory").is_symlink())
        self.assertFalse((persistence / "bin/kent-memory").is_symlink())

    def test_existing_global_install_does_not_activate_ordinary_alias(self):
        self.prepare()
        home = self.f.home / "fresh"
        home.mkdir()
        binaries = home / ".kent/bin"
        result = subprocess.run(
            [str(KIT / "scripts/install")],
            env={**self.env, "HOME": str(home), "KENT_PERSISTENCE_ROOT": ""},
            capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((binaries / "kent-project-memory").is_symlink())
        self.assertTrue((binaries / "kent-mcp-list").is_symlink())
        self.assertFalse((binaries / "kent-memory").is_symlink())
