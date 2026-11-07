from __future__ import annotations

import importlib.machinery
import importlib.util
import fcntl
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr
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
