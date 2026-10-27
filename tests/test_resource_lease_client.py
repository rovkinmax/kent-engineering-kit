from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
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

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
if args[:2] == ["task", "show"]:
    print(json.dumps({"summary":{"id":args[2],"done":False},
                      "status":{"kind":"doing","native_state":"active"}}))
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
if (root/"reject").exists() and action == "renew":
    raise SystemExit(9)
if action == "acquire":
    value = {"owner":owner,"capability":args[args.index("--capability")+1],
             "id":"lease-one","state":"active","cleanup_pending":False,
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

    def test_command_renews_and_releases_exact_serial(self):
        result = self.invoke(*self.run_args(
            "import os,time; assert os.environ['ANDROID_SERIAL']=='emulator-5574'; time.sleep(1.3)"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls().count("acquire"), 1)
        self.assertGreaterEqual(self.calls().count("renew"), 2)
        self.assertEqual(self.calls()[-1], "release")

    def test_command_failure_preserved_and_cleanup_runs(self):
        result = self.invoke(*self.run_args("raise SystemExit(7)"))
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn('"command_exit": 7', result.stderr)
        self.assertEqual(self.calls()[-1], "release")

    def test_renewal_failure_cannot_pass(self):
        (self.pool / "reject").touch()
        result = self.invoke(*self.run_args("import time; time.sleep(10)"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed with exit 9", result.stderr)
        self.assertEqual(self.calls()[-1], "release")

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
                  "status": {"kind": "doing", "native_state": "active"}}
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
        lease = client.Lease(self.root, {})
        client.write(self.root / "receipt.json", {})
        for result in ({"state": "completed", "cleanup_pending": True, "quarantined": False},
                       {"state": "completed", "cleanup_pending": False, "quarantined": True},
                       {"state": "failed", "cleanup_pending": False, "quarantined": False}):
            with (patch.object(client.Lease, "cli", return_value=result),
                  self.assertRaisesRegex(client.Error, "not confirmed")):
                lease.finish()

    def test_pruning_only_proven_finished_records(self):
        self.runs.mkdir(mode=0o700)
        for index in range(66):
            run = self.runs / str(index)
            run.mkdir(mode=0o700)
            client.write(run / "status.json", {
                "phase": "finished", "updated_at": index,
                "lease": {"state": "completed", "cleanup_pending": False, "quarantined": False},
            })
        unknown = self.runs / "unknown"
        unknown.mkdir(mode=0o700)
        client.write(unknown / "status.json", {"phase": "failed"})
        client.prune(self.runs)
        self.assertEqual(len(list(self.runs.iterdir())), 65)
        self.assertTrue(unknown.exists())
        self.assertFalse((self.runs / "0").exists())

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
        lease = client.Lease(self.root, {})
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
        run = next(self.runs.iterdir())
        lease = client.load(run)
        self.addCleanup(lease.finish)
        self.assertEqual(lease.cli("inspect")["state"], "expired")
        # The descendant survived wrapper death until its own bounded exit.
        self.assertNotIn("release", self.calls())

    def test_scoped_adapter_synchronization_preserves_runtime_loader(self):
        synchronizer = runpy.run_path(str(ROOT / "scripts/sync-project-adapters"))
        self.assertEqual(synchronizer["ADAPTER_TEMPLATES"]["resource_lease"], SCRIPT)
        self.assertIn("kit_runtime_loader", synchronizer["COMMAND_TEMPLATES"])
        target = self.root / "project/.kent/adapters/mobile/resource-lease-client"
        plan = synchronizer["plan_synchronization"](
            SCRIPT, target, key="resource_lease", update=False)
        synchronizer["apply_synchronization"](plan)
        self.assertEqual(target.read_bytes(), SCRIPT.read_bytes())
        self.assertTrue(os.access(target, os.X_OK))


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
