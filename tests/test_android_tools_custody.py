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
        self.root = Path(self.temporary.name)
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
            "custody_marker_protocol": 1,
        }
        self.state = {"phase": "active", "updated_at": time.monotonic(),
                      "lease": self.current, "command_stop": "unconfirmed",
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

    def test_remote_marker_is_durable_exclusive_and_only_owner_can_clear(self):
        with self.borrow() as custody:
            with patch.object(api.os, "fsync", wraps=os.fsync) as fsync:
                custody.begin_remote_execution()
                self.assertGreaterEqual(fsync.call_count, 2)
            marker = self.run / api.MARKER
            payload = json.loads(marker.read_bytes())
            self.assertEqual(payload["schema"], 1)
            self.assertEqual(payload["reason"], "remote_stop_unconfirmed")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            self.assertEqual(custody.check(), self.current)
            with self.assertRaisesRegex(api.Error, "lease_binding_invalid"):
                self.borrow().check()
            with self.assertRaisesRegex(api.Error, "remote_execution_already_started"):
                custody.begin_remote_execution()
            custody.confirm_remote_stopped()
            self.assertFalse(marker.exists())
            self.assertEqual(self.borrow().check(), self.current)
            with self.assertRaisesRegex(api.Error, "remote_execution_not_started"):
                custody.confirm_remote_stopped()

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
                    "schema": 1, "reason": "native_owner_stop_uncertain",
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
                if samples == 3:  # After both check() samples and the final cleanup sample.
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
            self.assertEqual(samples, 3)
            marker = self.run / api.MARKER
            self.assertTrue(marker.is_file(), "cleanup deleted the native owner's replacement")
            self.assertEqual(json.loads(marker.read_bytes())["reason"], "native_owner_stop_uncertain")

    def test_native_owner_publication_before_confirmation_is_retained(self):
        from test_resource_lease_client import client
        with self.borrow() as custody:
            custody.begin_remote_execution()
            client.write(self.run / api.MARKER, {"schema": 1, "reason": "signal_error"})
            with self.assertRaisesRegex(api.Error, "uncertainty_marker_changed"):
                custody.confirm_remote_stopped()
            self.assertEqual(json.loads((self.run / api.MARKER).read_bytes())["reason"],
                             "signal_error")

    def test_legacy_checks_remain_valid_but_remote_begin_requires_protocol_pair(self):
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
            with self.subTest(binding=binding_version, status=status_version), self.borrow() as custody:
                self.assertEqual(custody.check(), self.current)
                with self.assertRaisesRegex(api.Error, "owner_marker_protocol_required"):
                    custody.begin_remote_execution()
                self.assertFalse((self.run / api.MARKER).exists())

    def test_active_marker_refuses_lost_runtime_protocol_without_cleanup(self):
        with self.borrow() as custody:
            custody.begin_remote_execution()
            state = dict(self.state)
            state.pop("custody_marker_protocol")
            self.write("status.json", state)
            with self.assertRaisesRegex(api.Error, "owner_marker_protocol_required"):
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
            with patch.object(api.os, "fsync", side_effect=OSError("fixture_fsync")):
                with self.assertRaisesRegex(OSError, "fixture_fsync"):
                    custody.begin_remote_execution()
            self.assertTrue((self.run / api.MARKER).exists())
            with self.assertRaises(api.Error):
                custody.check()

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
        self.assertTrue((self.run / api.MARKER).exists())


if __name__ == "__main__":
    unittest.main()
