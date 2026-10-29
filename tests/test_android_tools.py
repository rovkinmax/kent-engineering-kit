"""Facade admission and verified-byte bundle tests."""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
import hashlib
import json
import os
import time
import subprocess
import sys
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
from dataclasses import make_dataclass
from contextlib import redirect_stdout
import io


SOURCE = Path(__file__).resolve().parents[1] / "templates/project/android-tools"
loader = importlib.machinery.SourceFileLoader("android_tools_facade", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
facade = importlib.util.module_from_spec(spec)
loader.exec_module(facade)


class BundleTests(unittest.TestCase):
    def test_unqualified_bundle_rejected(self):
        with patch.object(facade, "BUNDLE_HASHES", {"absent.py": "0" * 64}):
            with self.assertRaises(facade.Error):
                facade.load_bundle()

    def test_bundle_executes_verified_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"BUNDLE_PROTOCOL = 1\nVALUE = 42\n"
            (root / "sample.py").write_bytes(content)
            with patch.object(facade, "__file__", str(root / "android-tools")), patch.object(
                facade, "BUNDLE_HASHES", {"sample.py": hashlib.sha256(content).hexdigest()}
            ):
                self.assertEqual(facade.load_bundle()["sample"].VALUE, 42)
                (root / "sample.py").write_bytes(b"raise AssertionError('must not execute')\n")
                with self.assertRaises(facade.Error):
                    facade.load_bundle()

    def test_source_bundle_hashes_match_and_mixed_native_bundle_is_rejected(self):
        for filename in facade.BUNDLE_HASHES:
            source = SOURCE.parent / filename
            self.assertEqual(
                facade.BUNDLE_HASHES[filename],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        facade.load_bundle()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in facade.BUNDLE_HASHES:
                (root / filename).write_bytes((SOURCE.parent / filename).read_bytes())
            with patch.object(facade, "__file__", str(root / "android-tools")):
                facade.load_bundle()
                native_copy = root / "android_tools_native.py"
                native_copy.write_bytes(native_copy.read_bytes() + b"\n")
                with self.assertRaisesRegex(facade.Error, "bundle_mismatch"):
                    facade.load_bundle()

    def test_private_binding_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(facade.Error):
                facade.read_private(path)
            path.chmod(0o600)
            self.assertEqual(facade.read_private(path), {})

    def test_missing_run_is_not_ownership(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(facade.Error):
                facade.custody({}, "emulator-5576")

    def test_retention_defaults_and_explicit_bounds(self):
        config = {
            "schema": 1, "source_root": "/source", "records_directory": "/records",
            "timeout_seconds": 120, "output_limit_bytes": 1048576,
            "providers": {name: {"executable": "/tool", "version": "1"}
                          for name in ("mobile-native", "google-android")},
            "lease": {"schema": 1, "executable": "/lease", "pool": "/pool",
                      "runs_directory": "/runs", "profiles": {"ui": "clean"}},
        }
        with patch.object(facade, "read_private", return_value=dict(config)):
            self.assertEqual(facade.configuration(Path("/config"))["retention"],
                             {"max_bytes": 500000000, "max_age_seconds": 604800})
        for limits in (
            {"max_bytes": True, "max_age_seconds": 604800},
            {"max_bytes": 0, "max_age_seconds": 604800},
            {"max_bytes": 500000000, "max_age_seconds": 0},
            {"max_bytes": 500000000},
            {"max_bytes": 500000000, "max_age_seconds": 604800, "extra": 1},
        ):
            with self.subTest(limits=limits), patch.object(
                facade, "read_private", return_value={**config, "retention": limits}
            ), self.assertRaisesRegex(facade.Error, "invalid_retention"):
                facade.configuration(Path("/config"))
        limits = {"max_bytes": 10000000, "max_age_seconds": 3600}
        with patch.object(facade, "read_private",
                          return_value={**config, "retention": limits}):
            self.assertEqual(facade.configuration(Path("/config"))["retention"], limits)

    def test_admission_rejection_precedes_dispatch_and_run_is_released(self):
        config = {
            "records_directory": "/records", "source_root": "/source",
            "output_limit_bytes": 1048576, "timeout_seconds": 120,
            "retention": {"max_bytes": 500000000, "max_age_seconds": 604800},
            "providers": {"google-android": {"executable": "/tool", "version": "1"}},
        }
        handle = SimpleNamespace(staging_directory=Path("/stage"))
        for failure in ("admission", "native", "finish", "interrupt"):
            evidence = Mock()
            native = Mock()
            evidence.begin_run.return_value = handle
            evidence.reservation_for_operation.return_value = 2000000
            native.execute_android_native.return_value = SimpleNamespace(error=None)
            if failure == "admission":
                evidence.begin_run.side_effect = ValueError("storage_capacity")
            elif failure == "finish":
                evidence.finish_run.side_effect = ValueError("write_failed")
            else:
                native.execute_android_native.side_effect = (
                    KeyboardInterrupt() if failure == "interrupt" else ValueError("native_failed")
                )
            with self.subTest(failure=failure), patch.object(
                facade, "configuration", return_value=config
            ), patch.object(facade, "load_bundle", return_value={
                "android_tools_native": native, "android_tools_evidence": evidence,
            }), patch.object(sys, "argv", [
                "android-tools", "--config", "/config", "docs", "search",
                "--arguments", '{"query":"Compose"}',
            ]), self.assertRaises((ValueError, KeyboardInterrupt)):
                facade.main()
            if failure == "admission":
                native.execute_android_native.assert_not_called()
                evidence.release_run.assert_not_called()
            else:
                evidence.release_run.assert_called_once_with(handle)
            self.assertEqual(evidence.begin_run.call_args.kwargs, {
                "max_bytes": 500000000, "max_age_seconds": 604800,
                "reservation_bytes": 2000000,
            })

    def test_dpad_keys_use_custody_and_evidence_path(self):
        result_type = make_dataclass("DpadResult", [
            ("transport", str), ("effect", str), ("error", str),
            ("data", dict), ("artifacts", tuple),
        ])
        config = {
            "records_directory": "/records", "source_root": "/source",
            "output_limit_bytes": 1048576, "timeout_seconds": 120,
            "retention": {"max_bytes": 500000000, "max_age_seconds": 604800},
            "providers": {"mobile-native": {"executable": "/native", "version": "1"}},
        }
        aliases = ("up", "down", "left", "right", "center")
        for key in aliases:
            with self.subTest(key=key):
                evidence, native = Mock(), Mock()
                handle = SimpleNamespace(staging_directory=Path("/stage"))
                manifest = Path("/records/run/manifest.json")
                evidence.begin_run.return_value = handle
                evidence.reservation_for_operation.return_value = 4096
                evidence.finish_run.return_value = manifest
                result = result_type("completed", "unknown", None, {"dispatched": True}, ())
                native.execute_android_native.return_value = result
                lease = {"id": "lease-123", "generation": 7}
                serial = "emulator-5554"
                arguments = {"device_serial": serial, "key": key}
                output = io.StringIO()

                with patch.object(facade, "configuration", return_value=config), patch.object(
                    facade,
                    "load_bundle",
                    return_value={
                        "android_tools_native": native,
                        "android_tools_evidence": evidence,
                    },
                ), patch.object(facade, "custody", side_effect=[lease, lease]) as custody, patch.object(
                    sys,
                    "argv",
                    [
                        "android-tools", "--config", "/config", "device", "key",
                        "--arguments", json.dumps(arguments),
                    ],
                ), redirect_stdout(output):
                    self.assertEqual(facade.main(), 0)

                payload = json.loads(output.getvalue())
                self.assertEqual(payload["effect"], "unknown")
                self.assertEqual(payload["data"], {"dispatched": True})
                self.assertEqual(payload["local_manifest"], str(manifest))
                self.assertEqual(custody.call_count, 2)
                for call_args in custody.call_args_list:
                    self.assertEqual(call_args.args, (config, serial))
                self.assertEqual(
                    evidence.begin_run.call_args.args,
                    (
                        Path("/records"),
                        "device.key",
                        "mobile-native",
                        {
                            "lease_id": lease["id"],
                            "generation": lease["generation"],
                            "device_serial": serial,
                        },
                    ),
                )
                self.assertEqual(evidence.begin_run.call_args.kwargs, {
                    "max_bytes": 500000000,
                    "max_age_seconds": 604800,
                    "reservation_bytes": 4096,
                })
                native.execute_android_native.assert_called_once()
                self.assertEqual(
                    native.execute_android_native.call_args.args,
                    ("mobile-native", "device.key", arguments),
                )
                self.assertEqual(
                    native.execute_android_native.call_args.kwargs,
                    {
                        "executable": Path("/native"),
                        "expected_version": "1",
                        "timeout_s": 120,
                        "output_limit_bytes": 1048576,
                        "source_root": Path("/source"),
                        "staging_dir": handle.staging_directory,
                    },
                )
                evidence.finish_run.assert_called_once_with(handle, result)
                evidence.release_run.assert_called_once_with(handle)

    def test_dpad_keys_are_blocked_before_evidence_without_valid_lease(self):
        config = {
            "records_directory": "/records", "source_root": "/source",
            "output_limit_bytes": 1048576, "timeout_seconds": 120,
            "retention": {"max_bytes": 500000000, "max_age_seconds": 604800},
            "providers": {"mobile-native": {"executable": "/native", "version": "1"}},
        }
        failures = {
            "missing": facade.Error("owned_run_required"),
            "foreign": facade.Error("lease_lost_or_foreign"),
            "expired": facade.Error("lease_lost_or_foreign"),
        }
        for key in ("up", "down", "left", "right", "center"):
            for failure_name, failure in failures.items():
                with self.subTest(key=key, failure=failure_name):
                    evidence, native = Mock(), Mock()
                    with patch.object(
                        facade, "configuration", return_value=config
                    ), patch.object(
                        facade,
                        "load_bundle",
                        return_value={
                            "android_tools_native": native,
                            "android_tools_evidence": evidence,
                        },
                    ), patch.object(
                        facade, "custody", side_effect=failure
                    ), patch.object(
                        sys,
                        "argv",
                        [
                            "android-tools", "--config", "/config", "device", "key",
                            "--arguments",
                            json.dumps({
                                "device_serial": "emulator-5554",
                                "key": key,
                            }),
                        ],
                    ), self.assertRaises(facade.Error):
                        facade.main()

                    evidence.begin_run.assert_not_called()
                    native.execute_android_native.assert_not_called()

    def test_uncertain_drain_preserves_handoff_outcome_and_releases_record(self):
        result_type = make_dataclass("Result", [
            ("transport", str), ("effect", str), ("error", str),
            ("data", dict), ("artifacts", tuple),
        ])
        result = result_type("unknown", "unknown", "drain_unknown", {}, ())
        config = {
            "records_directory": "/records", "source_root": "/source",
            "output_limit_bytes": 1048576, "timeout_seconds": 120,
            "retention": {"max_bytes": 500000000, "max_age_seconds": 604800},
            "providers": {"mobile-native": {"executable": "/tool", "version": "1"}},
        }
        for marker_failure in (False, True):
            evidence, native = Mock(), Mock()
            native.execute_android_native.return_value = result
            evidence.finish_run.return_value = Path("/records/id/manifest.json")
            output = io.StringIO()
            with self.subTest(marker_failure=marker_failure), patch.object(
                facade, "configuration", return_value=config
            ), patch.object(facade, "load_bundle", return_value={
                "android_tools_native": native, "android_tools_evidence": evidence,
            }), patch.object(facade, "custody", side_effect=[
                {"id": "lease", "generation": 1}, facade.Error("lease_lost"),
            ]), patch.object(facade, "handoff_uncertain",
                             side_effect=OSError() if marker_failure else None), patch.object(
                sys, "argv", ["android-tools", "--config", "/config", "device", "tap",
                              "--arguments", '{"device_serial":"emulator-5576"}']
            ), redirect_stdout(output):
                self.assertEqual(facade.main(), 1)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["error"], "drain_unknown")
            self.assertEqual(payload["lease_handoff"],
                             "handoff_unknown" if marker_failure else "uncertainty_recorded")
            self.assertIs(evidence.finish_run.call_args.args[1], result)
            evidence.release_run.assert_called_once_with(evidence.begin_run.return_value)

    def test_custody_rejects_expired_foreign_generation_and_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir(mode=0o700)
            lease_config = {
                "schema": 1, "executable": "/qualified/resource-lease", "pool": "/pool",
                "runs_directory": str(root), "profiles": {"ui.clean": "clean"},
            }
            current = {
                "id": "original", "owner": "owner", "capability": "clean",
                "generation": 1, "slot": "slot", "state": "active",
                "service_available": True, "quarantined": False, "expires_in": 30,
                "endpoint": {"adb_serial": "emulator-5576"},
            }
            values = {
                "binding.json": {
                    "schema": 1, "config": lease_config, "profile": "ui.clean",
                    "owner": "owner", "capability": "clean",
                },
                "status.json": {"phase": "active", "updated_at": time.monotonic(),
                                "lease": current, "command_stop": "unconfirmed"},
                "receipt.json": {},
                "helper.lock": {},
            }
            for filename, value in values.items():
                path = run / filename
                path.write_text(json.dumps(value))
                path.chmod(0o600)
            with patch.dict(os.environ, {"RESOURCE_LEASE_RUN_DIRECTORY": str(run)}), patch.object(
                facade.fcntl, "flock", side_effect=BlockingIOError
            ), patch.object(facade, "inspect_lease", return_value=current):
                self.assertEqual(facade.custody({"lease": lease_config}, "emulator-5576"), current)
                for key, value in (
                    ("state", "expired"), ("owner", "foreign"), ("generation", 2),
                    ("id", "replacement"), ("service_available", False),
                    ("endpoint", {"adb_serial": "emulator-5580"}),
                ):
                    with self.subTest(key=key), patch.object(
                        facade, "inspect_lease", return_value={**current, key: value}
                    ), self.assertRaises(facade.Error):
                        facade.custody({"lease": lease_config}, "emulator-5576")
            with patch.dict(os.environ, {"RESOURCE_LEASE_RUN_DIRECTORY": str(run)}):
                with self.assertRaisesRegex(facade.Error, "lease_owner_not_running"):
                    facade.custody({"lease": lease_config}, "emulator-5576")

    def test_uncertainty_marker_survives_owner_resource_teardown(self):
        from test_resource_lease_client import FAKE, SCRIPT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool = root / "pool"
            pool.mkdir()
            executable = root / "resource-lease"
            executable.write_text(FAKE)
            executable.chmod(0o700)
            runs = root / "runs"
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema": 1, "executable": str(executable), "pool": str(pool),
                "runs_directory": str(runs), "profiles": {"ui.clean": "clean"},
            }))
            config.chmod(0o600)
            code = (
                "import runpy; "
                f"scope=runpy.run_path({str(SOURCE)!r},run_name='facade_fixture'); "
                "scope['handoff_uncertain'](); raise SystemExit(1)"
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "run", "--config", str(config),
                 "--profile", "ui.clean", "--owner", "fixture", "--",
                 sys.executable, "-c", code],
                capture_output=True, timeout=15,
            )
            self.assertNotEqual(result.returncode, 0)
            statuses = list(runs.glob("*/status.json"))
            self.assertEqual(len(statuses), 1)
            status = json.loads(statuses[0].read_text())
            self.assertEqual(status["phase"], "failed")
            self.assertEqual(status["command_stop"], "unconfirmed")
            self.assertEqual(status["lease"]["state"], "completed")
            self.assertFalse(status["lease"]["cleanup_pending"])
            self.assertIn("command stop is uncertain", status["error"])
            self.assertEqual((pool / "calls").read_text().splitlines().count("acquire"), 1)
            self.assertEqual((pool / "calls").read_text().splitlines().count("release"), 1)
            marker = statuses[0].parent / "command-stop-uncertain.json"
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            retried = subprocess.run(
                [sys.executable, str(SCRIPT), "finish", "--run-dir", str(statuses[0].parent)],
                capture_output=True, timeout=15,
            )
            self.assertNotEqual(retried.returncode, 0)


if __name__ == "__main__":
    unittest.main()
