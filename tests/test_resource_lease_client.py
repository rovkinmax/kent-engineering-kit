from __future__ import annotations

import importlib.machinery
import importlib.util
import fcntl
import io
import json
import os
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "templates/project/resource-lease-client"
loader = importlib.machinery.SourceFileLoader("resource_client_tested", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
client = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = client
loader.exec_module(client)

TASK = "task-11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
ACTOR_SESSION = "33333333-3333-4333-8333-333333333333"
STEP = "44444444-4444-4444-8444-444444444444"


class PublicCheckTests(unittest.TestCase):
    def test_standalone_check_observes_lease_without_native_task(self):
        self.check_case(None)

    def test_workflow_check_still_observes_native_task(self):
        self.check_case({"project": "/project", "task": TASK, "session": SESSION})

    def check_case(self, binding):
        @contextmanager
        def owned_lock(_):
            raise client.Busy("owned")
            yield

        with tempfile.TemporaryDirectory() as directory:
            current = {"state": "active", "endpoint": {"adb_serial": "emulator-5574"},
                       "expires_in": 30, "quarantined": False}
            lease = SimpleNamespace(run=Path(directory), metadata={"binding": binding},
                                    cli=lambda action: current)
            status = {"phase": "active", "updated_at": time.monotonic(),
                      "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
                      "parent_command_stop": "not_started",
                      "external_stop_unconfirmed": False,
                      "command_stop": "not_started",
                      "run_directory": str(lease.run), "lease": None, "error": None}
            with patch.object(client, "load", return_value=lease), patch.object(
                client, "read", side_effect=lambda _path: dict(status)
            ), patch.object(client, "lock", owned_lock), patch.object(
                client, "native_active"
            ) as native:
                self.assertEqual(client.check(lease.run)["lease"], current)
                if binding is None:
                    native.assert_not_called()
                else:
                    native.assert_called_once_with(binding)
                current["state"] = "completed"
                with self.assertRaisesRegex(client.Error, "lease is not active"):
                    client.check(lease.run)
                self.assertTrue((lease.run / "stop-renewal.json").exists())


FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
if args[:2] == ["task", "show"]:
    print(json.dumps({"summary":{"id":args[2],"done":False},
                      "status":{"kind":"running","native_state":"running"}}))
    raise SystemExit
if args[:2] == ["task", "sessions"]:
    state = pathlib.Path(os.environ["NATIVE_STATE"]).read_text().strip()
    print(json.dumps({"task_id":args[2], "items":[
        {"session_id":os.environ["NATIVE_SESSION"],"status":state}], "next_offset":None}))
    raise SystemExit
root = pathlib.Path(args[args.index("--root")+1])
action = args[2]
owner = args[args.index("--owner")+1]
receipt = pathlib.Path(args[args.index("--lease")+1])
with (root/"calls").open("a") as stream:
    stream.write(action+"\n")
if action == "acquire" and (root/"reject-acquire").exists():
    print("private-stdout-canary")
    print("private-stderr-canary", file=sys.stderr)
    raise SystemExit(8)
if (root/"reject").exists() and action == "renew":
    raise SystemExit(9)
if action == "acquire":
    value = {"owner":owner,"capability":args[args.index("--capability")+1],
             "id":"lease-one","generation":1,"slot":"test","service_available":True,
             "state":"active","cleanup_pending":False,
             "quarantined":False,"endpoint":{"adb_serial":"emulator-5574"}}
    receipt.write_text(json.dumps(value))
    receipt.chmod(0o600)
    while (root/"queued").exists():
        time.sleep(0.05)
else:
    value = json.loads(receipt.read_text())
if action in ("acquire","renew"):
    value["expires_at"] = time.monotonic() + 1.5
    receipt.write_text(json.dumps(value))
if action == "acquire" and (root/"reject-after-receipt").exists():
    print("private-stdout-canary")
    print("private-stderr-canary", file=sys.stderr)
    raise SystemExit(8)
if value["state"] == "active" and time.monotonic() >= value["expires_at"]:
    value["state"] = "expired"
    value["endpoint"] = None
if action in ("release","cancel"):
    value["state"] = "completed" if action == "release" else "cancelled"
    value["endpoint"] = None
    receipt.write_text(json.dumps(value))
value["expires_in"] = max(0, value["expires_at"] - time.monotonic())
if action == "release" and (root/"quarantine").exists():
    value["quarantined"] = True
print(json.dumps(value))
'''


class ResourceLeaseClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.runs = self.root / "runs"
        self.executable = self.root / "resource-lease"
        self.executable.write_text(FAKE)
        self.executable.chmod(0o700)
        (self.root / "kent").symlink_to(self.executable)
        self.native = self.root / "native"
        self.native.write_text("running")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "schema": 1, "executable": str(self.executable), "pool": str(self.pool),
            "runs_directory": str(self.runs),
            "profiles": {"ui.clean": "clean", "smoke.persistent": "persistent"},
        }))
        self.config.chmod(0o600)
        self.environment = dict(os.environ, PATH=f"{self.root}{os.pathsep}{os.environ['PATH']}",
                                NATIVE_STATE=str(self.native), NATIVE_SESSION=SESSION)
        self.environment.pop("ANDROID_SERIAL", None)

    def invoke(self, *args, **kwargs):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.environment,
                              capture_output=True, text=True, timeout=20, check=False, **kwargs)

    def run_args(self, code):
        return ["run", "--config", str(self.config), "--profile", "ui.clean",
                "--owner", "test-command", "--env", "ANDROID_SERIAL=adb_serial",
                "--", sys.executable, "-c", code]

    def calls(self):
        return (self.pool / "calls").read_text().splitlines()

    def test_owned_standalone_command_can_use_public_check(self):
        code = (
            "import os,subprocess,sys; "
            f"result=subprocess.run([sys.executable,{str(SCRIPT)!r},'check','--run-dir',"
            "os.environ['RESOURCE_LEASE_RUN_DIRECTORY']],capture_output=True,text=True); "
            "assert result.returncode==0,result.stderr"
        )
        result = self.invoke(*self.run_args(code))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls().count("acquire"), 1)
        self.assertEqual(self.calls()[-1], "release")

    def test_command_renews_and_releases_exact_serial(self):
        result = self.invoke(*self.run_args(
            "import os,time; assert os.environ['ANDROID_SERIAL']=='emulator-5574'; time.sleep(1.3)"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls().count("acquire"), 1)
        self.assertGreaterEqual(self.calls().count("renew"), 2)
        self.assertEqual(self.calls()[-1], "release")
        run = next(path for path in self.runs.iterdir() if path.is_dir())
        self.assertEqual(client.read(run / "binding.json")["custody_marker_protocol"], 1)
        self.assertEqual(client.read(run / "status.json")["custody_marker_protocol"], 1)

    def test_runtime_protocol_is_emitted_by_owner_not_copied_from_binding(self):
        self.root.chmod(0o700)
        lease = client.prepare(self.prepare_args("runtime-protocol"))
        status = lease.record("active")
        self.assertEqual(status["custody_marker_protocol"], client.CUSTODY_MARKER_PROTOCOL)

    def test_pending_parent_is_not_a_lifecycle_error_while_work_is_active(self):
        for phase in ("preparing", "acquiring", "active"):
            with self.subTest(phase=phase):
                lease = client.prepare(self.prepare_args(f"pending-{phase}"))
                status = lease.record(phase, parent_command_stop="pending")
                self.assertEqual(status["phase"], phase)
                self.assertEqual(status["command_stop"], "unconfirmed")
                self.assertIsNone(status["error"])

    def test_heartbeat_and_finish_preserve_existing_non_null_errors(self):
        terminal = {"state": "completed", "cleanup_pending": False,
                    "quarantined": False, "endpoint": None}
        for index, error in enumerate(("real lifecycle failure", "", "command stop is uncertain")):
            with self.subTest(error=error):
                lease = client.prepare(self.prepare_args(f"retained-error-{index}"))
                client.write(lease.run / "receipt.json", {})
                lease.record("stopped", error=error, parent_command_stop="confirmed")
                self.assertEqual(lease.record("active")["error"], error)
                with patch.object(lease, "cli", return_value=terminal):
                    status = lease.finish()
                self.assertEqual(status["phase"], "finished")
                self.assertEqual(status["error"], error)

    def test_intrinsic_unconfirmed_parent_remains_an_error_during_heartbeat(self):
        lease = client.prepare(self.prepare_args("intrinsic-stop-error"))
        status = lease.record("active", parent_command_stop="unconfirmed")
        self.assertEqual(status["command_stop"], "unconfirmed")
        self.assertIn("command stop is uncertain", status["error"])
        later = lease.record("active", parent_command_stop="confirmed")
        self.assertEqual(later["parent_command_stop"], "unconfirmed")
        self.assertEqual(later["command_stop"], "unconfirmed")
        self.assertEqual(later["error"], status["error"])

    def test_marker_lock_timeout_retains_durable_stop_barrier_without_unlocked_marker(self):
        lease = client.prepare(self.prepare_args("marker-timeout"))
        fd = os.open(lease.run, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(client, "CLI_TIMEOUT", 0.02), patch.object(
                    client.os, "fsync", wraps=os.fsync) as sync:
                with self.assertRaisesRegex(client.Busy, "marker coordination lock timed out"):
                    client.write(lease.run / client.UNCERTAINTY_MARKER, {
                        "schema": 1, "reason": "signal_error",
                    })
                self.assertGreaterEqual(sync.call_count, 2)
            self.assertFalse((lease.run / client.UNCERTAINTY_MARKER).exists())
            self.assertEqual(client.read(lease.run / "stop-renewal.json"), {
                "schema": 1, "reason": "marker_lock_unavailable",
            })
            fcntl.flock(fd, fcntl.LOCK_UN)
            lease.record("active", parent_command_stop="unconfirmed")
            terminal = {"state": "completed", "cleanup_pending": False,
                        "quarantined": False, "endpoint": None}
            with patch.object(lease, "cli", return_value=terminal):
                final = lease.finish()
            self.assertEqual(final["phase"], "failed")
            status = client.read(lease.run / "status.json")
            self.assertEqual(status["phase"], "failed")
            self.assertEqual(status["command_stop"], "unconfirmed")
        finally:
            os.close(fd)

    def test_marker_publication_file_and_directory_fsync_failures_retain_stop_barrier(self):
        self.root.chmod(0o700)
        real_fsync = os.fsync
        for failed_call in (1, 2):
            calls = 0

            def fsync(fd):
                nonlocal calls
                calls += 1
                if calls == failed_call:
                    raise OSError("fixture_fsync")
                return real_fsync(fd)

            marker = self.root / client.UNCERTAINTY_MARKER
            stop = self.root / "stop-renewal.json"
            with self.subTest(failed_call=failed_call), patch.object(
                    client.os, "fsync", side_effect=fsync):
                with self.assertRaisesRegex(OSError, "fixture_fsync"):
                    client.write(marker, {"schema": 1, "reason": "signal_error"})
            self.assertTrue(stop.is_file())
            self.assertFalse(any(self.root.glob(f".{client.UNCERTAINTY_MARKER}-*")))
            marker.unlink(missing_ok=True)
            stop.unlink()

    def test_marker_lock_failure_never_publishes_unlocked_and_reports_barrier_failure(self):
        self.root.chmod(0o700)
        with patch.object(client, "marker_lock", side_effect=OSError("fixture_lock")):
            with self.assertRaisesRegex(OSError, "fixture_lock"):
                client.write(self.root / client.UNCERTAINTY_MARKER, {})
        self.assertTrue((self.root / "stop-renewal.json").is_file())
        self.assertFalse((self.root / client.UNCERTAINTY_MARKER).exists())
        with patch.object(client, "marker_lock", side_effect=OSError("fixture_lock")), patch.object(
                client, "_stop_marker_publication_failed", side_effect=OSError("fixture_barrier")
        ), self.assertRaisesRegex(OSError, "fixture_barrier"):
            client.write(self.root / client.UNCERTAINTY_MARKER, {})
        self.assertFalse((self.root / client.UNCERTAINTY_MARKER).exists())

    def test_marker_replace_failure_preserves_durable_barrier_and_removes_temporary(self):
        self.root.chmod(0o700)
        real_replace = os.replace
        marker = self.root / client.UNCERTAINTY_MARKER

        def replace(source, destination):
            if destination == marker:
                raise OSError("fixture_replace")
            return real_replace(source, destination)

        with patch.object(client.os, "replace", side_effect=replace), patch.object(
                client.os, "fsync", wraps=os.fsync) as sync:
            with self.assertRaisesRegex(OSError, "fixture_replace"):
                client.write(marker, {"schema": 1, "reason": "signal_error"})
            self.assertGreaterEqual(sync.call_count, 3)
        self.assertFalse(marker.exists())
        self.assertTrue((self.root / "stop-renewal.json").is_file())
        self.assertFalse(any(self.root.glob(f".{client.UNCERTAINTY_MARKER}-*")))

    def test_command_failure_preserved_and_cleanup_runs(self):
        result = self.invoke(*self.run_args("raise SystemExit(7)"))
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn('"command_exit": 7', result.stderr)
        self.assertEqual(self.calls()[-1], "release")

    def test_renewal_failure_cannot_pass(self):
        (self.pool / "reject").touch()
        result = self.invoke(*self.run_args("import time; time.sleep(10)"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("resource-lease renew failed with exit 9", result.stderr)
        self.assertNotIn("Check pool status", result.stderr)
        self.assertEqual(self.calls()[-1], "release")

    def test_acquire_failure_has_safe_guidance_without_extra_commands(self):
        for after_receipt in (False, True):
            with self.subTest(after_receipt=after_receipt):
                flag = self.pool / (
                    "reject-after-receipt" if after_receipt else "reject-acquire"
                )
                flag.touch()
                before = self.calls() if (self.pool / "calls").exists() else []
                result = self.invoke(*self.run_args("raise SystemExit(99)"))
                self.assertEqual(result.returncode, 1)
                self.assertIn('"command_exit": null', result.stderr)
                self.assertIn("resource-lease acquire failed with exit 8", result.stderr)
                self.assertIn(
                    "Check pool status using executable and pool from the client configuration:",
                    result.stderr,
                )
                self.assertIn("<executable> --root <pool> status", result.stderr)
                self.assertIn("Inspect the original run before retrying.", result.stderr)
                for secret in ("private-stdout-canary", "private-stderr-canary"):
                    self.assertNotIn(secret, result.stdout + result.stderr)
                expected = ["acquire", "cancel"] if after_receipt else ["acquire"]
                self.assertEqual(self.calls()[len(before):], expected)
                flag.unlink()

    def test_capture_resource_operations_do_not_expose_child_streams(self):
        code = (
            "import sys; print('private-stdout-canary'); "
            "print('private-stderr-canary', file=sys.stderr); sys.exit(7)"
        )
        for operation in ("renew", "inspect", "release", "cancel"):
            with self.subTest(operation=operation):
                with self.assertRaises(client.Error) as raised:
                    client.capture(
                        [sys.executable, "-c", code], resource_operation=operation,
                    )
                self.assertEqual(
                    str(raised.exception), f"resource-lease {operation} failed with exit 7",
                )

    def test_capture_preserves_native_callback_failure(self):
        failure = client.Error("native Session is not running")

        def tick():
            raise failure

        with self.assertRaises(client.Error) as raised:
            client.capture(
                [sys.executable, "-c", "import time; time.sleep(3)"],
                tick=tick, resource_operation="acquire",
            )
        self.assertIs(raised.exception, failure)

    def test_native_run_completion_rejects_success_exit_with_pending_outcome(self):
        final = {"status": "ok", "result": "completed", "session_id": SESSION}
        self.assertTrue(client.native_run_completed(final, session=SESSION))
        for pending in (
            {"status": "ok", "session_id": SESSION,
             "outcome": {"kind": "question", "question_id": "q-1"}},
            {"status": "ok", "session_id": SESSION,
             "outcome": {"kind": "approval", "approval_id": "a-1"}},
            {"status": "ok", "session_id": SESSION, "result": ""},
            {"status": "ok", "session_id": SESSION, "result": "done",
             "error": "retained"},
            {"status": "error", "session_id": SESSION, "result": "done"},
            {**final, "session_id": ACTOR_SESSION},
        ):
            with self.subTest(pending=pending):
                self.assertFalse(client.native_run_completed(pending, session=SESSION))

    def test_capture_success_and_non_resource_error_remain_unchanged(self):
        with patch.object(client.subprocess, "Popen", wraps=subprocess.Popen) as spawn:
            value = client.capture(
                [sys.executable, "-c", 'print(\'{"state":"active"}\')'],
                resource_operation="acquire",
            )
        self.assertEqual(value, {"state": "active"})
        self.assertEqual(spawn.call_count, 1)
        with self.assertRaises(client.Error) as raised:
            client.capture([sys.executable, "-c", "raise SystemExit(6)"])
        self.assertEqual(
            str(raised.exception), f"{Path(sys.executable).name} failed with exit 6",
        )

    def test_conflicting_serial_rejected_before_command(self):
        self.environment["ANDROID_SERIAL"] = "foreign"
        result = self.invoke(*self.run_args("raise SystemExit(99)"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("conflicting inherited ANDROID_SERIAL", result.stderr)
        self.assertEqual(self.calls()[-1], "release")

    def test_private_config_and_explicit_profile(self):
        self.config.chmod(0o644)
        result = self.invoke(*self.run_args("pass"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.pool / "calls").exists())
        self.config.chmod(0o600)
        args = self.run_args("pass")
        args[args.index("ui.clean")] = "missing"
        self.assertIn("not configured", self.invoke(*args).stderr)

    def test_duplicate_config_fields_are_rejected(self):
        self.config.write_text('{"schema": 1, "schema": 1}')
        result = self.invoke(*self.run_args("pass"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate JSON field", result.stderr)

    def test_capture_bounds_output_timeout_and_type(self):
        with self.assertRaisesRegex(client.Error, "size limit"):
            client.capture([sys.executable, "-c", "print('x'*300000)"])
        with self.assertRaisesRegex(client.Error, "timed out"):
            client.capture([sys.executable, "-c", "import time; time.sleep(3)"], timeout=0.1)
        with self.assertRaisesRegex(client.Error, "non-object"):
            client.capture([sys.executable, "-c", "print('[]')"])

    def start_helper(self):
        run = self.runs / "smoke"
        args = ["start", "--config", str(self.config), "--profile", "smoke.persistent",
                "--run-dir", str(run), "--project", "/project", "--task", TASK, "--session", SESSION]
        # Register cleanup before starting the process, including failed assertions.
        self.addCleanup(lambda: self.invoke("finish", "--run-dir", str(run)))
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return run, args

    def test_smoke_helper_exact_retry_check_and_finish(self):
        run, args = self.start_helper()
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls().count("acquire"), 1)
        checked = self.invoke("check", "--run-dir", str(run))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["endpoint"]["adb_serial"], "emulator-5574")
        finished = self.invoke("finish", "--run-dir", str(run))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertNotEqual(self.invoke(*args).returncode, 0)

    def test_observed_question_denies_check(self):
        run, _ = self.start_helper()
        self.native.write_text("question")
        checked = self.invoke("check", "--run-dir", str(run))
        self.assertEqual(checked.returncode, 1)
        self.assertIn("not running", checked.stderr)
        self.native.write_text("running")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = client.read(run / "status.json")
            if status["phase"] == "stopped":
                break
            time.sleep(0.05)
        self.assertEqual(status["phase"], "stopped")
        self.assertNotEqual(self.invoke("check", "--run-dir", str(run)).returncode, 0)

    def test_native_pagination_and_unknown_status_fail_closed(self):
        binding = {"project": "/project", "task": TASK, "session": SESSION}
        detail = {"summary": {"id": TASK, "done": False},
                  "status": {"kind": "running", "native_state": "active"}}
        first = {"task_id": TASK, "items": [], "next_offset": 100}
        last = {"task_id": TASK, "items": [{"session_id": SESSION, "status": "running"}],
                "next_offset": None}
        with patch.object(client, "capture", side_effect=[detail, first, last]) as capture:
            client.native_active(binding)
            self.assertIn("100", capture.call_args.args[0])
        for status in ("question", "idle", "unknown"):
            last["items"][0]["status"] = status
            with (patch.object(client, "capture", side_effect=[detail, last]),
                  self.assertRaises(client.Error)):
                client.native_active(binding)

    def test_malformed_native_summary_is_rejected(self):
        binding = {"project": "/project", "task": TASK, "session": SESSION}
        with (patch.object(client, "capture", return_value={"summary": [], "status": {}}),
              self.assertRaisesRegex(client.Error, "not observably active")):
            client.native_active(binding)

    def test_current_native_schema_and_ineligible_tasks_before_acquisition(self):
        binding = {"project": "/project", "task": TASK, "session": SESSION}
        detail = {"summary": {"id": TASK, "done": False},
                  "status": {"kind": "running", "native_state": "running"}}
        page = {"task_id": TASK, "items": [{"session_id": SESSION, "status": "running"}],
                "next_offset": None}
        with patch.object(client, "capture", side_effect=[detail, page]) as capture:
            client.native_active(binding)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(capture.call_args_list[0].args[0],
                         ["kent", "task", "show", TASK, "--project", "/project", "--json"])
        self.assertEqual(capture.call_args_list[1].args[0],
                         ["kent", "task", "sessions", TASK, "--project", "/project",
                          "--offset", "0", "--limit", "100", "--json"])
        rejected = [
            dict(detail, status={"kind": kind, "native_state": "active"})
            for kind in ("doing", "waiting_question", "waiting_approval", "queued", "backlog",
                         "interrupted", "done", "active", "unknown")
        ]
        rejected += [
            dict(detail, summary={"id": TASK, "done": True}),
            dict(detail, summary={"id": "task-33333333-3333-4333-8333-333333333333", "done": False}),
        ]
        rejected += [
            dict(detail, status={"kind": "running", "native_state": state})
            for state in ("interrupted", "completed", "unknown", None)
        ]
        rejected.append(dict(detail, status={"kind": "running"}))
        args = SimpleNamespace(task=TASK, session=SESSION, project="/project",
                               config=str(self.config), profile="smoke.persistent",
                               run_dir=str(self.runs / "rejected"))
        for value in rejected:
            with self.subTest(value=value):
                with (patch.object(client, "capture", return_value=value) as capture,
                      self.assertRaisesRegex(client.Error, "not observably active")):
                    client.start(args)
                self.assertEqual(capture.call_count, 1)
                self.assertFalse(self.runs.exists())
                self.assertFalse((self.pool / "calls").exists())

    def test_current_native_schema_still_requires_exact_running_session(self):
        binding = {"project": "/project", "task": TASK, "session": SESSION}
        detail = {"summary": {"id": TASK, "done": False},
                  "status": {"kind": "running", "native_state": "running"}}
        matching = {"session_id": SESSION, "status": "running"}
        inventories = [[], [dict(matching, session_id="other")], [matching, matching]]
        inventories += [[dict(matching, status=state)]
                        for state in ("question", "waiting", "idle", "interrupted", "done", "unknown")]
        for items in inventories:
            page = {"task_id": TASK, "items": items, "next_offset": None}
            with (self.subTest(items=items),
                  patch.object(client, "capture", side_effect=[detail, page]) as capture,
                  self.assertRaises(client.Error)):
                client.native_active(binding)
            self.assertEqual(capture.call_count, 2)
        with (patch.object(client, "capture", side_effect=[
                detail, {"task_id": "other", "items": [matching], "next_offset": None}]),
              self.assertRaisesRegex(client.Error, "invalid native Session inventory")):
            client.native_active(binding)

    def test_renewal_stops_on_owner_failure_without_release(self):
        metadata = {"config": {}, "binding": {"task": TASK, "session": SESSION, "project": "/project"}}
        lease = client.Lease(self.root, metadata)
        renewal = client.Renewal(lease)
        with (patch.object(client, "native_active", side_effect=client.Error("owner stopped")),
              patch.object(client.Lease, "cli") as call):
            with self.assertRaisesRegex(client.Error, "owner stopped"):
                renewal.tick()
            call.assert_not_called()

    def test_expired_or_quarantined_response_rejected(self):
        base = {"state": "active", "expires_in": 10, "endpoint": {"serial": "one"}, "quarantined": False}
        self.assertAlmostEqual(client.active(base), 10 / 3)
        for change in ({"expires_in": 0}, {"expires_in": True}, {"expires_in": float("nan")},
                       {"quarantined": True}, {"state": "expired"}, {"endpoint": {}}):
            with self.assertRaises(client.Error):
                client.active(dict(base, **change))

    def test_cleanup_uncertainty_is_not_success(self):
        lease = client.prepare(self.prepare_args("cleanup-uncertain"))
        client.write(lease.run / "receipt.json", {})
        for result in ({"state": "completed", "cleanup_pending": True, "quarantined": False},
                       {"state": "completed", "cleanup_pending": False, "quarantined": True},
                       {"state": "failed", "cleanup_pending": False, "quarantined": False}):
            with (patch.object(client.Lease, "cli", return_value=result),
                  self.assertRaisesRegex(client.Error, "not confirmed")):
                lease.finish()

    def test_pruning_only_proven_finished_records(self):
        self.runs.mkdir(mode=0o700)
        for index in range(66):
            run = self.runs.resolve() / str(index)
            run.mkdir(mode=0o700)
            client.write(run / "status.json", {
                "phase": "finished", "updated_at": index,
                "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
                "parent_command_stop": "not_started",
                "external_stop_unconfirmed": False,
                "command_stop": "not_started",
                "run_directory": str(run.resolve()), "error": None,
                "lease": {"state": "completed", "cleanup_pending": False, "quarantined": False},
            })
        unknown = self.runs / "unknown"
        unknown.mkdir(mode=0o700)
        client.write(unknown / "status.json", {"phase": "failed"})
        client.prune(self.runs.resolve())
        self.assertEqual(len(list(self.runs.iterdir())), 65)
        self.assertTrue(unknown.exists())
        self.assertFalse((self.runs / "0").exists())

    def prepare_args(self, name, *, config=None, runs=None):
        return SimpleNamespace(config=str(config or self.config), profile="ui.clean",
                               owner="test-command", run_dir=str(((runs or self.runs) / name).resolve()))

    def stop_case(self, mode, *, cleanup_fails=False, marker_fails=False, status_fails=False,
                  finish_status_fails_once=False):
        args = self.prepare_args(f"stop-{mode}-{cleanup_fails}-{marker_fails}-{status_fails}")
        args.command, args.env = ["--", "never-executed"], []
        run = Path(args.run_dir)
        secret = "must-not-leak-stop-exception"
        actions, signals, waits = [], [], []
        original_tick, original_write_record = client.Renewal.tick, client._write_record
        finish_status_failed = False

        class Child:
            pid = 411000001
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                waits.append(timeout)
                if mode == "timeout":
                    raise subprocess.TimeoutExpired([secret], timeout)
                self.returncode = -signal.SIGTERM
                return self.returncode

        child = Child()

        def fake_capture(command, **_kwargs):
            action = command[3]
            self.assertIn(action, ("acquire", "release"))
            actions.append(action)
            value = {"owner": args.owner, "capability": "clean", "id": "lease-one",
                     "generation": 1, "slot": "test", "state": "active",
                     "cleanup_pending": False, "quarantined": False,
                     "endpoint": {"adb_serial": "never-used"}, "expires_in": 30,
                     "expires_at": time.monotonic() + 30}
            if action == "release":
                expected_stop = {"confirmed": "confirmed", "not_started": "not_started"}.get(
                    mode, "unconfirmed")
                self.assertEqual(client.read(run / "status.json")["command_stop"], expected_stop)
                if cleanup_fails:
                    raise client.Error("diagnostic resource cleanup not confirmed")
                value.update(state="completed", endpoint=None)
            original_write_record(run / "receipt.json", value)
            return value

        def interrupt(renewal):
            if renewal.value is not None:
                renewal.next_record = 0
                original_tick(renewal)
                self.assertEqual(client.read(run / "status.json")["command_stop"], "unconfirmed")
                raise client.Error("diagnostic consumer interruption")
            return original_tick(renewal)

        def killpg(pid, number):
            self.assertEqual(pid, child.pid)
            signals.append(number)
            if mode == "oserror":
                raise OSError(secret)

        def failing_write(path, value):
            nonlocal finish_status_failed
            if path.name == client.UNCERTAINTY_MARKER and marker_fails:
                raise OSError(secret)
            if path.name == "status.json" and value["phase"] == "failed" and status_fails:
                raise OSError(secret)
            if (finish_status_fails_once and not finish_status_failed and path.name == "status.json"
                    and value["phase"] == "failed" and value.get("lease", {}).get("state") == "completed"):
                finish_status_failed = True
                raise OSError(secret)
            return original_write_record(path, value)

        stderr = io.StringIO()
        with (patch.object(client, "capture", side_effect=fake_capture),
              patch.object(client.subprocess, "Popen", return_value=child,
                           side_effect=OSError("diagnostic command not started")
                           if mode == "not_started" else None),
              patch.object(client.os, "killpg", side_effect=killpg),
              patch.object(client.Renewal, "tick", interrupt),
              patch.object(client, "_write_record", side_effect=failing_write),
              redirect_stderr(stderr)):
            result = client.run_command(args)
        self.assertEqual(result, 1)
        self.assertEqual(actions, ["acquire", "release"])
        self.assertNotIn(secret, stderr.getvalue())
        self.assertTrue((run / "receipt.json").exists())
        if mode == "timeout":
            self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
            self.assertEqual(waits, [5, 5])
        return run, client.read(run / "status.json"), stderr.getvalue()

    def assert_retained_after_prune(self, run):
        for index in range(65):
            completed = self.runs.resolve() / f"newer-{index}"
            completed.mkdir(mode=0o700, exist_ok=True)
            client.write(completed / "status.json", {
                "phase": "finished", "updated_at": time.monotonic() + index,
                "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
                "parent_command_stop": "not_started",
                "external_stop_unconfirmed": False,
                "command_stop": "not_started",
                "run_directory": str(completed.resolve()), "error": None,
                "lease": {"state": "completed", "cleanup_pending": False, "quarantined": False},
            })
        client.prune(self.runs.resolve())
        self.assertTrue((run / "receipt.json").exists())
        self.assertTrue((run / "status.json").exists())
        self.assertEqual(len(list(self.runs.glob("newer-*"))), 64)

    def test_prune_retains_finished_runs_with_non_null_errors(self):
        terminal = {"state": "completed", "cleanup_pending": False,
                    "quarantined": False, "endpoint": None}
        for index, error in enumerate(("real lifecycle failure", "", "command stop is uncertain")):
            with self.subTest(error=error):
                lease = client.prepare(self.prepare_args(f"finished-error-{index}"))
                client.write(lease.run / "receipt.json", {})
                lease.record("stopped", error=error, parent_command_stop="confirmed")
                with patch.object(lease, "cli", return_value=terminal):
                    status = lease.finish()
                self.assertEqual(status["error"], error)
                self.assert_retained_after_prune(lease.run)

    def test_uncertain_command_stop_retains_cleanup_result_and_diagnostics(self):
        for mode in ("oserror", "timeout"):
            with self.subTest(mode=mode):
                run, status, stderr = self.stop_case(mode)
                self.assertEqual(status["phase"], "failed")
                self.assertEqual(status["command_stop"], "unconfirmed")
                self.assertEqual(status["lease"]["state"], "completed")
                self.assertFalse(status["lease"]["cleanup_pending"])
                self.assertIn("command stop is uncertain", status["error"])
                self.assertIn("command stop is uncertain", stderr)
                marker = run / client.UNCERTAINTY_MARKER
                marker_value = client.read(marker)
                self.assertEqual(marker_value["reason"],
                                 "wait_timeout" if mode == "timeout" else "signal_error")
                self.assertRegex(marker_value["operation"], r"^[0-9a-f]{32}$")
                self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
                retried = self.invoke("finish", "--run-dir", str(run))
                self.assertEqual(retried.returncode, 1, retried.stderr)
                self.assertIn("command stop is uncertain", retried.stdout)
                self.assertIn("resource cleanup confirmed", retried.stdout)
                self.assertEqual(client.read(run / "status.json")["lease"]["state"], "completed")
                self.assertTrue(marker.exists())
                self.assert_retained_after_prune(run)

    def test_uncertain_command_and_resource_cleanup_failures_both_survive(self):
        run, status, _stderr = self.stop_case("oserror", cleanup_fails=True)
        self.assertEqual(status["phase"], "failed")
        self.assertIn("command stop is uncertain", status["error"])
        self.assertIn("cleanup failed", status["error"])
        self.assertIn("resource cleanup not confirmed", status["error"])
        self.assertNotEqual(status["lease"]["state"], "completed")
        self.assert_retained_after_prune(run)

    def test_uncertainty_marker_write_failure_cannot_be_erased_by_finish(self):
        for status_fails in (False, True):
            with self.subTest(status_fails=status_fails):
                run, status, stderr = self.stop_case(
                    "oserror", marker_fails=True, status_fails=status_fails)
                self.assertFalse((run / client.UNCERTAINTY_MARKER).exists())
                self.assertEqual(status["command_stop"], "unconfirmed")
                self.assertNotEqual(status["phase"], "finished")
                self.assertIn("uncertainty marker persistence failed", stderr)
                # This goes through a fresh adapter process, not retained memory.
                retried = self.invoke("finish", "--run-dir", str(run))
                self.assertEqual(retried.returncode, 1, retried.stderr)
                after = client.read(run / "status.json")
                self.assertEqual(after["phase"], "failed")
                self.assertEqual(after["command_stop"], "unconfirmed")
                self.assertEqual(after["lease"]["state"], "completed")
                self.assert_retained_after_prune(run)

    def test_terminal_null_generation_finish_can_repeat(self):
        lease = client.prepare(self.prepare_args("repeat-terminal"))
        active = {
            "owner": "test-command", "capability": "clean", "id": "lease-one",
            "generation": 1, "slot": "test", "state": "active",
            "cleanup_pending": False, "quarantined": False,
            "endpoint": {"adb_serial": "emulator-5574"},
        }
        client.write(lease.run / "receipt.json", active)
        lease.record("active", lease=active, parent_command_stop="confirmed")
        terminal = dict(active, state="completed", generation=None, endpoint=None)
        with patch.object(lease, "cli", return_value=terminal):
            self.assertEqual(lease.finish()["phase"], "finished")
            self.assertEqual(lease.finish()["phase"], "finished")
        self.assertIsNone(client.read(lease.run / "status.json")["lease"]["generation"])

    def test_external_stop_negative_survives_aggregate_recomputation(self):
        lease = client.prepare(self.prepare_args("sticky-external-stop"))
        client.write(lease.run / "status.json", {
            "phase": "active",
            "updated_at": time.monotonic(),
            "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
            "parent_command_stop": "confirmed",
            "external_stop_unconfirmed": True,
            "command_stop": "unconfirmed",
            "run_directory": str(lease.run),
            "lease": None,
            "error": None,
        })

        status = lease.record("active", parent_command_stop="confirmed")

        self.assertTrue(status["external_stop_unconfirmed"])
        self.assertEqual(status["command_stop"], "unconfirmed")
        self.assertEqual(status["phase"], "failed")

    def test_unknown_marker_format_is_rejected_before_release(self):
        lease = client.prepare(self.prepare_args("unknown-marker-format"))
        lease.record("active", parent_command_stop="confirmed")
        client.write(lease.run / client.UNCERTAINTY_MARKER, {
            "schema": 2, "reason": "remote_stop_unconfirmed",
            "operation_id": "00000000-0000-4000-8000-000000000001",
        })
        with patch.object(lease, "cli") as call:
            with self.assertRaisesRegex(client.Error, "uncertainty marker format"):
                lease.finish()
            call.assert_not_called()

    def test_v2_status_journal_is_rejected_before_release(self):
        lease = client.prepare(self.prepare_args("v2-status"))
        status = client.read(lease.run / "status.json")
        status.update(stop_protocol=2, remote_command_stop="pending")
        client.write(lease.run / "status.json", status)
        with patch.object(lease, "cli") as call:
            with self.assertRaisesRegex(client.Error, "malformed owner status"):
                lease.finish()
            call.assert_not_called()

    def test_confirmed_command_stop_remains_finished(self):
        _run, status, stderr = self.stop_case("confirmed")
        self.assertEqual(status["phase"], "finished")
        self.assertEqual(status["command_stop"], "confirmed")
        self.assertEqual(status["lease"]["state"], "completed")
        self.assertNotIn("command stop is uncertain", stderr)

    def test_finish_status_write_failure_preserves_confirmed_resource_cleanup(self):
        run, status, stderr = self.stop_case("oserror", finish_status_fails_once=True)
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["command_stop"], "unconfirmed")
        self.assertEqual(status["lease"]["state"], "completed")
        self.assertFalse(status["lease"]["cleanup_pending"])
        self.assertIn("finish status persistence failed", status["error"])
        self.assertIn("resource cleanup is confirmed", stderr)
        self.assert_retained_after_prune(run)

    def test_command_not_started_is_distinct_from_unknown_stop(self):
        run, status, stderr = self.stop_case("not_started")
        self.assertEqual(status["phase"], "finished")
        self.assertEqual(status["command_stop"], "not_started")
        self.assertEqual(status["lease"]["state"], "completed")
        self.assertFalse((run / client.UNCERTAINTY_MARKER).exists())
        self.assertIn("diagnostic command not started", stderr)
        self.assertNotIn("command stop is uncertain", stderr)

    def test_finish_waits_through_failed_phase_until_helper_releases(self):
        lease = client.prepare(self.prepare_args("failed-before-release"))
        active = {
            "owner": "test-command", "capability": "clean", "id": "lease-one",
            "generation": 1, "slot": "test", "state": "active",
            "cleanup_pending": False, "quarantined": False,
            "endpoint": {"adb_serial": "emulator-5574"},
        }
        client.write(lease.run / "receipt.json", active)
        lease.record("active", lease=active, parent_command_stop="confirmed")
        lease.record("failed", error="helper failed before resource release")
        helper_fd = os.open(
            lease.run / "helper.lock", os.O_RDWR | os.O_CREAT, 0o600,
        )
        fcntl.flock(helper_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def stop_helper_lock():
            time.sleep(0.1)
            fcntl.flock(helper_fd, fcntl.LOCK_UN)

        thread = threading.Thread(target=stop_helper_lock)
        thread.start()
        terminal = {
            **active, "state": "completed", "endpoint": None,
            "cleanup_pending": False,
        }
        started = time.monotonic()
        try:
            with patch.object(client.Lease, "cli", return_value=terminal) as cli:
                final = client.finish(lease.run)
            self.assertGreaterEqual(time.monotonic() - started, 0.08)
            self.assertEqual(final["phase"], "finished")
            cli.assert_called_once()
        finally:
            thread.join(timeout=5)
            os.close(helper_fd)

    def seed_runs(self, count, *, finished=False):
        self.runs.mkdir(mode=0o700, exist_ok=True)
        for index in range(count):
            run = self.runs.resolve() / f"{'finished' if finished else 'failed'}-{index}"
            run.mkdir(mode=0o700)
            client.write(run / "status.json", {
                "phase": "finished" if finished else "failed", "updated_at": index,
                "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
                "parent_command_stop": "not_started" if finished else "unconfirmed",
                "external_stop_unconfirmed": False,
                "command_stop": "not_started" if finished else "unconfirmed",
                "run_directory": str(run),
                "lease": {"state": "completed", "cleanup_pending": False, "quarantined": False}
                if finished else None, "error": None if finished else "preserved unknown outcome",
            })

    def seed_prune_handoff_runs(self):
        from test_android_tools_custody import api

        self.runs.mkdir(mode=0o700, exist_ok=True)
        root = self.runs.resolve()
        config = json.loads(self.config.read_text())
        config["runs_directory"] = str(root)
        target = root / "finished-0"
        for index in range(65):
            run = root / f"finished-{index}"
            run.mkdir(mode=0o700)
            client.write(run / "status.json", {
                "phase": "finished", "updated_at": index,
                "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
                "parent_command_stop": "confirmed",
                "external_stop_unconfirmed": False,
                "command_stop": "confirmed",
                "run_directory": str(run),
                "lease": {"state": "completed", "cleanup_pending": False,
                          "quarantined": False},
                "error": None,
            })
            if run == target:
                client.write(run / "binding.json", {
                    "schema": 1, "config": config, "profile": "ui.clean",
                    "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
                    "capability": "clean", "owner": "test-command", "binding": None,
                })
                client.write(run / "receipt.json", {})
                client.write(run / "helper.lock", {})
        return api, target

    def test_prune_rechecks_after_handoff_commits_before_final_eligibility(self):
        self.prune_recheck_case(external=True)

    def test_prune_rechecks_after_error_commits_before_final_eligibility(self):
        self.prune_recheck_case(external=False)

    def prune_recheck_case(self, *, external):
        api, target = self.seed_prune_handoff_runs()
        helper_lock_held = threading.Event()
        continue_prune = threading.Event()
        prune_done = threading.Event()
        prune_errors = []
        original_lock = client.lock

        @contextmanager
        def pause_after_helper_lock(run):
            with original_lock(run):
                if run == target:
                    helper_lock_held.set()
                    if not continue_prune.wait(5):
                        raise AssertionError("prune final check was not released")
                yield

        def run_prune():
            try:
                client.prune(self.runs.resolve())
            except BaseException as error:
                prune_errors.append(error)
            finally:
                prune_done.set()

        with patch.object(client, "lock", pause_after_helper_lock), patch.dict(
                os.environ, {"RESOURCE_LEASE_RUN_DIRECTORY": str(target)}):
            thread = threading.Thread(target=run_prune, name="prune-before-final-check")
            thread.start()
            try:
                self.assertTrue(helper_lock_held.wait(5), "prune did not reach helper lock")
                if external:
                    api.handoff_uncertain("host_interrupted")
                else:
                    client.load(target).record("finished", error="late lifecycle failure")
                status = client.read(target / "status.json")
                if external:
                    self.assertTrue(status["external_stop_unconfirmed"])
                    self.assertEqual(status["phase"], "failed")
                else:
                    self.assertEqual(status["error"], "late lifecycle failure")
            finally:
                continue_prune.set()
                thread.join(timeout=10)

        self.assertTrue(prune_done.is_set())
        self.assertEqual(prune_errors, [])
        self.assertTrue(target.is_dir(), "a committed failure must remain in history")
        status = client.read(target / "status.json")
        if external:
            self.assertTrue(status["external_stop_unconfirmed"])
        else:
            self.assertEqual(status["error"], "late lifecycle failure")

    def test_prune_delete_lock_prevents_late_handoff_false_acknowledgement(self):
        api, target = self.seed_prune_handoff_runs()
        status_path = target / "status.json"
        deletion_started = threading.Event()
        continue_delete = threading.Event()
        handoff_lock_attempted = threading.Event()
        handoff_done = threading.Event()
        prune_done = threading.Event()
        handoff_lock_result = {}
        handoff_result = {}
        prune_errors = []
        target_info = target.stat()
        original_iterdir = Path.iterdir
        original_unlink = Path.unlink
        original_flock = fcntl.flock

        def ordered_iterdir(path):
            entries = list(original_iterdir(path))
            if path == target:
                entries.sort(key=lambda entry: entry.name != "status.json")
            return iter(entries)

        def pause_before_status_unlink(path, *args, **kwargs):
            if path == status_path and threading.current_thread().name == "prune-delete":
                deletion_started.set()
                if not continue_delete.wait(5):
                    raise AssertionError("status deletion was not released")
            return original_unlink(path, *args, **kwargs)

        def observe_real_flock(fd, flags):
            info = os.fstat(fd)
            is_handoff_directory = (
                threading.current_thread().name == "global-handoff"
                and (info.st_dev, info.st_ino) == (target_info.st_dev, target_info.st_ino)
                and "result" not in handoff_lock_result
            )
            try:
                result = original_flock(fd, flags)
            except BlockingIOError:
                if is_handoff_directory:
                    handoff_lock_result["result"] = "contended"
                    handoff_lock_attempted.set()
                raise
            else:
                if is_handoff_directory:
                    handoff_lock_result["result"] = "acquired"
                    handoff_lock_attempted.set()
                return result

        def run_prune():
            try:
                client.prune(self.runs.resolve())
            except BaseException as error:
                prune_errors.append(error)
            finally:
                prune_done.set()

        def run_handoff():
            try:
                api.handoff_uncertain("host_interrupted")
            except BaseException as error:
                handoff_result.update(success=False, error=error)
            else:
                handoff_result["success"] = True
            finally:
                handoff_done.set()

        with patch.object(Path, "iterdir", ordered_iterdir), patch.object(
                Path, "unlink", pause_before_status_unlink), patch.object(
                fcntl, "flock", observe_real_flock), patch.dict(
                    os.environ, {"RESOURCE_LEASE_RUN_DIRECTORY": str(target)}):
            prune_thread = threading.Thread(target=run_prune, name="prune-delete")
            prune_thread.start()
            try:
                self.assertTrue(deletion_started.wait(5), "prune did not reach deletion")
                handoff_thread = threading.Thread(target=run_handoff, name="global-handoff")
                handoff_thread.start()
                self.assertTrue(
                    handoff_lock_attempted.wait(5),
                    "global handoff did not attempt the run-directory lock",
                )
                first_lock_result = handoff_lock_result.get("result")
                if first_lock_result == "acquired":
                    handoff_done.wait(5)
                else:
                    self.assertFalse(
                        handoff_done.is_set(),
                        "handoff returned while prune held the run-directory lock",
                    )
            finally:
                continue_delete.set()
                prune_thread.join(timeout=10)
                if "handoff_thread" in locals():
                    handoff_thread.join(timeout=15)

        self.assertTrue(prune_done.is_set())
        self.assertEqual(prune_errors, [])
        self.assertTrue(handoff_done.is_set())
        self.assertFalse(
            handoff_result.get("success"),
            "a late handoff must not acknowledge a run that prune deleted",
        )
        self.assertEqual(handoff_lock_result.get("result"), "contended")
        self.assertFalse(target.exists())

    def test_concurrent_admission_uses_independent_root_lock_handles(self):
        self.seed_runs(255)
        first, second = self.prepare_args("first"), self.prepare_args("second")
        first_at_mkdir, allow_first, second_contended = (threading.Event() for _ in range(3))
        original_mkdir, original_flock = Path.mkdir, client.fcntl.flock
        handles = set()

        def mkdir(path, *args, **kwargs):
            if path == Path(first.run_dir):
                first_at_mkdir.set()
                if not allow_first.wait(5):
                    raise AssertionError("first admission was not released")
            return original_mkdir(path, *args, **kwargs)

        def flock(fd, flags):
            handles.add(fd)
            try:
                return original_flock(fd, flags)
            except BlockingIOError:
                second_contended.set()
                raise

        with (patch.object(Path, "mkdir", mkdir),
              patch.object(client.fcntl, "flock", side_effect=flock),
              ThreadPoolExecutor(max_workers=2) as executor):
            first_future = executor.submit(client.prepare, first)
            try:
                self.assertTrue(first_at_mkdir.wait(5), "first admission did not reach reservation")
                second_future = executor.submit(client.prepare, second)
                self.assertTrue(second_contended.wait(5), "independent flock handle did not contend")
            finally:
                allow_first.set()
            self.assertEqual(first_future.result(timeout=5).run, Path(first.run_dir))
            with self.assertRaisesRegex(client.Error, "retention limit"):
                second_future.result(timeout=5)
        self.assertGreaterEqual(len(handles), 2)
        self.assertEqual(len([path for path in self.runs.iterdir()
                              if path.name != "admission.lock"]), 256)
        self.assertEqual(len(list(self.runs.glob("failed-*"))), 255)
        self.assertEqual(client.prepare(first).run, Path(first.run_dir))
        with self.assertRaisesRegex(client.Error, "retention limit"):
            client.prepare(second)
        self.assertEqual((self.runs / "admission.lock").stat().st_mode & 0o777, 0o600)

    def test_admission_counts_unknown_entries_but_not_lock_metadata(self):
        self.seed_runs(255)
        client.write(self.runs / "unknown-entry", {})
        args = self.prepare_args("new")
        with self.assertRaisesRegex(client.Error, "retention limit"):
            client.prepare(args)
        self.assertFalse(Path(args.run_dir).exists())
        self.assertTrue((self.runs / "unknown-entry").exists())

    def test_pruning_preserves_failed_runs_and_opens_exactly_one_slot(self):
        self.seed_runs(191)
        self.seed_runs(65, finished=True)
        client.prepare(self.prepare_args("new"))
        self.assertEqual(len(list(self.runs.glob("failed-*"))), 191)
        self.assertEqual(len(list(self.runs.glob("finished-*"))), 64)
        self.assertEqual(len([path for path in self.runs.iterdir()
                              if path.name != "admission.lock"]), 256)

    def test_admission_lock_wait_is_bounded_without_reservation(self):
        self.runs.mkdir(mode=0o700)
        args = self.prepare_args("blocked")
        with client.admission_lock(self.runs):
            with (patch.object(client, "CLI_TIMEOUT", 0.02),
                  self.assertRaisesRegex(client.Busy, "admission lock timed out")):
                client.prepare(args)
        self.assertFalse(Path(args.run_dir).exists())

    def test_unsafe_admission_locks_fail_closed(self):
        for kind in ("symlink", "directory", "public", "fifo"):
            with self.subTest(kind=kind):
                runs = self.root / f"runs-{kind}"
                runs.mkdir(mode=0o700)
                lock = runs / "admission.lock"
                if kind == "symlink":
                    target = self.root / "lock-target"
                    client.write(target, {"untouched": True})
                    lock.symlink_to(target)
                elif kind == "directory":
                    lock.mkdir(mode=0o700)
                elif kind == "fifo":
                    os.mkfifo(lock, mode=0o600)
                else:
                    client.write(lock, {})
                    lock.chmod(0o644)
                config = self.root / f"config-{kind}.json"
                client.write(config, dict(client.read(self.config), runs_directory=str(runs)))
                args = self.prepare_args("rejected", runs=runs, config=config)
                with self.assertRaises((client.Error, OSError)):
                    client.prepare(args)
                self.assertFalse(Path(args.run_dir).exists())
                if kind == "symlink":
                    self.assertEqual(client.read(target), {"untouched": True})

    def test_admission_lock_path_is_not_a_consumer_run(self):
        with self.assertRaisesRegex(client.Error, "direct child"):
            client.prepare(self.prepare_args("admission.lock"))
        self.assertFalse((self.runs / "admission.lock").exists())

    def test_queue_cancellation_uses_original_receipt(self):
        args = type("Args", (), {"config": str(self.config), "profile": "ui.clean",
                                 "owner": "test-command"})()
        lease = client.prepare(args)
        client.write(lease.run / "receipt.json", {})
        with patch.object(client.Lease, "cli", return_value={
            "owner": "test-command", "capability": "clean", "state": "cancelled",
            "cleanup_pending": False, "quarantined": False,
        }) as call:
            result = lease.finish(cancel=True)
            self.assertEqual(result["phase"], "finished")
            self.assertEqual(call.call_args.args[0], "cancel")

    def test_no_receipt_after_acquire_stopped_means_no_registered_request(self):
        lease = client.prepare(self.prepare_args("no-receipt"))
        with patch.object(client.Lease, "cli") as call:
            result = lease.finish(cancel=True)
            self.assertEqual(result["phase"], "finished")
            self.assertFalse(result["lease"]["request_created"])
            call.assert_not_called()

    def spawn(self, args):
        child = subprocess.Popen([sys.executable, str(SCRIPT), *args], env=self.environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(child.stderr.close)
        self.addCleanup(child.stdout.close)
        self.addCleanup(client.stop_child, child)
        return child

    def await_call(self, name):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (self.pool / "calls").exists() and name in self.calls():
                return
            time.sleep(0.02)
        self.fail(f"missing expected CLI call {name}")

    def test_signal_during_queue_cancels_same_request(self):
        (self.pool / "queued").touch()
        child = self.spawn(self.run_args("raise SystemExit(99)"))
        self.await_call("acquire")
        child.send_signal(signal.SIGTERM)
        _stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 1, stderr)
        self.assertEqual(self.calls().count("acquire"), 1)
        self.assertEqual(self.calls()[-1], "cancel")
        self.assertIn('"command_exit": null', stderr)

    def test_signal_stops_owned_command_and_releases(self):
        child = self.spawn(self.run_args("import time; time.sleep(30)"))
        self.await_call("renew")
        child.send_signal(signal.SIGTERM)
        _stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 1, stderr)
        self.assertEqual(self.calls()[-1], "release")

    def test_successful_command_with_quarantine_is_failure(self):
        (self.pool / "quarantine").touch()
        result = self.invoke(*self.run_args("pass"))
        self.assertEqual(result.returncode, 1)
        self.assertIn('"command_exit": 0', result.stderr)
        self.assertIn("cleanup failed", result.stderr)

    def test_abrupt_wrapper_loss_stops_heartbeat_without_descendant_claim(self):
        # The command self-terminates; this test never adopts a grandchild PID.
        marker = self.root / "command-finished"
        child = self.spawn(self.run_args(
            f"import time,pathlib; time.sleep(1.4); pathlib.Path({str(marker)!r}).touch()"))
        self.await_call("renew")
        child.kill()
        child.wait(timeout=5)
        before = self.calls().count("renew")
        deadline = time.monotonic() + 4
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "the bounded command must finish itself")
        time.sleep(0.7)
        self.assertEqual(self.calls().count("renew"), before)
        run = next(path for path in self.runs.iterdir() if path.is_dir())
        lease = client.load(run)

        def cleanup_unknown_command():
            final = lease.finish()
            self.assertEqual(final["phase"], "failed")
            status = client.read(run / "status.json")
            self.assertEqual(status["phase"], "failed")
            self.assertEqual(status["command_stop"], "unconfirmed")
            self.assertEqual(status["lease"]["state"], "completed")

        self.addCleanup(cleanup_unknown_command)
        self.assertEqual(lease.cli("inspect")["state"], "expired")
        # The descendant survived wrapper death until its own bounded exit.
        self.assertNotIn("release", self.calls())

    def test_scoped_adapter_sync_omits_retired_reconciliation_reader(self):
        synchronizer = runpy.run_path(str(ROOT / "scripts/sync-project-adapters"))
        self.assertEqual(synchronizer["ADAPTER_TEMPLATES"]["resource_lease"], SCRIPT)
        self.assertIn("kit_runtime_loader", synchronizer["COMMAND_TEMPLATES"])
        target = self.root / "project/.kent/adapters/mobile/resource-lease-client"
        plan = synchronizer["plan_synchronization"](
            SCRIPT, target, key="resource_lease", update=False)
        synchronizer["apply_synchronization"](plan)
        self.assertEqual(target.read_bytes(), SCRIPT.read_bytes())
        self.assertTrue(os.access(target, os.X_OK))
        profile = SimpleNamespace(
            schema_version=3, kit_managed_adapters=["resource_lease"],
            kit_managed_commands=[], commands={},
            adapters={"resource_lease": ".kent/adapters/mobile/resource-lease-client"},
        )
        plans, _ = synchronizer["plan_project_synchronization"](
            profile, (self.root / "project").resolve(), update=False)
        self.assertEqual({plan.target.name for plan in plans}, {"resource-lease-client"})
        for plan in plans:
            synchronizer["apply_synchronization"](plan)
            self.assertEqual(plan.target.read_bytes(), SCRIPT.read_bytes())

    def test_legacy_reconciliation_command_is_not_published(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("reconcile", result.stdout)

    def test_recovery_reader_is_not_documented_as_a_kit_interface(self):
        contract = (ROOT / "contracts/resource-lease-integration.md").read_text()
        self.assertNotIn("resource-lease-client reconcile", contract)
        self.assertNotIn("resource_lease_stop_proof.py", contract)
        self.assertIn("## Configuration and interface", contract)
        self.assertIn("## Stop provenance", contract)
        self.assertIn("external_stop_unconfirmed", contract)
        self.assertIn("## Native owner observation", contract)


class ManagedLeaseTests(unittest.TestCase):
    project = "project-33333333-3333-4333-8333-333333333333"
    workflow = "44444444-4444-4444-8444-444444444444"

    def setUp(self):
        fixture = ResourceLeaseClientTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        temp = tempfile.TemporaryDirectory(dir=Path.home().resolve())
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        root = self.home / ".config/kent/android-tools"
        root.mkdir(parents=True, mode=0o700)
        self.registration = root / "lease-routes.json"
        self.root = root / "managed-runs"
        self.workspace, self.checkout = self.home / "primary", self.home / "linked"
        commands = [
            ["init", "-q", str(self.workspace)],
            ["-C", str(self.workspace), "-c", "user.name=Fixture", "-c",
             "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
             "commit", "-q", "--allow-empty", "-m", "Fixture"],
            ["-C", str(self.workspace), "worktree", "add", "-q", "--detach", str(self.checkout)],
        ]
        for command in commands:
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *command],
                           check=True, capture_output=True, timeout=10)
        self.kent_state = self.home / "kent-state"
        self.kent_state.mkdir(mode=0o700)
        self.node = self.kent_state / "node"
        self.node.write_text("Required Runtime Smoke")
        self.kent = self.home / "kent"
        self.kent.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
assert os.getcwd() == "/" and os.environ["HOME"] == {str(self.home)!r}
assert os.environ["KENT_PERSISTENCE_ROOT"] == {str(self.kent_state)!r}
assert os.environ["PATH"] == "/usr/bin:/bin"
a = sys.argv[1:]
if a[:2] == ["worktree", "status"]:
    assert "KENT_SESSION_ID" not in os.environ
    assert "KENT_STEP_ID" not in os.environ
    session = a[a.index("--session") + 1]
    root = ({str(self.checkout)!r}
            if session in {{{SESSION!r}, {ACTOR_SESSION!r}}}
            else {str(self.workspace)!r})
    linked = ({{"root": root, "availability": "available"}}
              if root == {str(self.checkout)!r} else None)
    print(json.dumps({{"problems": [], "target": {{
        "workspace_root": {str(self.workspace)!r}, "workspace_availability": "available",
        "effective_workdir": root, "worktree": linked
    }}, "worktree": {{"recorded_root": root, "observed_root": root}}}}))
elif a[:2] == ["task", "show"]:
    print(json.dumps({{"summary": {{"id": a[2], "done": False, "project_id": {self.project!r},
          "workflow_id": {self.workflow!r}}}, "status": {{"kind": "running", "native_state": "running"}}}}))
else:
    if a and a[0] == "run":
        Path({str(self.home / "native-run-invoked")!r}).touch()
        raise SystemExit(2)
    print(json.dumps({{"task_id": a[2], "items": [{{"session_id": {SESSION!r}, "status": "running",
          "node_name": Path({str(self.node)!r}).read_text()}}]}}))
""")
        self.kent.chmod(0o700)
        self.base_kent = self.kent.read_text()
        self.config = self.home / "config.json"
        self.allocator = self.home / "allocator"
        self.allocator_log = self.home / "allocator-environment"
        self.allocator.write_text(fixture.executable.read_text().replace(
            "#!/usr/bin/env python3", f"#!{sys.executable}", 1).replace(
                "action = args[2]", f"""action = args[2]
with open({str(self.allocator_log)!r}, "a") as log:
    environment = {{key: os.environ[key] if key in {{"HOME", "PATH"}} else None
                   for key in os.environ}}
    print(json.dumps([action, os.getcwd(), environment]), file=log)""",
                1,
            ))
        self.allocator.chmod(0o700)
        client.write(self.config, {**json.loads(fixture.config.read_text()),
                                   "executable": str(self.allocator)})
        self.registry = {"schema": 1, "kent_executable": str(self.kent),
                         "kent_persistence_root": str(self.kent_state),
                         "routes": [{"workspace": str(self.workspace), "project": self.project,
                                     "workflow_id": self.workflow, "smoke_node": "Required Runtime Smoke",
                                     "config": str(self.config)}]}
        client.write(self.registration, self.registry)
        # Inject only the OS account home into a disposable copy. The copy
        # then launches the actual helper and allocator as separate processes.
        self.installation = self.home / ".kent/tools/android"
        self.installation.mkdir(parents=True, mode=0o700)
        self.script = self.installation / "resource-lease-client"
        self.script.write_text(SCRIPT.read_text().replace(
            "import pwd\n",
            f"import pwd\nfrom types import SimpleNamespace\n"
            f"pwd.getpwuid = lambda _: SimpleNamespace(pw_dir={str(self.home)!r})\n", 1))
        self.script.chmod(0o700)
        self.environment = {**fixture.environment, "KENT_SESSION_ID": SESSION,
                            "KENT_STEP_ID": STEP,
                            "HOME": "/not-account-home", "KENT_PERSISTENCE_ROOT": "/not-native-state",
                            "KENT_MANAGED_OBSERVER": "untrusted context"}
        self.environment.pop("RESOURCE_LEASE_CONFIG", None)
        self.attempt = str(uuid.uuid4())

    def install_android_facade(self):
        for file in ("android-tools", "android_tools_custody.py",
                     "android_tools_evidence.py", "android_tools_native.py"):
            shutil.copy2(ROOT / "templates/project" / file, self.installation / file)

    def dispatch_android(self, arguments, *, cwd=None, environ=None, action="key", program=None):
        entry = (
            "import pwd,runpy,sys; from types import SimpleNamespace; "
            "home,launcher,*args=sys.argv[1:]; "
            "pwd.getpwuid=lambda _: SimpleNamespace(pw_dir=home); "
            "sys.argv=[launcher,*args]; runpy.run_path(launcher,run_name='__main__')"
        )
        return subprocess.run([
            sys.executable, "-I", "-B", "-c", entry, str(self.home),
            str(program or self.installation / "android-tools"),
            "--attempt", self.attempt, "device", action, "--arguments", json.dumps(arguments),
        ], env=environ or self.environment, cwd=cwd or self.checkout,
            capture_output=True, text=True, timeout=15)

    def invoke(self, action, *, attempt=None, cwd=None, env=None, profile="smoke.persistent"):
        args = [action, "--attempt", attempt or self.attempt]
        if action == "begin":
            args += ["--task", TASK, "--profile", profile]
        return subprocess.run([sys.executable, str(self.script), *args],
                              cwd=cwd or self.checkout, env=env or self.environment,
                              capture_output=True, text=True, timeout=20)

    def assert_result(self, result, phase):
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["phase"], phase)
        self.assertNotIn(str(self.home), result.stdout)
        return payload

    def standalone_only_route(self, settings=None):
        return {
            "project": self.project, "workspace": str(self.workspace),
            "config": str(self.config),
            "android_config": str(settings or self.home / "unused-android.json"),
            "standalone_intents": ["ui.clean"],
            "standalone_only": {"caller_session": SESSION, "checkout": str(self.checkout)},
        }

    def agent_command(self, *, cwd=None, env=None):
        return subprocess.run(
            [sys.executable, str(self.script), "agent", "--profile", "ui.clean",
             "--prompt", "Run the isolated fixture."],
            cwd=cwd or self.checkout, env=env or self.environment,
            capture_output=True, text=True, timeout=25,
        )

    def test_standalone_only_route_denies_workflow_before_task_observation(self):
        route = self.standalone_only_route()
        task_probe = self.home / "task-probe"
        self.kent.write_text(self.base_kent.replace(
            'elif a[:2] == ["task", "show"]:\n',
            'elif a[:2] == ["task", "show"]:\n'
            f'    Path({str(task_probe)!r}).touch()\n',
            1,
        ))
        self.assert_result(self.invoke("begin"), "active")
        self.assertTrue(task_probe.is_file())

        client.write(self.registration, {**self.registry, "routes": [route]})
        self.assert_result(self.invoke("end"), "finished")
        task_probe.unlink()
        calls = self.fixture.calls()
        denied_attempt = str(uuid.uuid4())
        denied = self.invoke("begin", attempt=denied_attempt)
        self.assertEqual(denied.returncode, 1)
        self.assertFalse(task_probe.exists())
        self.assertFalse((self.root / denied_attempt).exists())
        self.assertEqual(self.fixture.calls(), calls)

    def test_standalone_only_route_authority_has_closed_shape(self):
        route = self.standalone_only_route()
        invalid = [
            dict(route, workflow_id=self.workflow),
            dict(route, smoke_node="Required Runtime Smoke"),
            dict(route, standalone_only=None),
            dict(route, standalone_only={"caller_session": SESSION}),
            dict(route, standalone_only={**route["standalone_only"], "extra": True}),
            dict(route, standalone_only={**route["standalone_only"], "checkout": "relative"}),
            dict(route, standalone_only={**route["standalone_only"], "caller_session": "invalid"}),
            dict(route, standalone_intents=[]),
            {key: value for key, value in route.items() if key != "android_config"},
            {key: value for key, value in route.items() if key != "standalone_intents"},
            {key: value for key, value in self.registry["routes"][0].items() if key != "workflow_id"},
        ]
        with patch.object(client.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home))):
            client.write(self.registration, {**self.registry, "routes": [route]})
            self.assertEqual(client.HostRouting().route(self.workspace), route)
            for bad in invalid:
                with self.subTest(route=bad):
                    client.write(self.registration, {**self.registry, "routes": [bad]})
                    with self.assertRaises(client.Error):
                        client.HostRouting().route(self.workspace)

    def test_standalone_only_route_denies_other_native_caller_and_valid_link(self):
        client.write(self.registration, {**self.registry, "routes": [self.standalone_only_route()]})
        foreign_link = self.home / "foreign-link"
        subprocess.run(
            ["git", "-C", str(self.workspace), "worktree", "add", "--detach", str(foreign_link)],
            check=True, capture_output=True, timeout=10,
        )
        wrong_caller = dict(self.environment, KENT_SESSION_ID=ACTOR_SESSION)
        self.assertIn("caller or checkout is not enrolled", self.agent_command(env=wrong_caller).stderr)
        # Native observation also reports the other checkout; only enrollment
        # should deny it, not the existing caller-to-cwd consistency check.
        self.kent.write_text(self.base_kent.replace(repr(str(self.checkout)), repr(str(foreign_link))))
        self.assertIn("caller or checkout is not enrolled", self.agent_command(cwd=foreign_link).stderr)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / "native-run-invoked").exists())
        self.assertFalse((self.fixture.pool / "calls").exists())

    def test_standalone_only_pool_overlap_blocks_both_selected_routes(self):
        pilot = self.standalone_only_route()
        second_primary = self.home / "second-primary"
        second_primary.mkdir()
        workflow = dict(self.registry["routes"][0], workspace=str(second_primary))
        same_pool_config = self.home / "same-pool.json"
        client.write(same_pool_config, json.loads(self.config.read_text()))
        with patch.object(client.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home))):
            for other in (workflow, dict(workflow, config=str(same_pool_config))):
                with self.subTest(config=other["config"]):
                    client.write(self.registration, {**self.registry, "routes": [pilot, other]})
                    host = client.HostRouting()
                    for workspace in (self.workspace, second_primary):
                        with self.assertRaisesRegex(client.Error, "requires a dedicated pool"):
                            host.config(host.route(workspace))
        for other in (workflow, dict(workflow, config=str(same_pool_config))):
            client.write(self.registration, {**self.registry, "routes": [pilot, other]})
            self.assertIn("requires a dedicated pool", self.agent_command().stderr)
            self.kent.write_text(self.base_kent.replace(repr(str(self.workspace)), repr(str(second_primary))))
            try:
                self.assertEqual(self.invoke("begin").returncode, 1)
            finally:
                self.kent.write_text(self.base_kent)
            self.assertFalse((self.root / self.attempt).exists())
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / "native-run-invoked").exists())
        self.assertFalse((self.fixture.pool / "calls").exists())

    def test_route_accepts_closed_standalone_intents(self):
        route = dict(self.registry["routes"][0], standalone_intents=["ui.clean"])
        unrelated = self.home / "unrelated-primary"
        unrelated.mkdir()
        legacy_route = dict(
            self.registry["routes"][0],
            project="project-55555555-5555-4555-8555-555555555555",
            workspace=str(unrelated),
        )
        client.write(self.registration, {**self.registry, "routes": [route, legacy_route]})
        with patch.object(client.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home))):
            host = client.HostRouting()
            self.assertEqual(host.route(self.workspace)["standalone_intents"], ["ui.clean"])
            self.assertNotIn("standalone_intents", host.route(unrelated))

        for invalid in (["ui.clean", "ui.clean"], [""], "ui.clean", [None]):
            with self.subTest(invalid=invalid):
                client.write(self.registration, {
                    **self.registry,
                    "routes": [dict(route, standalone_intents=invalid), legacy_route],
                })
                with patch.object(
                    client.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home)),
                ), self.assertRaises(client.Error):
                    client.HostRouting().route(self.workspace)

    def test_standalone_terminal_requires_original_identity_and_positive_null_endpoint(self):
        run = self.home / "terminal-proof"
        run.mkdir(mode=0o700)
        metadata = {"owner": "standalone:attempt", "capability": "clean"}
        original = {
            "id": "lease-one", "generation": 1, "slot": "slot",
            "owner": metadata["owner"], "capability": metadata["capability"],
        }
        client.write(run / "status.json", {
            "phase": "active", "updated_at": time.monotonic(),
            "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
            "parent_command_stop": "confirmed", "external_stop_unconfirmed": False,
            "command_stop": "confirmed", "run_directory": str(run),
            "lease": original, "error": None,
        })
        terminal = {
            **original, "generation": None, "state": "completed", "endpoint": None,
            "cleanup_pending": False, "quarantined": False,
        }
        client.validate_standalone_terminal(run, metadata, terminal)
        for change in (
            {"state": "expired"},
            {"endpoint": {"adb_serial": "emulator-5574"}},
            {"endpoint": None, "cleanup_pending": True},
            {"endpoint": None, "quarantined": True},
            {"endpoint": None, "id": "replacement"},
            {"endpoint": None, "owner": "foreign"},
            {"endpoint": None, "generation": True},
            {"endpoint": None, "error": "retained"},
        ):
            candidate = {**terminal, **change}
            with self.subTest(change=change), self.assertRaises(client.Error):
                client.validate_standalone_terminal(run, metadata, candidate)
        missing = dict(terminal)
        missing.pop("generation")
        with self.assertRaises(client.Error):
            client.validate_standalone_terminal(run, metadata, missing)
        missing = dict(terminal)
        missing.pop("endpoint")
        with self.assertRaises(client.Error):
            client.validate_standalone_terminal(run, metadata, missing)
        finished = {
            "phase": "finished", "updated_at": time.monotonic(),
            "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
            "parent_command_stop": "confirmed", "external_stop_unconfirmed": False,
            "command_stop": "confirmed", "run_directory": str(run),
            "lease": {**terminal}, "error": None,
        }
        client.validate_standalone_status(run, finished)
        for change in (
            {"error": "retained"},
            {"parent_command_stop": "unconfirmed", "command_stop": "unconfirmed"},
            {"external_stop_unconfirmed": True, "command_stop": "unconfirmed"},
            {"command_stop": "not_started"},
            {"lease": {**terminal, "endpoint": {"adb_serial": "emulator-5574"}}},
            {"lease": {**terminal, "cleanup_pending": True}},
            {"lease": {**terminal, "quarantined": True}},
        ):
            with self.subTest(change=change), self.assertRaises(client.Error):
                client.validate_standalone_status(run, {**finished, **change})
        marker = {
            "schema": 1, "reason": "remote_stop_unconfirmed", "operation": uuid.uuid4().hex,
        }
        client.write(run / client.UNCERTAINTY_MARKER, marker)
        with self.assertRaises(client.Error):
            client.validate_standalone_status(run, finished)

    def test_controller_stop_fences_new_provider_admission_without_holding_guard_io(self):
        run = self.home / str(uuid.uuid4())
        run.mkdir(mode=0o700)
        scope = {
            "schema": 1, "phase": "claimed", "grant_sha256": "0" * 64,
            "attempt": run.name, "project": self.project,
            "workspace": str(self.workspace), "checkout": str(self.checkout),
            "intent": "ui.clean", "session": SESSION,
            "stop_requested": False, "admission": None,
        }
        client.write(run / "controller.json", scope)
        for name in ("controller.lock", "controller-state.lock"):
            path = run / name
            path.touch(mode=0o600)
            path.chmod(0o600)
        entered = threading.Event()
        guard_open = threading.Event()
        allow_finish = threading.Event()
        stop_finished = threading.Event()
        operations = []
        failures = []

        def provider_admission():
            try:
                with client.controller_admission(run, "acquire"):
                    operations.append("acquire")
                    with client.controller_guard(run):
                        guard_open.set()
                    entered.set()
                    if not allow_finish.wait(5):
                        raise AssertionError("fixture admission was not released")
            except BaseException as error:
                failures.append(error)

        def stop():
            try:
                client.stop_controller(run)
                stop_finished.set()
            except BaseException as error:
                failures.append(error)

        with patch.object(client, "controller_is_live", return_value=True):
            provider = threading.Thread(target=provider_admission)
            provider.start()
            self.assertTrue(entered.wait(5))
            self.assertTrue(guard_open.is_set())
            stopper = threading.Thread(target=stop)
            stopper.start()
            deadline = time.monotonic() + 5
            while not client.read(run / "controller.json")["stop_requested"] \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(client.read(run / "controller.json")["stop_requested"])
            self.assertFalse(stop_finished.is_set())
            allow_finish.set()
            provider.join(timeout=5)
            stopper.join(timeout=5)
            self.assertFalse(provider.is_alive())
            self.assertFalse(stopper.is_alive())
            self.assertEqual(failures, [])
            self.assertTrue(stop_finished.is_set())
            self.assertEqual(operations, ["acquire"])
            self.assertEqual(client.read(run / "controller.json")["phase"], "closed")
            with self.assertRaises(client.Error):
                with client.controller_admission(run, "renew"):
                    operations.append("renew")
        self.assertEqual(operations, ["acquire"])

    def test_controller_stop_timeout_keeps_admission_sticky_and_unresolved(self):
        run = self.home / str(uuid.uuid4())
        run.mkdir(mode=0o700)
        scope = {
            "schema": 1, "phase": "claimed", "grant_sha256": "0" * 64,
            "attempt": run.name, "project": self.project,
            "workspace": str(self.workspace), "checkout": str(self.checkout),
            "intent": "ui.clean", "session": SESSION,
            "stop_requested": False,
            "admission": {"operation": "acquire", "nonce": "a" * 32},
        }
        client.write(run / "controller.json", scope)
        for name in ("controller.lock", "controller-state.lock"):
            path = run / name
            path.touch(mode=0o600)
            path.chmod(0o600)
        with self.assertRaisesRegex(
                client.Error, "standalone provider admission remains unresolved"):
            client.stop_controller(run, timeout=0.02)
        stopped = client.read(run / "controller.json")
        self.assertEqual(stopped["phase"], "failed")
        self.assertTrue(stopped["stop_requested"])
        self.assertEqual(stopped["admission"], scope["admission"])
        with self.assertRaises(client.Error):
            with client.controller_admission(run, "renew"):
                self.fail("timed-out admission must remain closed")

    def test_agent_reports_unresolved_when_controller_admission_does_not_drain(self):
        route = dict(self.registry["routes"][0], standalone_intents=["ui.clean"])
        config = {**client.configuration(self.config), "runs_directory": str(self.root)}
        runtime = {"checkout": str(self.checkout), "settings": {}}
        host = SimpleNamespace(
            root=self.root, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            kent=self.kent, route=lambda _workspace: route,
            target=lambda _session, exact_cwd: (self.workspace, self.checkout),
            config=lambda _route: config,
            android=lambda _route, _checkout, _config: runtime,
        )
        admission = {"operation": "acquire", "nonce": "b" * 32}

        def native_launch(_command, **options):
            options["on_started"](SimpleNamespace())
            attempt_dirs = [path for path in self.root.iterdir() if path.is_dir()]
            self.assertEqual(len(attempt_dirs), 1)
            run = attempt_dirs[0]
            state = client.read(run / "controller.json")
            state.update(phase="claimed", session=ACTOR_SESSION, admission=admission)
            client.write_controller(run, state)
            return {
                "status": "ok", "result": "native run ended", "session_id": ACTOR_SESSION,
            }, 0

        real_stop = client.stop_controller

        def short_stop(run, *, failed=False):
            return real_stop(run, failed=failed, timeout=0.02)

        args = SimpleNamespace(profile="ui.clean", prompt="fixture", run_dir=None)
        output = io.StringIO()
        with (patch.object(client, "pwd", SimpleNamespace(
                getpwuid=lambda _uid: SimpleNamespace(pw_dir=str(self.home)))),
              patch.object(client, "physical_linked_checkout",
                           return_value=(self.workspace, self.checkout)),
              patch.object(client, "capture", side_effect=native_launch),
              patch.object(client, "stop_controller", side_effect=short_stop),
              patch.dict(os.environ, {"KENT_SESSION_ID": SESSION, "KENT_STEP_ID": STEP}),
              redirect_stdout(output)):
            result = client.agent(args, host)
        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["frontend_completion"], "completed")
        self.assertEqual(report["device_cleanup"], "unresolved")
        attempt = report["attempt"]
        state = client.read(self.root / attempt / "controller.json")
        self.assertEqual(state["phase"], "failed")
        self.assertTrue(state["stop_requested"])
        self.assertEqual(state["admission"], admission)
        self.assertFalse({"release", "cancel"} & set(
            self.fixture.calls() if (self.fixture.pool / "calls").exists() else []
        ))

    def test_agent_does_not_equate_pending_question_with_successful_cleanup(self):
        route = dict(self.registry["routes"][0], standalone_intents=["ui.clean"])
        config = {**client.configuration(self.config), "runs_directory": str(self.root)}
        runtime = {"checkout": str(self.checkout), "settings": {}}
        host = SimpleNamespace(
            root=self.root, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            kent=self.kent, route=lambda _workspace: route,
            target=lambda _session, exact_cwd: (self.workspace, self.checkout),
            config=lambda _route: config,
            android=lambda _route, _checkout, _config: runtime,
        )

        def pending_question(_command, **options):
            options["on_started"](SimpleNamespace())
            run = next(path for path in self.root.iterdir() if path.is_dir())
            state = client.read(run / "controller.json")
            state.update(phase="claimed", session=ACTOR_SESSION)
            client.write_controller(run, state)
            client.write(run / "status.json", {
                "phase": "finished", "updated_at": time.monotonic(),
                "custody_marker_protocol": client.CUSTODY_MARKER_PROTOCOL,
                "parent_command_stop": "confirmed", "external_stop_unconfirmed": False,
                "command_stop": "confirmed", "run_directory": str(run),
                "lease": {
                    "id": "lease-original", "generation": None, "slot": "slot-original",
                    "owner": f"standalone:{run.name}", "capability": "clean",
                    "state": "completed", "endpoint": None,
                    "cleanup_pending": False, "quarantined": False,
                },
                "error": None,
            })
            return {
                "status": "ok", "session_id": ACTOR_SESSION,
                "outcome": {"kind": "question", "question_id": "question-1"},
            }, 0

        args = SimpleNamespace(profile="ui.clean", prompt="fixture", run_dir=None)
        output = io.StringIO()
        with (patch.object(client, "pwd", SimpleNamespace(
                getpwuid=lambda _uid: SimpleNamespace(pw_dir=str(self.home)))),
              patch.object(client, "physical_linked_checkout",
                           return_value=(self.workspace, self.checkout)),
              patch.object(client, "capture", side_effect=pending_question),
              patch.dict(os.environ, {"KENT_SESSION_ID": SESSION}),
              redirect_stdout(output)):
            result = client.agent(args, host)
        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["frontend_exit"], 0)
        self.assertEqual(report["frontend_completion"], "unresolved")
        self.assertEqual(report["device_cleanup"], "confirmed")

    def test_agent_requires_exact_native_caller_and_bidirectional_git_link_before_run(self):
        route = dict(self.registry["routes"][0], standalone_intents=["ui.clean"])
        client.write(self.registration, {**self.registry, "routes": [route]})
        cases = []
        missing = dict(self.environment)
        missing.pop("KENT_SESSION_ID")
        cases.append(("missing", missing, self.checkout))
        malformed = dict(self.environment, KENT_SESSION_ID="not-a-session")
        cases.append(("malformed", malformed, self.checkout))
        mismatched = dict(
            self.environment,
            KENT_SESSION_ID="55555555-5555-4555-8555-555555555555",
        )
        cases.append(("native-target-mismatch", mismatched, self.checkout))
        malformed_step = dict(self.environment, KENT_STEP_ID="not-a-step")
        cases.append(("malformed-step", malformed_step, self.checkout))
        cases.append(("wrong-cwd", self.environment, self.workspace))
        for label, environment, cwd in cases:
            with self.subTest(label=label):
                rejected = subprocess.run(
                    [sys.executable, str(self.script), "agent", "--profile", "ui.clean",
                     "--prompt", "Must not launch from an unqualified caller."],
                    cwd=cwd, env=environment, capture_output=True, text=True, timeout=20,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse(self.root.exists())
                self.assertFalse((self.home / "native-run-invoked").exists())
                self.assertFalse((self.fixture.pool / "calls").exists())

        marker = self.checkout / ".git"
        gitdir = Path(marker.read_text().strip()[8:]).resolve()
        backlink = gitdir / "gitdir"
        original = backlink.read_bytes()
        original_mode = backlink.stat().st_mode & 0o777
        try:
            backlink.write_text(str(self.workspace / ".git") + "\n")
            backlink.chmod(original_mode)
            rejected = subprocess.run(
                [sys.executable, str(self.script), "agent", "--profile", "ui.clean",
                 "--prompt", "Must not launch from a stale Git backlink."],
                cwd=self.checkout, env=self.environment,
                capture_output=True, text=True, timeout=20,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(self.root.exists())
            self.assertFalse((self.home / "native-run-invoked").exists())
            self.assertFalse((self.fixture.pool / "calls").exists())
        finally:
            backlink.write_bytes(original)
            backlink.chmod(original_mode)

    def test_interrupted_standalone_begin_does_not_publish_explicit_end(self):
        attempt = str(uuid.uuid4())
        grant = "A" * 43
        config = {**client.configuration(self.config), "runs_directory": str(self.root)}
        standalone = {
            "attempt": attempt, "project": self.project,
            "workspace": str(self.workspace), "checkout": str(self.checkout),
            "intent": "ui.clean", "session": None, "caller_session": SESSION,
        }
        lease = client.prepare(
            SimpleNamespace(profile="ui.clean", run_dir=str(self.root / attempt)),
            managed_config=config, standalone=standalone,
            grant_digest=client.hashlib.sha256(grant.encode("ascii")).hexdigest(),
        )
        controller = client.read(lease.run / "controller.json")
        controller["phase"] = "claimed"
        controller["session"] = str(uuid.uuid4())
        client.write_controller(lease.run, controller)
        fake_child = SimpleNamespace(poll=lambda: None)
        with (patch.object(client, "STOP", True),
              patch.object(client.subprocess, "Popen", return_value=fake_child),
              patch.object(client, "stop_controller") as stop,
              patch.object(client, "request_managed_finish") as request_finish):
            with self.assertRaisesRegex(client.Error, "startup interrupted"):
                client.launch(lease, native=lambda _scope: None, helper_env={})
        stop.assert_called_once_with(lease.run, failed=True)
        request_finish.assert_not_called()
        self.assertFalse((lease.run / "finish.json").exists())
        self.assertFalse((lease.run / "receipt.json").exists())
        calls = self.fixture.calls() if (self.fixture.pool / "calls").exists() else []
        self.assertFalse({"release", "cancel"} & set(calls))

    def test_unenabled_standalone_intent_and_failed_native_launch_do_not_allocate(self):
        route = dict(self.registry["routes"][0], standalone_intents=["not-configured"])
        client.write(self.registration, {**self.registry, "routes": [route]})
        denied = subprocess.run(
            [sys.executable, str(self.script), "agent", "--profile", "ui.clean",
             "--prompt", "Denied before native launch."],
            cwd=self.checkout, env=self.environment, capture_output=True, text=True, timeout=20,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse((self.fixture.pool / "calls").exists())

        route["standalone_intents"] = ["ui.clean"]
        failed_records = self.home / "failed-records"
        failed_records.mkdir(mode=0o700)
        failed_sdk = self.home / "failed-sdk"
        failed_adb = failed_sdk / "platform-tools/adb"
        failed_adb.parent.mkdir(parents=True)
        failed_adb.write_text("#!/bin/sh\nexit 0\n")
        failed_adb.chmod(0o700)
        failed_provider = self.home / "failed-provider"
        failed_provider.write_text("#!/bin/sh\nexit 0\n")
        failed_provider.chmod(0o700)
        failed_settings = self.home / "failed-settings.json"
        client.write(failed_settings, {
            "schema": 1, "timeout_seconds": 5, "output_limit_bytes": 4096,
            "providers": {"mobile-native": {
                "executable": str(failed_provider), "version": "1.0",
            }},
            "records_directory": str(failed_records),
            "android_sdk_root": str(failed_sdk),
        })
        route["android_config"] = str(failed_settings)
        client.write(self.registration, {**self.registry, "routes": [route]})
        invocation_marker = f"Path({str(self.home / 'native-run-invoked')!r}).touch()"
        self.assertIn(invocation_marker, self.base_kent)
        failed_kent = self.base_kent.replace(
            invocation_marker + "\n        raise SystemExit(2)",
            "raise SystemExit(7)",
        )
        self.kent.write_text(failed_kent)
        self.kent.chmod(0o700)
        failed = subprocess.run(
            [sys.executable, str(self.script), "agent", "--profile", "ui.clean",
             "--prompt", "Fail before actor claim."],
            cwd=self.checkout, env=self.environment, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(failed.returncode, 1)
        result = json.loads(failed.stdout)
        self.assertEqual(result["device_cleanup"], "not_started")
        self.assertNotIn(str(self.home), failed.stdout + failed.stderr)
        self.assertFalse((self.fixture.pool / "calls").exists())

    def test_agent_sigterm_stops_unresponsive_native_child_and_keeps_cleanup_unresolved(self):
        records = self.home / "signal-records"
        records.mkdir(mode=0o700)
        sdk = self.home / "signal-sdk"
        adb = sdk / "platform-tools/adb"
        adb.parent.mkdir(parents=True)
        adb.write_text("#!/bin/sh\nexit 0\n")
        adb.chmod(0o700)
        provider = self.home / "signal-provider"
        provider.write_text("#!/bin/sh\nexit 0\n")
        provider.chmod(0o700)
        settings = self.home / "signal-settings.json"
        client.write(settings, {
            "schema": 1, "timeout_seconds": 5, "output_limit_bytes": 4096,
            "providers": {"mobile-native": {
                "executable": str(provider), "version": "1.0",
            }},
            "records_directory": str(records), "android_sdk_root": str(sdk),
        })
        route = self.standalone_only_route(settings)
        client.write(self.registration, {**self.registry, "routes": [route]})
        child_pid_file = self.home / "native-child.pid"
        self.kent.write_text(f"""#!{sys.executable}
import json, os, signal, sys, time
from pathlib import Path
a = sys.argv[1:]
if a[:2] == ["worktree", "status"]:
    root = {str(self.checkout)!r}
    print(json.dumps({{"problems": [], "target": {{
        "workspace_root": {str(self.workspace)!r}, "workspace_availability": "available",
        "effective_workdir": root, "worktree": {{"root": root, "availability": "available"}}
    }}, "worktree": {{"recorded_root": root, "observed_root": root}}}}))
else:
    Path({str(child_pid_file)!r}).write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
""")
        self.kent.chmod(0o700)

        agent = subprocess.Popen(
            [sys.executable, str(self.script), "agent", "--profile", "ui.clean",
             "--prompt", "Wait for the host stop signal."],
            cwd=self.checkout, env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 10
        while not child_pid_file.exists() and time.monotonic() < deadline:
            if agent.poll() is not None:
                break
            time.sleep(0.02)
        self.assertTrue(child_pid_file.exists(), "native child did not start")
        native_pid = int(child_pid_file.read_text())
        agent.send_signal(signal.SIGTERM)
        stdout, stderr = agent.communicate(timeout=12)
        self.assertEqual(agent.returncode, 1, (stdout, stderr))
        result = json.loads(stdout)
        self.assertIsNone(result["frontend_exit"])
        self.assertEqual(result["frontend_completion"], "unresolved")
        self.assertEqual(result["device_cleanup"], "unresolved")
        with self.assertRaises(ProcessLookupError):
            os.kill(native_pid, 0)
        self.assertFalse((self.fixture.pool / "calls").exists())
        runs = [path for path in self.root.iterdir() if path.is_dir()]
        self.assertEqual(len(runs), 1)
        controller = client.read(runs[0] / "controller.json")
        self.assertEqual(controller["phase"], "failed")
        self.assertTrue(controller["stop_requested"])
        self.assertEqual(self.invoke("begin").returncode, 1)
        self.assertIn(
            "caller or checkout is not enrolled",
            self.agent_command(env=dict(self.environment, KENT_SESSION_ID=ACTOR_SESSION)).stderr,
        )
        self.assertFalse((self.root / self.attempt).exists())
        self.assertFalse((self.fixture.pool / "calls").exists())

    def test_fresh_agent_grant_runs_scoped_android_dispatch_and_positive_end(self):
        settings_path = self.home / "android-settings.json"
        records = self.home / "records"
        records.mkdir(mode=0o700)
        sdk = self.home / "sdk"
        adb = sdk / "platform-tools/adb"
        adb.parent.mkdir(parents=True)
        adb.write_text("#!/bin/sh\nexit 0\n")
        adb.chmod(0o700)
        provider = self.home / "provider"
        provider_log = self.home / "provider-calls.jsonl"
        provider.write_text(f"""#!{sys.executable}
import json, os, sys
with open({str(provider_log)!r}, "a") as out:
    print(json.dumps({{"argv": sys.argv[1:], "environment": dict(os.environ)}}), file=out)
if sys.argv[1:] == ["--version"]:
    print("fixture-mobile-native 1.0")
elif sys.argv[1:] != ["key", "--platform", "android", "--device", "emulator-5574", "back"]:
    raise SystemExit(2)
""")
        provider.chmod(0o700)
        client.write(settings_path, {
            "schema": 1, "timeout_seconds": 5, "output_limit_bytes": 4096,
            "providers": {"mobile-native": {
                "executable": str(provider), "version": "1.0",
            }},
            "records_directory": str(records), "android_sdk_root": str(sdk),
        })
        route = self.standalone_only_route(settings_path)
        workflow_pool = self.home / "workflow-pool"
        workflow_pool.mkdir(mode=0o700)
        workflow_workspace = self.home / "workflow-workspace"
        workflow_workspace.mkdir(mode=0o700)
        workflow_config = self.home / "workflow-config.json"
        client.write(workflow_config, {**json.loads(self.config.read_text()), "pool": str(workflow_pool)})
        workflow = dict(self.registry["routes"][0], workspace=str(workflow_workspace),
                        config=str(workflow_config))
        workflow_session = str(uuid.uuid4())
        workflow_env = dict(self.environment, KENT_SESSION_ID=workflow_session)
        client.write(self.registration, {**self.registry, "routes": [route, workflow]})
        self.install_android_facade()

        # Model the installed native launcher as a subprocess boundary. Its
        # only actor input is the host-issued prompt; tool calls remain separate
        # processes, and native target readback is independent of caller cwd.
        actor = self.home / "actor.py"
        actor.write_text(f"""import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
client = {str(self.script)!r}
launcher = {str(self.installation / "android-tools")!r}
checkout = {str(self.checkout)!r}
workspace = {str(self.workspace)!r}
home = {str(self.home)!r}
records = {str(records)!r}
managed_root = {str(self.root)!r}
registration = {str(self.registration)!r}
allocator_log = {str(self.allocator_log)!r}
provider_log = {str(provider_log)!r}
wrong_target_marker = {str(self.home / "wrong-target")!r}
session = {ACTOR_SESSION!r}
caller_session = {SESSION!r}
task = {TASK!r}
reference = sys.argv[1]
attempt, grant = reference.split(".", 1)
assert "ATTEMPT_REF" not in os.environ
assert all(reference not in item for item in os.environ.values())
def call(*args, cwd=checkout, env=None):
    result = subprocess.run([sys.executable, client, *args], cwd=cwd,
                            env=env or os.environ.copy(), capture_output=True,
                            text=True, timeout=45)
    assert reference not in result.stdout + result.stderr
    return result, json.loads(result.stdout) if result.stdout else None
bare, bare_result = call("begin", "--attempt", attempt, "--task", task,
                         "--profile", "ui.clean")
assert bare.returncode != 0
assert bare_result == {{"error": "managed_attempt_unresolved"}}
clone_env = dict(os.environ, KENT_SESSION_ID=caller_session)
clone, _ = call("begin", "--attempt", reference, "--profile", "ui.clean", env=clone_env)
assert clone.returncode != 0
wrong = reference[:-1] + ("A" if reference[-1] != "A" else "B")
denied, _ = call("begin", "--attempt", wrong, "--profile", "ui.clean")
assert denied.returncode != 0
__import__("pathlib").Path(wrong_target_marker).touch()
wrong_target, _ = call("begin", "--attempt", reference, "--profile", "ui.clean")
assert wrong_target.returncode != 0
__import__("pathlib").Path(wrong_target_marker).unlink()
with ThreadPoolExecutor(max_workers=2) as workers:
    starts = list(workers.map(
        lambda _: call("begin", "--attempt", reference, "--profile", "ui.clean"),
        range(2),
    ))
successful = [(process, payload) for process, payload in starts if process.returncode == 0]
assert len(successful) >= 1
for process, payload in starts:
    if process.returncode == 0:
        assert payload["phase"] == "active"
    else:
        assert payload == {{"error": "attempt_pending"}}
first, begun = successful[0]
assert begun["phase"] == "active", first.stderr
retry, repeated = call("begin", "--attempt", reference, "--profile", "ui.clean")
assert retry.returncode == 0 and repeated["attempt"] == attempt
foreign_env = dict(os.environ, KENT_SESSION_ID={"55555555-5555-4555-8555-555555555555"!r})
foreign, _ = call("begin", "--attempt", reference, "--profile", "ui.clean", env=foreign_env)
assert foreign.returncode != 0
wrong_cwd, _ = call("begin", "--attempt", reference, "--profile", "ui.clean", cwd=workspace)
assert wrong_cwd.returncode != 0
checked, status = call("status", "--attempt", reference)
assert checked.returncode == 0 and status["phase"] == "active"
time.sleep(2.2)
checked, renewed = call("status", "--attempt", reference)
assert checked.returncode == 0 and renewed["phase"] == "active"
device = subprocess.run(
    [sys.executable, "-I", "-B", "-c",
     "import pwd,runpy,sys; from types import SimpleNamespace; "
     "home,launcher,*args=sys.argv[1:]; "
     "pwd.getpwuid=lambda _: SimpleNamespace(pw_dir=home); "
     "sys.argv=[launcher,*args]; runpy.run_path(launcher,run_name='__main__')",
     home, launcher, "--attempt", reference, "device", "key",
     "--arguments", json.dumps({{"key": "back"}})],
    cwd=checkout, env=os.environ.copy(), capture_output=True, text=True, timeout=45,
)
assert device.returncode == 0, (device.stdout, device.stderr)
assert reference not in device.stdout + device.stderr
device_result = json.loads(device.stdout)
assert device_result["transport"] == "completed" and device_result["data"]["dispatched"], device.stdout
lost_end = subprocess.run([sys.executable, client, "end", "--attempt", reference],
                          cwd=checkout, env=os.environ.copy(), capture_output=True,
                          text=True, timeout=45)
assert lost_end.returncode == 0
assert reference not in lost_end.stdout + lost_end.stderr
route_config = json.loads(Path(registration).read_text())
route_config["routes"].remove({route!r})
staged_registry = Path(registration).with_suffix(".next")
staged_registry.write_text(json.dumps(route_config))
staged_registry.chmod(0o600)
staged_registry.replace(registration)
checked, final = call("status", "--attempt", reference)
assert checked.returncode == 0 and final["phase"] == "finished"
reopened, retained = call("begin", "--attempt", reference, "--profile", "ui.clean")
assert reopened.returncode == 0 and retained["phase"] == "finished"
for directory in (records, managed_root):
    for path in __import__("pathlib").Path(directory).rglob("*"):
        if path.is_file():
            assert reference not in path.read_text(errors="ignore")
for path in (allocator_log, provider_log):
    assert reference not in __import__("pathlib").Path(path).read_text()
""")
        actor.chmod(0o700)
        self.kent.write_text(f"""#!{sys.executable}
import json, os, re, subprocess, sys
from pathlib import Path
a = sys.argv[1:]
workspace = {str(self.workspace)!r}
checkout = {str(self.checkout)!r}
if a[:2] == ["worktree", "status"]:
    requested_session = a[a.index("--session") + 1]
    if requested_session == {workflow_session!r}:
        root = {str(workflow_workspace)!r}
        print(json.dumps({{"problems": [], "target": {{
            "workspace_root": root, "workspace_availability": "available",
            "effective_workdir": root, "worktree": None
        }}, "worktree": {{"recorded_root": root, "observed_root": root}}}}))
    elif (Path({str(self.home / "wrong-target")!r}).exists()
            or requested_session not in {{{SESSION!r}, {ACTOR_SESSION!r}}}):
        print(json.dumps({{"problems": [], "target": {{
            "workspace_root": workspace, "workspace_availability": "available",
            "effective_workdir": workspace, "worktree": None
        }}, "worktree": {{"recorded_root": workspace, "observed_root": workspace}}}}))
    else:
        print(json.dumps({{"problems": [], "target": {{
            "workspace_root": workspace, "workspace_availability": "available",
            "effective_workdir": checkout,
            "worktree": {{"root": checkout, "availability": "available"}}
        }}, "worktree": {{"recorded_root": checkout, "observed_root": checkout}}}}))
elif a and a[0] == "run":
    assert a[a.index("--workspace") + 1] == checkout
    assert "--output-mode=json" in a and "--session" not in a
    assert os.getcwd() == checkout
    assert os.environ["KENT_SESSION_ID"] == {SESSION!r}
    assert os.environ["KENT_STEP_ID"] == {STEP!r}
    launch_environment = dict(os.environ)
    for key in ("LC_CTYPE", "__CF_USER_TEXT_ENCODING"):
        launch_environment.pop(key, None)
    assert set(launch_environment) == {{
        "HOME", "PATH", "KENT_PERSISTENCE_ROOT", "KENT_SESSION_ID", "KENT_STEP_ID",
    }}
    match = re.search(r"Host-issued ATTEMPT_REF for this fresh Run: "
                      r"([0-9a-f-]{{36}}\\.[A-Za-z0-9_-]{{43}})", a[-1])
    assert match
    env = dict(os.environ, KENT_SESSION_ID={ACTOR_SESSION!r})
    env.pop("KENT_STEP_ID")
    child = subprocess.run([sys.executable, {str(actor)!r}, match.group(1)],
                          cwd=checkout, env=env, capture_output=True, text=True)
    print(json.dumps({{
        "session_id": {ACTOR_SESSION!r}, "status": "ok",
        "result": "fixture native Run completed",
    }}))
    raise SystemExit(child.returncode)
elif a[:2] == ["task", "show"]:
    print(json.dumps({{"summary": {{"id": a[2], "done": False, "project_id": {self.project!r},
          "workflow_id": {self.workflow!r}}}, "status": {{"kind": "running", "native_state": "running"}}}}))
elif a[:2] == ["task", "sessions"]:
    print(json.dumps({{"task_id": a[2], "items": [{{"session_id": {workflow_session!r},
          "status": "running", "node_name": "Required Runtime Smoke"}}]}}))
else:
    raise SystemExit(2)
""")
        self.kent.chmod(0o700)
        self.assert_result(self.invoke("begin", cwd=workflow_workspace, env=workflow_env), "active")
        self.assert_result(self.invoke("end", cwd=workflow_workspace, env=workflow_env), "finished")
        # A terminal observation may legitimately clear generation after
        # preserving the original lease ID, owner, capability and slot.
        self.allocator.write_text(self.allocator.read_text().replace(
            'value["state"] = "completed" if action == "release" else "cancelled"\n',
            'value["state"] = "completed" if action == "release" else "cancelled"\n'
            '    if action == "release": value["generation"] = None\n',
            1,
        ))
        command = subprocess.run(
            [sys.executable, str(self.script), "agent", "--profile", "ui.clean",
             "--prompt", "Run the isolated fixture and verify one borrowed Android command."],
            cwd=self.checkout, env=self.environment, capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(command.returncode, 0, (command.stdout, command.stderr))
        result = json.loads(command.stdout)
        self.assertEqual(result["frontend_exit"], 0)
        self.assertEqual(result["frontend_completion"], "completed")
        self.assertEqual(result["device_cleanup"], "confirmed")
        self.assertEqual(result["session_id"], ACTOR_SESSION)
        self.assertNotIn(str(self.home), command.stdout + command.stderr)
        self.assertNotIn(".", result["attempt"])
        self.assertEqual(self.fixture.calls().count("acquire"), 1)
        self.assertEqual(self.fixture.calls().count("release"), 1)
        self.assertGreaterEqual(self.fixture.calls().count("renew"), 2)
        self.assertEqual(json.loads(self.registration.read_text())["routes"], [workflow])
        self.assertIn("route is missing", self.agent_command().stderr)
        later = str(uuid.uuid4())
        self.assert_result(
            self.invoke("begin", attempt=later, cwd=workflow_workspace, env=workflow_env), "active",
        )
        self.assert_result(
            self.invoke("end", attempt=later, cwd=workflow_workspace, env=workflow_env), "finished",
        )
        self.assertEqual((workflow_pool / "calls").read_text().splitlines().count("acquire"), 2)
        self.assertEqual(self.fixture.calls().count("acquire"), 1)
        provider_calls = [json.loads(row) for row in provider_log.read_text().splitlines()]
        self.assertEqual(sum(row["argv"][:1] == ["key"] for row in provider_calls), 1)
        self.assertTrue(all(not any("ATTEMPT_REF" in value for value in row["environment"].values())
                            for row in provider_calls))
        invocation = json.loads(command.stdout)["session_id"]
        self.assertEqual(invocation, ACTOR_SESSION)
        self.assertTrue(all(
            "KENT_SESSION_ID" not in row["environment"]
            and "KENT_STEP_ID" not in row["environment"]
            for row in provider_calls
        ))
        allocator_environment = [json.loads(line)[2] for line in self.allocator_log.read_text().splitlines()]
        self.assertTrue(all(environment.get("KENT_SESSION_ID") is None
                            and environment.get("KENT_STEP_ID") is None
                            for environment in allocator_environment))
        runs = list((records / "runs").iterdir())
        dispatches = [
            path for path in runs
            if path.is_dir()
            and json.loads((path / "manifest.json").read_bytes())["operation"] == "device.key"
        ]
        self.assertEqual(len(dispatches), 1)
        manifest = json.loads((dispatches[0] / "manifest.json").read_bytes())
        context = manifest["context"]
        self.assertEqual(context["attempt_id"], result["attempt"])
        self.assertRegex(context["attempt_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(set(context), {"attempt_id", "generation", "lease_id", "device_serial"})
        self.assertNotIn(".", context["attempt_id"])

    def test_route_pinned_across_lost_reply_and_removed_config(self):
        first = self.assert_result(self.invoke("begin"), "active")
        try:
            client.write(self.registration, {**self.registry, "routes": []})
            self.config.unlink()
            repeated = self.assert_result(self.invoke("begin"), "active")
            checked = self.assert_result(self.invoke("status"), "active")
            self.assertEqual(first, repeated)
            self.assertEqual(first, checked)
        finally:
            self.assert_result(self.invoke("end"), "finished")
        self.assert_result(self.invoke("begin"), "finished")
        self.assertEqual(self.fixture.calls().count("acquire"), 1)
        self.assertIn("release", self.fixture.calls())

    def test_other_stage_and_checkout_cannot_allocate(self):
        self.node.write_text("Implement")
        self.assertNotEqual(self.invoke("begin").returncode, 0)
        self.node.write_text("Required Runtime Smoke")
        self.assertNotEqual(self.invoke("begin", cwd=self.workspace).returncode, 0)
        nested = self.checkout / "nested"
        subprocess.run(["git", "init", "-q", str(nested)], check=True, timeout=10)
        self.assertNotEqual(self.invoke("begin", cwd=nested).returncode, 0)
        self.assertFalse((self.fixture.pool / "calls").exists())

    def test_unsafe_registration_and_wrong_project_do_not_allocate(self):
        self.registration.chmod(0o644)
        self.assertNotEqual(self.invoke("begin").returncode, 0)
        other = dict(self.registry["routes"][0],
                     project="project-55555555-5555-4555-8555-555555555555")
        client.write(self.registration, {**self.registry, "routes": [other]})
        self.assertNotEqual(self.invoke("begin").returncode, 0)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.fixture.pool / "calls").exists())

    def test_changed_intent_cannot_replace_an_active_attempt(self):
        first = self.assert_result(self.invoke("begin"), "active")
        try:
            self.assertNotEqual(self.invoke("begin", profile="ui.clean").returncode, 0)
            self.assertEqual(self.assert_result(self.invoke("status"), "active"), first)
            self.assertEqual(self.fixture.calls().count("acquire"), 1)
        finally:
            self.assert_result(self.invoke("end"), "finished")

    def test_launched_without_receipt_is_not_confirmed_finished(self):
        run = self.root / self.attempt
        ready = self.home / "allocator-ready"
        fake = self.allocator
        fake.write_text(fake.read_text().replace(
            "if action == \"acquire\":\n",
            f"if action == 'acquire':\n"
            f"    pathlib.Path({str(ready)!r}).write_text(f'{{os.getpid()}} {{os.getppid()}}')\n"
            f"    time.sleep(30)\n"
            "if action == \"acquire\":\n", 1))
        with ThreadPoolExecutor(max_workers=1) as workers:
            starting = workers.submit(self.invoke, "begin")
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.025)
            allocator, helper = map(int, ready.read_text().split())
            try:
                os.kill(helper, signal.SIGKILL)
                self.assertNotEqual(starting.result().returncode, 0)
                self.assertFalse((run / "receipt.json").exists())
                self.assertNotEqual(self.invoke("end").returncode, 0)
                explicit = subprocess.run(
                    [sys.executable, str(self.script), "finish", "--run-dir", str(run)],
                    cwd=self.checkout, env=self.environment, capture_output=True, timeout=10)
                self.assertNotEqual(explicit.returncode, 0)
                self.assertNotEqual(client.read(run / "status.json")["phase"], "finished")
            finally:
                os.kill(allocator, signal.SIGKILL)

    def test_concurrent_begin_reuses_attempt_or_reports_pending(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            replies = list(workers.map(lambda _: self.invoke("begin"), range(2)))
        try:
            settled = self.assert_result(self.invoke("status"), "active")
            for reply in replies:
                if reply.returncode:
                    self.assertEqual(json.loads(reply.stdout), {"error": "attempt_pending"})
                else:
                    self.assertEqual(self.assert_result(reply, "active"), settled)
            self.assertEqual(self.fixture.calls().count("acquire"), 1)
        finally:
            self.assert_result(self.invoke("end"), "finished")

    def test_prepared_attempt_resumes_after_route_removal(self):
        run = self.root / self.attempt
        owner = {"project": self.project, "task": TASK, "session": SESSION}
        config = {**client.configuration(self.config), "runs_directory": str(self.root)}
        client.prepare(SimpleNamespace(profile="smoke.persistent", run_dir=str(run)),
                       owner, managed_config=config)
        client.write(self.registration, {**self.registry, "routes": []})
        self.config.unlink()
        self.assert_result(self.invoke("begin"), "active")
        try:
            self.assertEqual(self.fixture.calls().count("acquire"), 1)
        finally:
            self.assert_result(self.invoke("end"), "finished")

    def test_allocator_failure_reuses_receipt_for_end_not_for_reacquire(self):
        flag = self.fixture.pool / "reject-after-receipt"
        flag.touch()
        self.assertNotEqual(self.invoke("begin").returncode, 0)
        self.assertTrue((self.root / self.attempt / "receipt.json").is_file())
        self.assertNotEqual(self.invoke("begin").returncode, 0)
        self.assert_result(self.invoke("end"), "finished")
        self.assertEqual(self.fixture.calls().count("acquire"), 1)
        self.assertIn("release", self.fixture.calls())

    def test_managed_finish_spellings_withhold_allocator_while_marker_is_live(self):
        self.assert_result(self.invoke("begin"), "active")
        run = self.root / self.attempt
        marker = run / client.UNCERTAINTY_MARKER
        client.write(marker, {"schema": 1, "reason": "remote_stop_unconfirmed",
                              "operation": uuid.uuid4().hex})
        try:
            self.assertNotEqual(self.invoke("end").returncode, 0)
            direct = subprocess.run(
                [sys.executable, str(self.script), "finish", "--run-dir", str(run)],
                env=self.environment, cwd=self.checkout, capture_output=True, timeout=20,
            )
            self.assertNotEqual(direct.returncode, 0)
            self.assertTrue(marker.is_file())
            self.assertFalse((run / "finish.json").exists())
            self.assertFalse({"release", "cancel"} & set(self.fixture.calls()))
        finally:
            with client.marker_lock(run):
                marker.unlink()
            self.assert_result(self.invoke("end"), "finished")

    def test_managed_helper_signal_does_not_release_live_marker(self):
        self.assert_helper_loss_keeps_live_marker(signal.SIGTERM)

    def test_managed_helper_crash_does_not_release_live_marker(self):
        self.assert_helper_loss_keeps_live_marker(signal.SIGKILL)

    def assert_helper_loss_keeps_live_marker(self, interruption):
        run = self.root / self.attempt
        config = {**client.configuration(self.config), "runs_directory": str(self.root)}
        owner = {"project": self.project, "task": TASK, "session": SESSION}
        client.prepare(SimpleNamespace(profile="smoke.persistent", run_dir=str(run)),
                       owner, managed_config=config)
        environment = {**self.environment, "KENT_MANAGED_OBSERVER": json.dumps({
            **self.registry, "routes": [],
        })}
        with subprocess.Popen(
            [sys.executable, str(self.script), "_serve-managed", "--run-dir", str(run)],
            env=environment, cwd=self.checkout, start_new_session=True,
        ) as helper:
            try:
                deadline = time.monotonic() + 10
                while self.invoke("status").returncode and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assert_result(self.invoke("status"), "active")
                marker = run / client.UNCERTAINTY_MARKER
                client.write(marker, {"schema": 1, "reason": "remote_stop_unconfirmed",
                                      "operation": uuid.uuid4().hex})
                helper.send_signal(interruption)
                self.assertNotEqual(helper.wait(timeout=10), 0)
                self.assertNotEqual(self.invoke("end").returncode, 0)
                self.assertTrue(marker.is_file())
                self.assertFalse((run / "finish.json").exists())
                self.assertFalse({"release", "cancel"} & set(self.fixture.calls()))
            finally:
                if helper.poll() is None:
                    helper.kill()

    def test_managed_android_dispatch_matches_original_lease_after_route_removal(self):
        from test_android_tools_native import PNG

        android_settings = self.home / "android-settings.json"
        records = self.home / "records"
        records.mkdir(mode=0o700)
        provider = self.home / "provider"
        provider_calls = self.home / "provider-log"
        sdk = self.home / "sdk"
        adb = sdk / "platform-tools/adb"
        adb.parent.mkdir(parents=True)
        adb_log = self.home / "adb-log"
        adb.write_text(f"#!/bin/sh\n/usr/bin/touch {str(adb_log)!r}\n")
        adb.chmod(0o700)
        expected_provider_environment = {
            "HOME": str(self.home), "PATH": f"{adb.parent}:/usr/bin:/bin",
            "ANDROID_HOME": str(sdk), "ANDROID_SDK_ROOT": str(sdk),
            "ADB_SERVER_SOCKET": "tcp:127.0.0.1:5037",
        }
        provider.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
environment = dict(os.environ)
# OS/runtime can add their own locale variables to an otherwise closed env.
for key in ("LC_CTYPE", "__CF_USER_TEXT_ENCODING"):
    environment.pop(key, None)
assert os.getcwd() == "/"
assert environment == {expected_provider_environment!r}, sorted(environment)
subprocess.run(["adb"], check=True)
with Path({str(provider_calls)!r}).open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args == ["--version"]:
    print("fixture-mobile-native 1.0")
elif (args[:6] == ["screenshot", "--platform", "android", "--device",
                  "emulator-5574", "--output"] and len(args) == 7):
    Path(args[6]).write_bytes({PNG!r})
elif args != ["key", "--platform", "android", "--device", "emulator-5574", "back"]:
    raise SystemExit(2)
""")
        provider.chmod(0o700)
        settings = {
            "schema": 1, "timeout_seconds": 5, "output_limit_bytes": 4096,
            "providers": {"mobile-native": {"executable": str(provider), "version": "1.0"}},
            "records_directory": str(records), "android_sdk_root": str(sdk),
        }
        checkout_adb = self.checkout / "sdk/platform-tools/adb"
        checkout_adb.parent.mkdir(parents=True)
        shutil.copy2(adb, checkout_adb)
        unrepresentable_sdk = self.home / f"sdk{os.pathsep}split"
        unrepresentable_adb = unrepresentable_sdk / "platform-tools/adb"
        unrepresentable_adb.parent.mkdir(parents=True)
        shutil.copy2(adb, unrepresentable_adb)
        self.registry["routes"][0]["android_config"] = str(android_settings)
        client.write(self.registration, self.registry)
        for invalid in (
            {**settings, "lease": {}},
            {**settings, "timeout_seconds": 0.01},
            {key: value for key, value in settings.items() if key != "android_sdk_root"},
            {**settings, "android_sdk_root": str(self.checkout / "sdk")},
            {**settings, "android_sdk_root": str(self.home / "missing-sdk")},
            {**settings, "android_sdk_root": str(unrepresentable_sdk)},
            {**settings, "providers": {"mobile-native": {
                "executable": str(provider), "version": "bad version",
            }}},
        ):
            with self.subTest(invalid=invalid):
                client.write(android_settings, invalid)
                self.assertNotEqual(self.invoke("begin").returncode, 0)
                self.assertFalse((self.fixture.pool / "calls").exists())
        client.write(android_settings, settings)
        poison = {**self.environment, "PATH": "/untrusted-path",
                  "ANDROID_HOME": "/untrusted-sdk", "ANDROID_SDK_ROOT": "/untrusted-sdk",
                  "ADB_SERVER_SOCKET": "tcp:127.0.0.1:58471", "ANDROID_SERIAL": "emulator-9999",
                  "ANDROID_ADB_SERVER_ADDRESS": "127.0.0.1", "ANDROID_ADB_SERVER_PORT": "58472",
                  "RESOURCE_LEASE_RUN_DIRECTORY": "/foreign"}
        active = self.assert_result(self.invoke("begin", env=poison), "active")
        ended = False
        try:
            self.config.unlink()
            android_settings.unlink()
            client.write(self.registration, {**self.registry, "routes": []})
            self.assertEqual(self.assert_result(self.invoke("status"), "active"), active)
            self.install_android_facade()
            launcher = self.installation / "android-tools"

            def dispatch(arguments, *, cwd=None, environ=None, action="key", program=launcher):
                return self.dispatch_android(arguments, cwd=cwd, environ=environ or poison,
                                             action=action, program=program)

            for rejected in (
                dispatch({"key": "back", "device_serial": "emulator-5574"}),
                dispatch({"key": "back"}, cwd=self.workspace),
                dispatch({"key": "back"}, environ={**self.environment, "KENT_SESSION_ID": ""}),
            ):
                self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(provider_calls.exists())
            for file in (launcher, *self.installation.glob("android_tools_*.py")):
                shutil.copy2(file, self.checkout / file.name)
            checkout_client = self.checkout / "resource-lease-client"
            status_calls = self.home / "checkout-status-calls"
            checkout_client.write_text(
                f"from pathlib import Path\nPath({str(status_calls)!r}).touch()\n"
                f"print({json.dumps(active)!r})\n",
            )
            checkout_client.chmod(0o700)
            self.assertNotEqual(
                dispatch({"key": "back"}, program=self.checkout / launcher.name).returncode, 0,
            )
            self.assertFalse(status_calls.exists())
            self.assertFalse(provider_calls.exists())
            result = dispatch({"key": "back"})
            self.assertEqual(result.returncode, 0, (result.stderr, result.stdout))
            self.assertTrue(adb_log.is_file())
            response = json.loads(result.stdout)
            self.assertNotIn(str(self.home), result.stdout)
            self.assertNotIn("local_manifest", response)
            invocation = response["invocation_id"]
            manifest = json.loads((records / "runs" / invocation / "manifest.json").read_bytes())
            self.assertEqual(manifest["context"], {
                "attempt_id": self.attempt, "generation": active["lease"]["generation"],
                "lease_id": active["lease"]["id"], "device_serial": active["endpoint"]["adb_serial"],
            })
            captured = dispatch({"scene": "known-safe"}, action="capture")
            self.assertEqual(captured.returncode, 0, (captured.stdout, captured.stderr))
            capture = json.loads(captured.stdout)
            self.assertNotIn(str(self.home), captured.stdout)
            self.assertEqual(len(capture["artifacts"]), 1)
            self.assertNotIn("path", capture["artifacts"][0])
            self.assertEqual(capture["artifacts"][0]["media_type"], "image/png")
            capture_manifest = records / "runs" / capture["invocation_id"] / "manifest.json"
            artifact = json.loads(capture_manifest.read_bytes())["artifacts"][0]
            self.assertEqual((capture_manifest.parent / artifact["local_locator"]).read_bytes(), PNG)
            self.assertFalse((self.root / self.attempt / client.UNCERTAINTY_MARKER).exists())
            self.assert_result(self.invoke("end", env=poison), "finished")
            ended = True
            self.assertNotEqual(dispatch({"key": "back"}).returncode, 0)
            # With admission disabled and the lease settled, retained evidence
            # is still readable by this bundle without using the allocator.
            from test_android_tools import facade

            evidence = facade.load_bundle()["android_tools_evidence"]
            original = capture_manifest.read_bytes()
            reopened = evidence.open_run(records, capture["invocation_id"])
            self.assertTrue(evidence._run_info(reopened.run_directory, reopened.invocation_id)["managed"])
            self.assertEqual(reopened.manifest_path.read_bytes(), original)
            self.assertFalse((reopened.run_directory / "export.jsonl").exists())
            self.assertEqual(len(provider_calls.read_bytes().splitlines()), 4)
            self.assertEqual(self.fixture.calls().count("acquire"), 1)
            self.assertIn("release", self.fixture.calls())
            allocator_calls = [json.loads(row) for row in self.allocator_log.read_text().splitlines()]
            self.assertTrue({"acquire", "inspect", "renew", "release"} <=
                            {operation for operation, _, _ in allocator_calls})
            self.assertGreaterEqual(sum(op == "inspect" for op, _, _ in allocator_calls), 12)
            for operation, cwd, environment in allocator_calls:
                environment.pop("LC_CTYPE", None)
                environment.pop("__CF_USER_TEXT_ENCODING", None)
                self.assertEqual(cwd, "/", operation)
                self.assertEqual(environment, {"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                                 operation)
        finally:
            if not ended:
                self.assert_result(self.invoke("end"), "finished")

    def test_retained_v2_without_sdk_can_settle_but_not_dispatch(self):
        self.assert_retained_v2_cannot_dispatch()

    def test_retained_v2_with_sdk_path_separator_can_settle_but_not_dispatch(self):
        sdk = self.home / f"sdk{os.pathsep}split"
        adb = sdk / "platform-tools/adb"
        adb.parent.mkdir(parents=True)
        adb.write_text("#!/bin/sh\nexit 0\n")
        adb.chmod(0o700)
        self.assert_retained_v2_cannot_dispatch(sdk)

    def assert_retained_v2_cannot_dispatch(self, sdk=None):
        owner = {"project": self.project, "task": TASK, "session": SESSION}
        config = {**client.configuration(self.config), "runs_directory": str(self.root)}
        records = self.home / "records"
        records.mkdir(mode=0o700)
        provider_calls = self.home / "provider-unexpected-calls"
        provider = self.home / "provider"
        provider.write_text(f"#!/bin/sh\n/usr/bin/touch {str(provider_calls)!r}\n")
        provider.chmod(0o700)
        runtime = {"checkout": str(self.checkout), "settings": {
            "schema": 1, "timeout_seconds": 5, "output_limit_bytes": 4096,
            "providers": {"mobile-native": {"executable": str(provider), "version": "1.0"}},
            "records_directory": str(records),
        }}
        if sdk is not None:
            runtime["settings"]["android_sdk_root"] = str(sdk)
        with patch.object(client, "managed_root", return_value=self.root):
            client.prepare(SimpleNamespace(profile="smoke.persistent",
                                          run_dir=str(self.root / self.attempt)),
                           owner, managed_config=config, android_runtime=runtime)
        self.install_android_facade()
        self.assert_result(self.invoke("begin"), "active")
        try:
            self.assert_result(self.invoke("status"), "active")
            result = self.dispatch_android({"key": "back"})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {
                "error": "managed_device_unavailable", "effect": "not_dispatched",
            })
            self.assertFalse(provider_calls.exists())
            self.assertFalse((records / "runs").exists())
        finally:
            self.assert_result(self.invoke("end"), "finished")
        self.assertEqual(self.fixture.calls().count("acquire"), 1)

    def test_managed_completed_records_are_not_pruned(self):
        config = {**client.configuration(self.config), "runs_directory": str(self.root)}
        owner = {"project": self.project, "task": TASK, "session": SESSION}
        runs = []
        for _ in range(66):
            run = self.root / str(uuid.uuid4())
            lease = client.prepare(SimpleNamespace(profile="smoke.persistent", run_dir=str(run)),
                                   owner, managed_config=config)
            lease.finish()
            runs.append(run)
        self.assertEqual(sum(run.is_dir() for run in runs), 66)
        colliding = self.home / "legacy-config.json"
        client.write(colliding, config)
        with patch.object(client.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home))), \
                self.assertRaisesRegex(client.Error, "reserved for managed"):
            client.prepare(SimpleNamespace(profile="smoke.persistent", config=str(colliding),
                                           run_dir=str(self.root / str(uuid.uuid4())), owner="legacy"))
        self.assert_result(self.invoke("begin", attempt=runs[0].name), "finished")
        with patch.object(client, "MAX_RUNS", 66), self.assertRaisesRegex(client.Error, "retention limit"):
            client.prepare(SimpleNamespace(profile="smoke.persistent",
                                           run_dir=str(self.root / str(uuid.uuid4()))),
                           owner, managed_config=config)
        self.assertFalse((self.fixture.pool / "calls").exists())

    def test_unmarked_legacy_in_fixed_root_is_not_a_managed_attempt(self):
        run = self.root / self.attempt
        owner = {"project": self.project, "task": TASK, "session": SESSION}
        colliding = self.home / "legacy-config.json"
        client.write(colliding, {**client.configuration(self.config), "runs_directory": str(self.root)})
        client.prepare(SimpleNamespace(config=str(colliding), profile="smoke.persistent",
                                       run_dir=str(run)), owner)
        self.node.write_text("Implement")
        self.assertNotEqual(self.invoke("end").returncode, 0)
        self.assertFalse((run / "finish.json").exists())
        self.assertFalse((self.fixture.pool / "calls").exists())

    def test_marker_named_run_remains_valid_in_unrelated_legacy_root(self):
        config = client.configuration(self.config)
        root = Path(config["runs_directory"])
        for name in (".managed-only", "normal-run"):
            client.prepare(SimpleNamespace(config=str(self.config), profile="smoke.persistent",
                                           run_dir=str(root / name), owner="legacy"))
        self.assertTrue((root / "normal-run" / "binding.json").is_file())

    def test_allocator_cannot_succeed_without_exact_public_lease_identity(self):
        self.allocator.write_text(self.allocator.read_text().replace(
            '"id":"lease-one","generation":1,', '"generation":True,'))
        try:
            result = self.invoke("begin")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("adb_serial", result.stdout)
        finally:
            self.assert_result(self.invoke("end"), "finished")


@unittest.skipUnless(os.environ.get("RESOURCE_LEASE_SOURCE"),
                     "optional standalone CLI checkout integration")
class StandaloneIntegrationTests(unittest.TestCase):
    def test_real_cli_fake_sdk_modes_renewal_and_command_failure(self):
        source = Path(os.environ["RESOURCE_LEASE_SOURCE"]).resolve()
        with patch.object(sys, "path", [str(source), *sys.path]):
            fixture_module = runpy.run_path(str(source / "tests/test_resource_lease_android.py"))
        executable = source / "dist/resource-lease.pyz"
        self.assertTrue(os.access(executable, os.X_OK))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sdk = fixture_module["make_fake_android_sdk"](root / "sdk")
            port = fixture_module["free_console_port"](5600)
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"ANDROID_SERIAL", "UI_TEST_DEVICE_SERIAL"}}
            environment["RESOURCE_LEASE_HOME"] = str(root / "guards")
            for mode in ("persistent", "clean"):
                directory = root / mode
                directory.mkdir(mode=0o700)
                pool = directory / "pool"
                options = sdk.options(port)
                options["state_mode"] = mode
                state = directory / "state"
                if mode == "persistent":
                    options["state_directory"] = str(state)
                config = directory / "pool.json"
                config.write_text(json.dumps({
                    "schema": 1, "lease_seconds": 3,
                    "slots": [{"id": "test", "backend": "android",
                               "capabilities": ["test"], "options": options}],
                }))
                adapter_config = directory / "client.json"
                adapter_config.write_text(json.dumps({
                    "schema": 1, "executable": str(executable), "pool": str(pool),
                    "runs_directory": str(directory / "runs"),
                    "profiles": {"test": "test"},
                }))
                adapter_config.chmod(0o600)

                def cli(*arguments, pool=pool):
                    result = subprocess.run(
                        [str(executable), "--root", str(pool), *arguments],
                        check=False, env=environment, capture_output=True, text=True, timeout=40)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)

                cli("init", "--config", str(config))
                try:
                    cli("start")
                    for exit_code in (0, 7):
                        result = subprocess.run([
                            str(SCRIPT), "run", "--config", str(adapter_config),
                            "--profile", "test", "--owner", "integration",
                            "--env", "ANDROID_SERIAL=adb_serial", "--",
                            sys.executable, "-c",
                            ("import os,time,sys; "
                            f"assert os.environ['ANDROID_SERIAL']=='emulator-{port}'; "
                            f"time.sleep(5); sys.exit({exit_code})"),
                        ], check=False, env=environment, capture_output=True, text=True, timeout=45)
                        self.assertEqual(result.returncode, exit_code, result.stderr)
                        diagnostic = json.loads(result.stderr.splitlines()[-1])
                        status = json.loads(
                            (Path(diagnostic["run_directory"]) / "status.json").read_text())
                        self.assertEqual(status["phase"], "finished")
                        self.assertEqual(status["lease"]["state"], "completed")
                        self.assertFalse(status["lease"]["cleanup_pending"])
                        self.assertFalse(status["lease"]["quarantined"])
                        if mode == "persistent":
                            avd = next((state / "android-home/avd").glob("*.avd"))
                            marker = avd / "preservation-probe"
                            if exit_code == 0:
                                marker.write_text("preserved")
                            else:
                                self.assertEqual(marker.read_text(), "preserved")
                    calls = [call for call in sdk.calls("avdmanager")
                             if str(directory) in call["android_avd_home"]]
                    self.assertEqual(len(calls), 1 if mode == "persistent" else 2)
                finally:
                    stopped = cli("shutdown", "--wait", "20")
                    self.assertEqual(stopped["service"]["state"], "stopped")
                    self.assertEqual(stopped["slots"][0]["state"], "idle")
                    time.sleep(0.3)


if __name__ == "__main__":
    unittest.main()
