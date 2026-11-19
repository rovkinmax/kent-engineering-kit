"""Deterministic borrowed-custody, marker identity and concurrent-consumer tests."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "templates/project/android_tools_custody.py"
spec = importlib.util.spec_from_file_location("custody_test_api", SOURCE)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = self.root / "run"
        self.run.mkdir(mode=0o700)
        self.config = {
            "schema": 1, "executable": "/qualified/resource-lease", "pool": "/pool",
            "runs_directory": str(self.root), "profiles": {"ui.clean": "clean"},
        }
        self.current = {
            "id": "original", "owner": "owner", "capability": "clean",
            "generation": 1, "slot": "slot", "state": "active",
            "service_available": True, "quarantined": False, "expires_in": 30,
            "endpoint": {"adb_serial": "emulator-5576"},
        }
        self.binding = {
            "schema": 1, "config": self.config, "profile": "ui.clean",
            "owner": "owner", "capability": "clean",
            "custody_marker_protocol": 1, "binding": None,
        }
        self.state = {"phase": "active", "updated_at": time.monotonic(),
                      "lease": self.current, "command_stop": "unconfirmed",
                      "parent_command_stop": "pending",
                      "external_stop_unconfirmed": False,
                      "run_directory": str(self.run),
                      "error": None,
                      "custody_marker_protocol": 1}
        for name, value in (("binding.json", self.binding), ("status.json", self.state),
                            ("receipt.json", {}), ("helper.lock", {})):
            self.write(name, value)
        self.owner = os.open(self.run / "helper.lock", os.O_RDONLY)
        self.addCleanup(os.close, self.owner)
        fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = patch.dict(os.environ, {"RESOURCE_LEASE_RUN_DIRECTORY": str(self.run)})
        env.start()
        self.addCleanup(env.stop)
        inspect = patch.object(api, "inspect_lease", return_value=self.current)
        self.inspect = inspect.start()
        self.addCleanup(inspect.stop)

    def write(self, name, value):
        path = self.run / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def borrow(self):
        return api.borrow_device(self.config, "emulator-5576")

    def test_public_typed_configuration_and_no_lease_ownership_calls(self):
        self.assertEqual(api.CUSTODY_API_VERSION, 1)
        config = self.write("config.json", {"lease": self.config})
        typed = api.load_lease_config(config)
        self.assertIsInstance(typed, api.LeaseConfig)
        self.assertEqual(typed.as_mapping(), self.config)
        with api.borrow_device(typed, "emulator-5576") as custody:
            self.assertEqual(custody.check(), self.current)
        for call in self.inspect.call_args_list:
            self.assertEqual(call.args[0], [
                "/qualified/resource-lease", "--root", "/pool", "inspect",
                "--owner", "owner", "--lease", str(self.run / "receipt.json"),
            ])
        self.assertEqual(set(p.name for p in self.run.iterdir()), {
            "binding.json", "status.json", "receipt.json", "helper.lock", "config.json",
        })
        with self.assertRaisesRegex(api.Error, "custody_closed"):
            custody.check()

    def test_standalone_schema_is_bound_to_one_attempt_and_native_session(self):
        attempt = str(uuid.uuid4())
        session = str(uuid.uuid4())
        workspace = self.root / "primary"
        checkout = self.root / "linked"
        run = self.root / attempt
        run.mkdir(mode=0o700)
        binding = {
            "schema": 3, "config": self.config, "profile": "ui.clean",
            "owner": f"standalone:{attempt}", "capability": "clean",
            "custody_marker_protocol": 1, "binding": None,
            "standalone": {
                "attempt": attempt, "project": "project-33333333-3333-4333-8333-333333333333",
                "workspace": str(workspace), "checkout": str(checkout),
                "intent": "ui.clean", "session": session,
                "caller_session": str(uuid.uuid4()),
            },
        }
        (run / "binding.json").write_text(json.dumps(binding))
        (run / "binding.json").chmod(0o600)
        self.assertEqual(api._owner_binding(run), binding)
        self.assertEqual(api._split_attempt_ref(attempt), (attempt, None))
        grant = "A" * 43
        self.assertEqual(api._split_attempt_ref(f"{attempt}.{grant}"), (attempt, grant))
        for change in (
            {"standalone": {**binding["standalone"], "attempt": str(uuid.uuid4())}},
            {"standalone": {**binding["standalone"], "session": None}},
            {"standalone": {**binding["standalone"], "project": "project-not-a-uuid"}},
            {"standalone": {**binding["standalone"], "checkout": str(workspace)}},
            {"standalone": {**binding["standalone"],
                            "caller_session": binding["standalone"]["session"]}},
            {"binding": {"task": "task", "session": session, "project": "project"}},
        ):
            candidate = {**binding, **change}
            (run / "binding.json").write_text(json.dumps(candidate))
            (run / "binding.json").chmod(0o600)
            with self.subTest(change=change), self.assertRaisesRegex(
                    api.Error, "lease_binding_invalid"):
                api._owner_binding(run)
        with self.assertRaisesRegex(api.Error, "invalid_managed_identity"):
            api._split_attempt_ref(f"{attempt}.{grant[:-1]}")

    def test_rejected_configuration_and_private_files(self):
        for value in (None, {}, {**self.config, "extra": 1},
                      {**self.config, "schema": True},
                      {**self.config, "pool": "relative"},
                      {**self.config, "profiles": {}}):
            with self.subTest(value=value), self.assertRaises(api.Error):
                api.LeaseConfig.from_mapping(value)
        for filename in ("binding.json", "status.json", "receipt.json", "helper.lock"):
            path = self.run / filename
            path.chmod(0o644)
            with self.subTest(filename=filename), self.assertRaisesRegex(
                    api.Error, "private_binding_required"):
                self.borrow().check()
            path.chmod(0o600)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
                api.Error, "owned_run_required"):
            self.borrow()
        for serial in ("", None, "emulator;command", "x" * 129):
            with self.assertRaisesRegex(api.Error, "device_serial_required"):
                api.borrow_device(self.config, serial)

    def test_rejects_expired_foreign_generation_endpoint_and_service(self):
        for key, value in (
            ("state", "expired"), ("owner", "foreign"), ("capability", "foreign"),
            ("generation", 2), ("id", "replacement"), ("slot", "other"),
            ("service_available", False), ("quarantined", True), ("expires_in", 0),
            ("endpoint", {"adb_serial": "emulator-5580"}),
        ):
            with self.subTest(key=key), patch.object(
                    api, "inspect_lease", return_value={**self.current, key: value}
            ), self.assertRaisesRegex(api.Error, "lease_lost_or_foreign"):
                self.borrow().check()
        fcntl.flock(self.owner, fcntl.LOCK_UN)
        with self.assertRaisesRegex(api.Error, "lease_owner_not_running"):
            self.borrow().check()

    def test_malformed_freshness_and_native_response_fail_closed(self):
        for value in (None, True, "fresh", float("nan"), float("inf")):
            self.write("status.json", {**self.state, "updated_at": value})
            with self.subTest(value=value), self.assertRaisesRegex(
                    api.Error, "lease_binding_invalid"):
                self.borrow().check()
        self.write("status.json", self.state)
        for key, value in (("expires_in", True), ("expires_in", "30"),
                           ("expires_in", float("nan")), ("expires_in", float("inf")),
                           ("endpoint", None)):
            with self.subTest(key=key, value=value), patch.object(
                    api, "inspect_lease", return_value={**self.current, key: value}
            ), self.assertRaisesRegex(api.Error, "lease_lost_or_foreign"):
                self.borrow().check()

    def test_rejects_stale_status_and_finish_signals(self):
        self.write("status.json", {**self.state, "updated_at": time.monotonic() - 31})
        with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
            self.borrow().check()
        self.write("status.json", self.state)
        for name in ("finish.json", "stop-renewal.json"):
            path = self.write(name, {})
            with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
                self.borrow().check()
            path.unlink()
        self.write("binding.json", {**self.binding, "capability": "foreign"})
        with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
            self.borrow().check()

    def test_borrow_cannot_follow_owner_or_lease_replacement(self):
        with self.borrow() as custody:
            replacement = {**self.current, "id": "replacement", "generation": 2}
            self.write("status.json", {**self.state, "lease": replacement})
            with patch.object(api, "inspect_lease", return_value=replacement):
                with self.assertRaisesRegex(api.Error, "lease_lost_or_foreign"):
                    custody.check()

    def test_marker_is_durable_exclusive_and_only_live_context_can_clear(self):
        from test_resource_lease_client import client

        with self.borrow() as custody:
            with patch.object(api.os, "fsync", wraps=os.fsync) as fsync:
                custody.begin_remote_execution()
                self.assertGreaterEqual(fsync.call_count, 2)
            marker = self.run / api.MARKER
            payload = json.loads(marker.read_bytes())
            self.assertEqual(payload["schema"], 1)
            self.assertEqual(payload["reason"], "remote_stop_unconfirmed")
            self.assertRegex(payload["operation"], r"^[0-9a-f]{32}$")
            self.assertFalse((self.run / "remote-stop.json").exists())
            self.assertEqual(client.read(self.run / "status.json")["command_stop"], "unconfirmed")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            self.assertEqual(custody.check(), self.current)
            with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
                self.borrow().check()
            with self.assertRaisesRegex(api.Error, "remote_execution_already_started"):
                custody.begin_remote_execution()
            custody.confirm_remote_stopped()
            self.assertFalse(marker.exists())
            self.assertEqual(client.read(self.run / "status.json")["command_stop"], "unconfirmed")
            self.assertEqual(self.borrow().check(), self.current)
            with self.assertRaisesRegex(api.Error, "remote_execution_not_started"):
                custody.confirm_remote_stopped()

    def managed_borrow(self):
        binding = {**self.binding, "schema": 2,
                   "binding": {"project": "project", "task": "task", "session": "session"},
                   "android_runtime": {"checkout": str(self.root), "settings": {}}}
        self.write("binding.json", binding)
        self.write("status.json", {**self.state, "parent_command_stop": "not_started",
                                   "command_stop": "not_started"})
        return api.DeviceCustody(self.config, "emulator-5576",
                                 managed=(self.run, binding, ("original", 1)))

    def standalone_borrow(self):
        from test_resource_lease_client import client

        attempt, session, caller = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        run = self.root / attempt
        run.mkdir(mode=0o700)
        owner = f"standalone:{attempt}"
        workspace, checkout = self.root / "primary", self.root / "linked"
        config = {**self.config, "runs_directory": str(self.root)}
        binding = {
            "schema": 3, "config": config, "profile": "ui.clean",
            "owner": owner, "capability": "clean",
            "custody_marker_protocol": 1, "binding": None,
            "standalone": {
                "attempt": attempt,
                "project": "project-33333333-3333-4333-8333-333333333333",
                "workspace": str(workspace), "checkout": str(checkout),
                "intent": "ui.clean", "session": session,
                "caller_session": caller,
            },
            "android_runtime": {"checkout": str(checkout), "settings": {}},
        }
        current = {
            **self.current, "owner": owner, "capability": "clean",
        }
        status = {
            "phase": "active", "updated_at": time.monotonic(),
            "custody_marker_protocol": 1, "parent_command_stop": "confirmed",
            "external_stop_unconfirmed": False, "command_stop": "confirmed",
            "run_directory": str(run), "lease": current, "error": None,
        }
        for name, value in (("binding.json", binding), ("status.json", status),
                            ("receipt.json", {}), ("helper.lock", {})):
            path = run / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        helper_fd = os.open(run / "helper.lock", os.O_RDONLY)
        fcntl.flock(helper_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, helper_fd)
        for name in ("controller-state.lock", "controller.lock"):
            path = run / name
            path.touch(mode=0o600)
            path.chmod(0o600)
        controller_fd = os.open(run / "controller.lock", os.O_RDONLY)
        fcntl.flock(controller_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, controller_fd)
        client.write(run / "controller.json", {
            "schema": 1, "phase": "claimed",
            "grant_sha256": api.hashlib.sha256(b"A" * 43).hexdigest(),
            "attempt": attempt, "project": binding["standalone"]["project"],
            "workspace": str(workspace), "checkout": str(checkout),
            "intent": "ui.clean", "session": session,
            "stop_requested": False, "admission": None,
        })
        self.inspect.return_value = current
        custody = api.DeviceCustody(
            config, "emulator-5576",
            managed=(run, binding, (current["id"], current["generation"])),
            standalone_grant="A" * 43,
        )
        return custody, run

    def test_controller_stop_between_inspect_and_marker_blocks_device_admission(self):
        from test_resource_lease_client import client

        custody, run = self.standalone_borrow()
        inspecting = threading.Event()
        finish_inspect = threading.Event()
        provider_calls = []
        failures = []

        def delayed_inspect(_command, **_options):
            inspecting.set()
            if not finish_inspect.wait(5):
                raise AssertionError("inspection fixture was not released")
            return self.inspect.return_value

        def dispatch():
            try:
                custody.begin_remote_execution()
                provider_calls.append("device-provider")
            except BaseException as error:
                failures.append(error)

        with patch.object(api, "inspect_lease", side_effect=delayed_inspect):
            worker = threading.Thread(target=dispatch)
            worker.start()
            self.assertTrue(inspecting.wait(5))
            client.stop_controller(run, timeout=0.2)
            finish_inspect.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(provider_calls, [])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], api.Error)
        self.assertFalse((run / api.MARKER).exists())
        controller = client.read(run / "controller.json")
        self.assertEqual(controller["phase"], "closed")
        self.assertTrue(controller["stop_requested"])

    def test_standalone_marker_admission_rechecks_exact_grant(self):
        custody, run = self.standalone_borrow()
        wrong = api.DeviceCustody(
            custody.config, custody.serial,
            managed=(run, custody._binding, custody._managed_identity),
            standalone_grant="B" * 43,
        )
        with self.assertRaisesRegex(api.Error, "controller_admission_unavailable"):
            wrong.begin_remote_execution()
        self.assertFalse((run / api.MARKER).exists())
        custody.begin_remote_execution()
        self.assertTrue((run / api.MARKER).exists())

    def test_managed_borrow_cannot_publish_marker_after_finish_fence(self):
        from test_resource_lease_client import client

        custody = self.managed_borrow()
        with self.assertRaisesRegex(api.Error, "managed_attempt_required"):
            self.borrow().check()
        self.assertEqual(custody.check(), self.current)
        client.request_managed_finish(self.run)
        with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
            custody.begin_remote_execution()
        self.assertFalse((self.run / api.MARKER).exists())

    def test_slow_managed_inspect_does_not_lock_helper_heartbeat(self):
        from test_resource_lease_client import client

        custody = self.managed_borrow()
        first_inspect = threading.Event()
        resume = threading.Event()
        original = client.read(self.run / "status.json")
        problems = []

        def inspect(_, **_options):
            if not first_inspect.is_set():
                first_inspect.set()
                if not resume.wait(5):
                    raise RuntimeError("fixture did not resume inspection")
            return self.current

        def borrower():
            try:
                custody.begin_remote_execution()
            except BaseException as error:
                problems.append(error)

        with patch.object(api, "inspect_lease", side_effect=inspect):
            thread = threading.Thread(target=borrower)
            thread.start()
            try:
                self.assertTrue(first_inspect.wait(5))
                owner = client.Lease(self.run, custody._binding)
                renewal = client.Renewal(owner, native=lambda _: None)
                renewal.value = self.current
                renewal.phase = "active"
                with patch.object(client, "CLI_TIMEOUT", 0.1), patch.object(
                    client.Lease, "cli", return_value=self.current,
                ) as allocator:
                    renewal.tick()
                    allocator.assert_called_once_with("renew")
                heartbeat = client.read(self.run / "status.json")
                self.assertNotEqual(heartbeat["updated_at"], original["updated_at"])
            finally:
                resume.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(problems, [])
        with self.assertRaisesRegex(client.Error, "stop is not confirmed"):
            client.request_managed_finish(self.run)
        custody.confirm_remote_stopped()
        client.request_managed_finish(self.run)

    def test_finish_during_unlocked_managed_inspection_prevents_marker(self):
        from test_resource_lease_client import client

        custody = self.managed_borrow()

        def inspect(_, **_options):
            with patch.object(client, "CLI_TIMEOUT", 0.1):
                client.request_managed_finish(self.run)
            return self.current

        with patch.object(api, "inspect_lease", side_effect=inspect):
            with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
                custody.begin_remote_execution()
        self.assertFalse((self.run / api.MARKER).exists())

    def test_delayed_managed_lease_observation_cannot_authorize_dispatch(self):
        custody = self.managed_borrow()
        instant = time.monotonic()

        def inspect(_, **_options):
            nonlocal instant
            instant += 2
            return {**self.current, "expires_in": 1}

        with patch.object(api.time, "monotonic", side_effect=lambda: instant), patch.object(
            api, "inspect_lease", side_effect=inspect,
        ):
            with self.assertRaisesRegex(api.Error, "lease_lost_or_foreign"):
                custody.begin_remote_execution()
        self.assertFalse((self.run / api.MARKER).exists())

    def test_exit_and_exception_are_not_remote_stop_proof(self):
        for failed in (False, True):
            marker = self.run / api.MARKER
            try:
                with self.borrow() as custody:
                    custody.begin_remote_execution()
                    if failed:
                        raise RuntimeError("fixture")
            except RuntimeError:
                pass
            self.assertTrue(marker.exists())
            with self.assertRaisesRegex(api.Error, "custody_closed"):
                custody.confirm_remote_stopped()
            with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
                self.borrow().check()
            marker.unlink()  # Fixture reset, never production cleanup.

    def test_host_process_exit_preserves_marker(self):
        code = (
            "import importlib.util,os,sys;"
            f"s=importlib.util.spec_from_file_location('custody',{str(SOURCE)!r});"
            "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);"
            f"m.inspect_lease=lambda _: {self.current!r};"
            f"c=m.borrow_device({self.config!r},'emulator-5576');"
            "c.__enter__();c.begin_remote_execution();os._exit(0)"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.run / api.MARKER).exists())
        with self.assertRaises(api.Error):
            self.borrow().check()

    def test_status_persistence_failure_leaves_marker_before_dispatch(self):
        with self.borrow() as custody, patch.object(
                api, "_write_owner_status", side_effect=OSError("disk")):
            with self.assertRaisesRegex(OSError, "disk"):
                custody.begin_remote_execution()
        self.assertTrue((self.run / api.MARKER).exists())
        self.assertFalse((self.run / "remote-stop.json").exists())

    def test_changed_replaced_missing_and_symlink_markers_are_never_removed(self):
        marker = self.run / api.MARKER
        for mutation in ("changed", "replaced", "missing", "symlink", "permissions"):
            with self.subTest(mutation=mutation):
                with self.borrow() as custody:
                    custody.begin_remote_execution()
                    original = marker.read_bytes()
                    if mutation == "changed":
                        marker.write_bytes(original + b" ")
                    elif mutation == "replaced":
                        marker.unlink()
                        marker.write_bytes(original)
                        marker.chmod(0o600)
                    elif mutation == "missing":
                        marker.unlink()
                    elif mutation == "symlink":
                        marker.unlink()
                        marker.symlink_to(self.run / "receipt.json")
                    else:
                        marker.chmod(0o644)
                    for operation in (custody.check, custody.confirm_remote_stopped,
                                      lambda: custody.handoff_uncertain("host_interrupted")):
                        with self.assertRaises((api.Error, OSError)):
                            operation()
                    self.assertEqual(marker.exists() or marker.is_symlink(), mutation != "missing")
                if marker.exists() or marker.is_symlink():
                    marker.unlink()

    def test_stop_signal_arriving_during_pool_inspection_blocks_return(self):
        def inspect(_):
            self.write("finish.json", {"schema": 1})
            return self.current

        with patch.object(api, "inspect_lease", side_effect=inspect):
            with self.assertRaisesRegex(api.Error, "parent_command_stop_unconfirmed"):
                self.borrow().check()
        self.assertTrue((self.run / "finish.json").exists())

    def test_two_consumers_race_exclusively_and_loser_cannot_clean_winner(self):
        ready = threading.Barrier(2)
        outcomes = []

        def consume():
            try:
                with self.borrow() as custody:
                    ready.wait(timeout=5)
                    custody.begin_remote_execution()
                    custody.handoff_uncertain("host_interrupted")
                    outcomes.append("created")
            except api.Error:
                outcomes.append("blocked")

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["created", "blocked"])
        self.assertTrue((self.run / api.MARKER).exists())

    def test_foreign_marker_racing_exclusive_create_is_not_overwritten_or_removed(self):
        marker = self.run / api.MARKER
        real_open = os.open

        def raced_open(path, flags, *args, **kwargs):
            if Path(path) == marker and flags & os.O_EXCL:
                self.write(api.MARKER, {"schema": 1, "reason": "foreign_fixture"})
            return real_open(path, flags, *args, **kwargs)

        with self.borrow() as custody:
            with patch.object(api.os, "open", side_effect=raced_open):
                with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
                    custody.begin_remote_execution()
            self.assertEqual(json.loads(marker.read_bytes())["reason"], "foreign_fixture")
            with self.assertRaisesRegex(api.Error, "remote_execution_not_started"):
                custody.confirm_remote_stopped()
            self.assertTrue(marker.exists())

    def test_marker_replaced_during_inspection_is_not_cleaned(self):
        with self.borrow() as custody:
            custody.begin_remote_execution()

            def inspect(_):
                path = self.run / api.MARKER
                path.unlink()
                self.write(api.MARKER, {"schema": 1, "reason": "foreign_fixture"})
                return self.current

            with patch.object(api, "inspect_lease", side_effect=inspect):
                with self.assertRaisesRegex(api.Error, "uncertainty_marker_changed"):
                    custody.confirm_remote_stopped()
            self.assertEqual(json.loads((self.run / api.MARKER).read_bytes())["reason"],
                             "foreign_fixture")

    def test_native_owner_replacement_after_last_snapshot_is_never_deleted(self):
        from test_resource_lease_client import client
        blocked = threading.Event()
        published = threading.Event()
        failures = []
        real_flock = fcntl.flock

        def observe_contention(fd, flags):
            try:
                return real_flock(fd, flags)
            except BlockingIOError:
                if threading.current_thread().name == "native-marker-owner":
                    blocked.set()
                raise

        def owner_publish():
            try:
                client.write(self.run / api.MARKER, {
                    "schema": 1, "reason": "native_runner_drain_unconfirmed",
                    "operation": "00000000000040008000000000000002",
                })
                published.set()
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=owner_publish, name="native-marker-owner")
        with self.borrow() as custody:
            custody.begin_remote_execution()
            snapshot = api._marker_snapshot
            samples = 0

            def snapshot_then_owner_replace(path):
                nonlocal samples
                value = snapshot(path)
                samples += 1
                if samples == 2:
                    thread.start()
                    self.assertTrue(blocked.wait(5), "real owner did not contend on directory lock")
                    self.assertFalse(published.is_set())
                return value

            try:
                with patch.object(api, "_marker_snapshot", side_effect=snapshot_then_owner_replace), patch.object(
                        client.fcntl, "flock", side_effect=observe_contention):
                    custody.confirm_remote_stopped()
            finally:
                if thread.ident is not None:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertGreaterEqual(samples, 2)
            marker = self.run / api.MARKER
            self.assertTrue(marker.is_file(), "cleanup deleted the native owner's replacement")
            self.assertEqual(json.loads(marker.read_bytes())["reason"],
                             "native_runner_drain_unconfirmed")

    def test_native_owner_publication_before_confirmation_is_retained(self):
        from test_resource_lease_client import client
        with self.borrow() as custody:
            custody.begin_remote_execution()
            client.write(self.run / api.MARKER, {
                "schema": 1, "reason": "signal_error",
                "operation": "00000000000040008000000000000002",
            })
            with self.assertRaisesRegex(api.Error, "uncertainty_marker_changed"):
                custody.confirm_remote_stopped()
            self.assertEqual(json.loads((self.run / api.MARKER).read_bytes())["reason"],
                             "signal_error")

    def test_legacy_records_are_not_upgraded_or_used_by_new_custody(self):
        for binding_version, status_version in (
                (None, None), (None, 1), (1, None), (0, 1), (1, 2), (True, 1), (1, True)):
            binding = dict(self.binding)
            state = dict(self.state)
            if binding_version is None:
                binding.pop("custody_marker_protocol")
            else:
                binding["custody_marker_protocol"] = binding_version
            if status_version is None:
                state.pop("custody_marker_protocol")
            else:
                state["custody_marker_protocol"] = status_version
            self.write("binding.json", binding)
            self.write("status.json", state)
            with self.subTest(binding=binding_version, status=status_version):
                with self.assertRaises(api.Error):
                    self.borrow().check()
                self.assertFalse((self.run / api.MARKER).exists())

    def test_active_marker_refuses_lost_runtime_protocol_without_cleanup(self):
        with self.borrow() as custody:
            custody.begin_remote_execution()
            state = dict(self.state)
            state.pop("custody_marker_protocol")
            self.write("status.json", state)
            with self.assertRaises(api.Error):
                custody.confirm_remote_stopped()
            self.assertTrue((self.run / api.MARKER).exists())

    def test_borrower_directory_lock_timeout_is_categorical_and_bounded(self):
        fd = os.open(self.run, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(api, "MARKER_LOCK_TIMEOUT_SECONDS", 0.02):
                with self.assertRaisesRegex(api.Error, "marker_lock_timeout"):
                    self.borrow().check()
            self.assertFalse((self.run / api.MARKER).exists())
        finally:
            os.close(fd)

    def test_lease_loss_during_remote_execution_retains_marker(self):
        with self.borrow() as custody:
            custody.begin_remote_execution()
            with patch.object(api, "inspect_lease", return_value={
                    **self.current, "state": "expired",
            }), self.assertRaisesRegex(api.Error, "lease_lost_or_foreign"):
                custody.confirm_remote_stopped()
            self.assertTrue((self.run / api.MARKER).exists())

    def test_marker_create_and_readback_failures_prevent_dispatch(self):
        with self.borrow() as custody:
            with patch.object(api, "_marker_snapshot", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    custody.begin_remote_execution()
            self.assertTrue((self.run / api.MARKER).exists())
            with self.assertRaises(api.Error):
                custody.check()

    def test_marker_creation_fsync_failure_retains_uncertainty(self):
        with self.borrow() as custody:
            real_fsync = os.fsync
            calls = 0

            def fail_marker_sync(fd):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("fixture_fsync")
                return real_fsync(fd)

            with patch.object(api.os, "fsync", side_effect=fail_marker_sync):
                with self.assertRaisesRegex(OSError, "fixture_fsync"):
                    custody.begin_remote_execution()
            self.assertTrue((self.run / api.MARKER).exists())
            self.assertFalse((self.run / "remote-stop.json").exists())
            with self.assertRaises(api.Error):
                self.borrow().check()

    def test_categorical_handoff_preserves_existing_marker_and_rejects_secret_reason(self):
        with self.assertRaisesRegex(api.Error, "invalid_uncertainty_reason"):
            api.handoff_uncertain("secret=must-not-be-persisted")
        self.assertFalse((self.run / api.MARKER).exists())
        with self.borrow() as custody:
            custody.begin_remote_execution()
            original = (self.run / api.MARKER).read_bytes()
            custody.handoff_uncertain("host_interrupted")
            api.handoff_uncertain("native_runner_drain_unconfirmed")
            self.assertEqual((self.run / api.MARKER).read_bytes(), original)
            custody.confirm_remote_stopped()
        api.handoff_uncertain("native_runner_drain_unconfirmed")
        self.assertFalse((self.run / api.MARKER).exists())
        self.assertTrue(json.loads((self.run / "status.json").read_bytes())[
            "external_stop_unconfirmed"])

    def test_late_uncertainty_handoff_persists_after_lease_ended(self):
        from test_resource_lease_client import client

        heartbeat = time.monotonic()
        self.write("status.json", {
            **self.state,
            "updated_at": heartbeat,
            "phase": "active",
            "parent_command_stop": "confirmed",
            "external_stop_unconfirmed": False,
            "command_stop": "confirmed",
            "lease": self.current,
        })
        # The owner helper has stopped, while its still-active lease receipt
        # remains available for ordinary finish/release.
        fcntl.flock(self.owner, fcntl.LOCK_UN)

        handoff_error = None
        try:
            api.handoff_uncertain("host_interrupted")
        except api.Error as error:
            handoff_error = str(error)
        self.inspect.assert_not_called()
        handed_off = client.read(self.run / "status.json")
        self.assertEqual(handed_off["updated_at"], heartbeat)
        self.assertTrue(handed_off["external_stop_unconfirmed"])

        terminal = {
            **self.current,
            "state": "completed",
            "endpoint": None,
            "expires_in": 0,
            "cleanup_pending": False,
        }
        with patch.object(client.Lease, "cli", return_value=terminal):
            final = client.finish(self.run)

        status = client.read(self.run / "status.json")
        self.assertIsNone(
            handoff_error,
            f"late handoff was lost after owner stop: {handoff_error}; "
            f"final={final!r}, status={status!r}",
        )
        self.assertEqual(
            final["phase"], "failed",
            f"UNKNOWN must not be reported as successful finish: {status!r}",
        )
        self.assertTrue(status["external_stop_unconfirmed"])
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["command_stop"], "unconfirmed")
        self.assertEqual(status["lease"]["state"], "completed")
        self.assertFalse(status["lease"]["cleanup_pending"])
        self.assertIn("resource cleanup confirmed", status["error"])

    def test_late_handoff_invalidates_persisted_finish_without_releasing(self):
        heartbeat = time.monotonic()
        terminal = {
            **self.current, "state": "completed", "endpoint": None,
            "expires_in": 0, "cleanup_pending": False,
        }
        self.write("status.json", {
            **self.state, "phase": "finished", "updated_at": heartbeat,
            "parent_command_stop": "confirmed", "external_stop_unconfirmed": False,
            "command_stop": "confirmed", "lease": terminal,
        })
        fcntl.flock(self.owner, fcntl.LOCK_UN)

        api.handoff_uncertain("host_interrupted")

        status = json.loads((self.run / "status.json").read_bytes())
        self.assertEqual(status["phase"], "failed")
        self.assertTrue(status["external_stop_unconfirmed"])
        self.assertEqual(status["command_stop"], "unconfirmed")
        self.assertEqual(status["updated_at"], heartbeat)
        self.assertEqual(status["lease"], terminal)
        self.inspect.assert_not_called()

    def test_foreign_uncertainty_survives_current_operation_confirmation(self):
        from test_resource_lease_client import client

        self.write("status.json", {
            **self.state,
            "parent_command_stop": "confirmed",
            "external_stop_unconfirmed": False,
            "command_stop": "confirmed",
        })
        with self.borrow() as custody:
            custody.begin_remote_execution()
            # This represents a distinct caller's negative result while this
            # custody already owns a pending marker.
            api.handoff_uncertain("host_interrupted")
            custody.confirm_remote_stopped()

        owner = client.Lease(self.run, client.read(self.run / "binding.json"))
        fcntl.flock(self.owner, fcntl.LOCK_UN)
        terminal = {
            **self.current,
            "state": "completed",
            "endpoint": None,
            "expires_in": 0,
            "cleanup_pending": False,
        }
        with patch.object(client.Lease, "cli", return_value=terminal):
            final = client.finish(self.run)

        status = client.read(self.run / "status.json")
        self.assertEqual(final["phase"], "failed")
        self.assertTrue(status["external_stop_unconfirmed"])
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["command_stop"], "unconfirmed")
        self.assertEqual(status["lease"]["state"], "completed")
        self.assertFalse(status["lease"]["cleanup_pending"])

    def test_heartbeat_aggregate_settles_after_owned_marker_confirmation(self):
        from test_resource_lease_client import client

        owner = client.Lease(self.run, client.read(self.run / "binding.json"))
        self.write("status.json", {
            **self.state,
            "parent_command_stop": "pending",
            "external_stop_unconfirmed": False,
            "command_stop": "unconfirmed",
        })
        with self.borrow() as custody:
            custody.begin_remote_execution()
            heartbeat = owner.record(
                "active", lease=self.current, parent_command_stop="pending",
            )
            self.assertEqual(heartbeat["command_stop"], "unconfirmed")
            self.assertIsNone(heartbeat["error"])
            owner.record("active", lease=self.current, parent_command_stop="confirmed")
            custody.confirm_remote_stopped()

        settled = owner.record("active", parent_command_stop="confirmed")
        self.assertEqual(settled["command_stop"], "confirmed")
        self.assertIsNone(settled["error"])

        fcntl.flock(self.owner, fcntl.LOCK_UN)
        terminal = {
            **self.current,
            "state": "completed",
            "endpoint": None,
            "expires_in": 0,
            "cleanup_pending": False,
        }
        with patch.object(client.Lease, "cli", return_value=terminal):
            final = client.finish(self.run)
        self.assertEqual(final["phase"], "finished")
        self.assertEqual(final["command_stop"], "confirmed")
        self.assertIsNone(final["error"])

    def test_native_helper_can_finish_immediately_after_owned_confirmation(self):
        self.finish_after_owned_confirmation("not_started")

    def test_confirmed_parent_can_finish_immediately_after_owned_confirmation(self):
        self.finish_after_owned_confirmation("confirmed")

    def finish_after_owned_confirmation(self, parent):
        from test_resource_lease_client import client

        self.write("status.json", {
            **self.state, "parent_command_stop": parent, "command_stop": parent,
        })
        owner = client.Lease(self.run, self.binding)
        with self.borrow() as custody:
            custody.begin_remote_execution()
            heartbeat = owner.record("active", lease=self.current)
            self.assertEqual(heartbeat["command_stop"], "unconfirmed")
            custody.confirm_remote_stopped()

        self.assertFalse((self.run / api.MARKER).exists())
        fcntl.flock(self.owner, fcntl.LOCK_UN)
        terminal = {
            **self.current, "state": "completed", "endpoint": None,
            "expires_in": 0, "cleanup_pending": False,
        }
        with patch.object(client.Lease, "cli", return_value=terminal) as release:
            final = client.finish(self.run)
        self.assertEqual(release.call_count, 1)
        self.assertEqual(release.call_args.args, ("release",))
        self.assertEqual(final["phase"], "finished")
        self.assertEqual(final["command_stop"], parent)
        self.assertFalse(final["external_stop_unconfirmed"])
        self.assertIsNone(final["error"], final)


if __name__ == "__main__":
    unittest.main()
