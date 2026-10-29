from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import struct
import tempfile
import threading
import time
import unittest
import zlib
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = runpy.run_path(str(ROOT / "templates/project/android_tools_evidence.py"))
NATIVE = runpy.run_path(str(ROOT / "templates/project/android_tools_native.py"))


def png_1x1() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        content = kind + payload
        return struct.pack(">I", len(payload)) + content + struct.pack(">I", zlib.crc32(content))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )


FAKE_STANDALONE = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
args = sys.argv[1:]
root = args[args.index("--root") + 1]
project = args[args.index("--project") + 1]
project_uid = args[args.index("--project-uid") + 1]
raw = sys.stdin.buffer.read()
header, sep, body = raw.partition(b"\n")
request = json.loads(header)
counter = os.environ["EVIDENCE_COUNTER"]
with open(counter, "a", encoding="utf-8") as stream:
    stream.write(request["operation"] + "\n")
if os.environ.get("EVIDENCE_REPLY") == "fail":
    print("credential=never-persist-this", file=sys.stderr)
    raise SystemExit(9)
digest = hashlib.sha256(body).hexdigest()
reported = "f" * 64 if os.environ.get("EVIDENCE_REPLY") == "bad-hash" else digest
path = pathlib.Path(root) / project / "tasks" / request["task_key"] / \
    "artifacts" / "attachments" / "objects" / digest
print(json.dumps({
    "schema": "r2-artifact-response-v1",
    "operation": "artifact-write",
    "object_sha256": reported,
    "byte_size": len(body),
    "object_path": str(path),
}))
'''


FAKE_WORKFLOW = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
assert sys.argv[1:] == ["workflow"]
request_raw = sys.stdin.buffer.read()
header, sep, body = request_raw.partition(b"\n")
request = json.loads(header)
counter = os.environ["EVIDENCE_COUNTER"]
with open(counter, "a", encoding="utf-8") as stream:
    stream.write(request["operation"] + "\n")
task = os.environ["EVIDENCE_TASK"]
project = pathlib.Path(os.environ["EVIDENCE_PROJECT"])
project_uid = os.environ["EVIDENCE_PROJECT_UID"]
task_uid = os.environ["EVIDENCE_TASK_UID"]
binding_sha = os.environ["EVIDENCE_BINDING_SHA"]
if request["operation"] == "resolve-task":
    result = {
        "mode": "r2",
        "binding_ref": {
            "path": str(project / "tasks" / task / "task.json"),
            "sha256": binding_sha,
        },
        "task_key": task,
        "project_path": str(project),
        "project_uid": project_uid,
        "task_uid": task_uid,
    }
    print(json.dumps({"schema": "r2-workflow-response-v1", "result": result}))
    raise SystemExit(0)
digest = hashlib.sha256(body).hexdigest()
path = project / "tasks" / task / "artifacts" / "attachments" / "objects" / digest
print(json.dumps({
    "schema": "r2-artifact-response-v1",
    "operation": "artifact-write",
    "object_sha256": digest,
    "byte_size": len(body),
    "object_path": str(path),
}))
'''


class AndroidToolsEvidenceTests(unittest.TestCase):
    def test_bundle_protocol_matches_launcher(self) -> None:
        self.assertEqual(EVIDENCE["BUNDLE_PROTOCOL"], 1)

    def test_operation_reservations_cover_direct_provider_staging(self) -> None:
        low_preview = EVIDENCE["reservation_for_operation"]("preview.render", 4096)
        high_preview = EVIDENCE["reservation_for_operation"]("preview.render", 8 * 1024 * 1024)
        expected_preview = (
            16 * 2 * EVIDENCE["MAX_ARTIFACT_BYTES"]
            + EVIDENCE["MAX_TOTAL_ARTIFACT_BYTES"]
            + EVIDENCE["RESERVATION_METADATA_BYTES"]
        )
        self.assertEqual(low_preview, expected_preview)
        self.assertEqual(high_preview, expected_preview)
        self.assertEqual(
            EVIDENCE["reservation_for_operation"]("device.capture", 4096),
            2 * EVIDENCE["MAX_ARTIFACT_BYTES"] + EVIDENCE["RESERVATION_METADATA_BYTES"],
        )
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["reservation_for_operation"]("unknown.operation", 4096)
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["reservation_for_operation"]("device.tap", 4095)

    def test_unlock_releases_original_thread_lock_after_fd_reuse(self) -> None:
        lock_dir = self.root / "fd-reuse-locks"
        lock_dir.mkdir(mode=0o700)
        original_fd = EVIDENCE["_lock"](lock_dir, "first.lock")
        original_key = (
            os.fstat(original_fd).st_dev, os.fstat(original_fd).st_ino,
        )
        actual_close = os.close
        fillers = []
        while True:
            filler = os.open(os.devnull, os.O_RDONLY)
            fillers.append(filler)
            if filler >= original_fd:
                break
        replacement_fd = None
        replacement_key = None

        def close_and_reuse(fd: int) -> None:
            nonlocal replacement_fd, replacement_key
            actual_close(fd)
            if fd == original_fd and replacement_fd is None:
                replacement_fd = os.open(
                    lock_dir / "second.lock",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                self.assertEqual(replacement_fd, original_fd)
                replacement_info = os.fstat(replacement_fd)
                replacement_key = (replacement_info.st_dev, replacement_info.st_ino)
                self.assertTrue(EVIDENCE["_thread_lock"](replacement_fd, blocking=True))

        try:
            with patch.object(os, "close", close_and_reuse):
                EVIDENCE["_unlock"](original_fd)
            self.assertNotIn(original_key, EVIDENCE["_LOCK_REGISTRY"])
            self.assertIn(replacement_fd, EVIDENCE["_FD_LOCKS"])
            self.assertIn(replacement_key, EVIDENCE["_LOCK_REGISTRY"])
        finally:
            if replacement_fd is not None:
                EVIDENCE["_release_thread_lock"](replacement_fd)
                actual_close(replacement_fd)
            for filler in fillers:
                actual_close(filler)

    def test_missing_invocation_export_never_calls_memory_helper(self) -> None:
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "missing-invocation-helper-calls"
        unknown_id = "00000000-0000-4000-8000-000000000001"
        handle = EVIDENCE["open_run"](self.record_root, unknown_id)
        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            with self.assertRaises(EVIDENCE["EvidenceError"]):
                EVIDENCE["export_run"](handle, self.binding(), helper=helper)
        self.assertFalse(counter.exists())

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.record_root = self.root / "private-records"
        self.device_effects: list[str] = []
        self.handles = []
        self._original_begin_run = EVIDENCE["begin_run"]

        def tracked_begin_run(*args, **kwargs):
            handle = self._original_begin_run(*args, **kwargs)
            self.handles.append(handle)
            return handle

        EVIDENCE["begin_run"] = tracked_begin_run
        self.handle = EVIDENCE["begin_run"](
            self.record_root,
            "device.capture",
            "mobile-native",
            {
                "target": ":app@devDebug",
                "device_serial": "emulator-5554",
                "lease_id": "lease-123",
                "generation": 4,
                "source_fingerprint": "a" * 64,
            },
        )

    def tearDown(self) -> None:
        EVIDENCE["begin_run"] = self._original_begin_run
        for handle in reversed(self.handles):
            EVIDENCE["release_run"](handle)
        self.temp.cleanup()

    def write_helper(self, name: str, content: str) -> Path:
        helper = self.root / name
        helper.write_text(content, encoding="utf-8")
        helper.chmod(0o700)
        return helper

    def binding(self) -> dict[str, str]:
        return {
            "project_root": str(self.root / "memory"),
            "project_key": "appsome",
            "project_uid": "519462bf-a544-4a83-a9aa-0f3265bc42c8",
            "task_key": "intake-7098a68b-1fed-4bff-b6f7-f5c6cbd7876c",
            "task_uid": "7098a68b-1fed-4bff-b6f7-f5c6cbd7876c",
            "task_binding_sha256": "b" * 64,
        }

    def set_started_at(self, handle, timestamp: str) -> None:
        path = handle.run_directory / "started.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["started_at"] = timestamp
        path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8")
        path.chmod(0o600)

    def make_started_at(self, handle, age_seconds: int) -> None:
        started = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        self.set_started_at(handle, started.isoformat(timespec="microseconds").replace("+00:00", "Z"))

    def add_pressure(self, handle, size: int = 800_000) -> None:
        path = handle.staging_directory / "retention-pressure.bin"
        path.write_bytes(b"x" * size)
        path.chmod(0o600)

    def finish_native_result(self, *, effect: str = "observed") -> Path:
        image = png_1x1()
        staged = self.handle.staging_directory / "screen.png"
        staged.write_bytes(image)
        staged.chmod(0o600)
        artifact = {
            "path": str(staged),
            "media_type": "image/png",
            "size_bytes": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
            "width": 1,
            "height": 1,
        }
        result = NATIVE["NativeResult"](
            transport="completed",
            effect=effect,
            error=None,
            data={
                "visible": True,
                "selector": {"field": "text", "value": "do-not-persist-this-text"},
                "text": "do-not-persist-this-doc-text",
            },
            artifacts=(artifact,),
        )
        return EVIDENCE["finish_run"](self.handle, result)

    def test_native_result_is_sanitized_and_artifacts_are_content_addressed(self) -> None:
        manifest_path = self.finish_native_result(effect="unknown")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["outcome"]["effect"], "unknown")
        self.assertEqual(manifest["outcome"]["data"], {
            "selector": {"field": "text"},
            "visible": True,
        })
        self.assertNotIn("do-not-persist", manifest_path.read_text(encoding="utf-8"))
        artifact = manifest["artifacts"][0]
        stored = self.handle.run_directory / artifact["local_locator"]
        self.assertEqual(stored.read_bytes(), png_1x1())
        self.assertEqual(artifact["sha256"], hashlib.sha256(stored.read_bytes()).hexdigest())
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.handle.run_directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.handle.staging_directory.stat().st_mode & 0o777, 0o700)

    def test_symlink_and_unsafe_context_fail_closed(self) -> None:
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["begin_run"](linked / "records", "device.tap", "mobile-native", {})
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["begin_run"](
                self.root / "bad-context", "device.tap", "mobile-native",
                {"source_root": "/private/source"},
            )

    def test_artifact_outside_staging_or_hash_mismatch_fails(self) -> None:
        image = png_1x1()
        outside = self.root / "outside.png"
        outside.write_bytes(image)
        outcome = NATIVE["NativeResult"](
            "completed", "observed", artifacts=({
                "path": str(outside), "media_type": "image/png", "size_bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(), "width": 1, "height": 1,
            },),
        )
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["finish_run"](self.handle, outcome)
        staged = self.handle.staging_directory / "wrong.png"
        staged.write_bytes(image)
        staged.chmod(0o600)
        mismatched = NATIVE["NativeResult"](
            "completed", "observed", artifacts=({
                "path": str(staged), "media_type": "image/png", "size_bytes": len(image),
                "sha256": "f" * 64, "width": 1, "height": 1,
            },),
        )
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["finish_run"](self.handle, mismatched)

    def test_nested_artifact_is_contained_and_content_verified(self) -> None:
        image = png_1x1()
        nested_dir = self.handle.staging_directory / "capture"
        nested_dir.mkdir(mode=0o700)
        nested = nested_dir / "screen.png"
        nested.write_bytes(image)
        nested.chmod(0o600)
        artifact = {
            "path": str(nested),
            "media_type": "image/png",
            "size_bytes": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
            "width": 1,
            "height": 1,
        }
        result = NATIVE["NativeResult"]("completed", "observed", artifacts=(artifact,))
        path = EVIDENCE["finish_run"](self.handle, result)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        stored = self.handle.run_directory / manifest["artifacts"][0]["local_locator"]
        self.assertEqual(stored.read_bytes(), image)
        self.assertEqual(manifest["artifacts"][0]["name"], "capture/screen.png")

    def test_native_preview_result_with_png_and_json_artifacts_finishes(self) -> None:
        image = png_1x1()
        executable = self.root / "fake-google-android"
        encoded_image = base64.b64encode(image).decode("ascii")
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import base64, json, os, sys\n"
            "from pathlib import Path\n"
            "os.umask(0o077)\n"
            f"image_bytes = base64.b64decode({encoded_image!r})\n"
            "args = sys.argv[1:]\n"
            "if args[-1] == '--version':\n"
            "    print('4.4.1')\n"
            "    raise SystemExit(0)\n"
            "output = next(arg.split('=', 1)[1] for arg in args "
            "if arg.startswith('--output='))\n"
            "directory = Path(output) / 'com' / 'example'\n"
            "directory.mkdir(parents=True, mode=0o700)\n"
            "image = directory / 'LoginPreview_0.png'\n"
            "tree = directory / 'LoginPreview_0.json'\n"
            "image.write_bytes(image_bytes)\n"
            "tree.write_text(json.dumps({"
            "'components': [], 'densityDpi': 420, 'deviceId': 'test-device', "
            "'fontScale': 1.0, 'height': 1, 'methodFQN': 'com.example.LoginPreview', "
            "'previewId': 'LoginPreview', 'uiMode': 'normal', 'width': 1, "
            "'apiToken': 'must-be-redacted'}))\n"
            "print(json.dumps({'success': True, 'results': [{"
            "'previewId': 'LoginPreview', 'methodFqn': 'com.example.LoginPreview', "
            "'imagePath': str(image), 'componentTreePath': str(tree)}]}))\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        outcome = NATIVE["execute_android_native"](
            "google-android",
            "preview.render",
            {"target": "app@devDebug", "fqn": "com.example.LoginPreview"},
            executable=executable,
            expected_version="4.4.1",
            source_root=self.root,
            staging_dir=self.handle.staging_directory,
        )

        self.assertIsInstance(outcome, NATIVE["NativeResult"])
        self.assertIsNone(outcome.error)
        self.assertEqual(len(outcome.artifacts), 2)
        manifest_path = EVIDENCE["finish_run"](self.handle, outcome)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts = {item["media_type"]: item for item in manifest["artifacts"]}
        self.assertEqual(artifacts["image/png"]["width"], 1)
        self.assertEqual(artifacts["image/png"]["height"], 1)
        self.assertNotIn("width", artifacts["application/json"])
        self.assertNotIn("height", artifacts["application/json"])
        tree_bytes = (self.handle.run_directory / artifacts["application/json"]["local_locator"])
        self.assertNotIn(b"must-be-redacted", tree_bytes.read_bytes())

    def test_export_success_uses_standalone_attachment_protocol(self) -> None:
        self.finish_native_result()
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "helper-calls"
        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            result = EVIDENCE["export_run"](
                self.handle, self.binding(), helper=helper, include_artifacts=True,
            )
        self.assertEqual(result.status, "exported")
        self.assertEqual(len(result.attachment_sha256s), 2)
        self.assertEqual(counter.read_text().splitlines(), ["artifact-write", "artifact-write"])
        history = (self.handle.run_directory / "export.jsonl").read_text(encoding="utf-8")
        self.assertIn(result.manifest_sha256, history)
        self.assertNotIn(str(self.root), history)

    def test_failed_export_retry_and_hash_mismatch_never_replay_device_effect(self) -> None:
        self.finish_native_result()
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "helper-calls"
        self.device_effects.append("one-device-action")
        with patch.dict(os.environ, {
            "EVIDENCE_COUNTER": str(counter),
            "EVIDENCE_REPLY": "fail",
        }):
            first = EVIDENCE["export_run"](self.handle, self.binding(), helper=helper)
        self.assertEqual(first.status, "unknown")
        self.assertEqual(first.reason_code, "helper_failed")
        with patch.dict(os.environ, {
            "EVIDENCE_COUNTER": str(counter),
            "EVIDENCE_REPLY": "bad-hash",
        }):
            second = EVIDENCE["export_run"](self.handle, self.binding(), helper=helper)
        self.assertEqual(second.status, "unknown")
        self.assertEqual(second.reason_code, "receipt_mismatch")
        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            third = EVIDENCE["export_run"](self.handle, self.binding(), helper=helper)
        self.assertEqual(third.status, "exported")
        self.assertEqual([row for row in counter.read_text().splitlines()],
                         ["artifact-write", "artifact-write", "artifact-write"])
        self.assertEqual(self.device_effects, ["one-device-action"])
        attempts = [
            json.loads(line)
            for line in (self.handle.run_directory / "export.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [item["status"] for item in attempts],
            ["pending", "unknown", "pending", "unknown", "pending", "exported"],
        )
        self.assertNotIn("credential=never-persist-this", repr(attempts))

    def test_export_intent_is_durable_before_attachment_write(self) -> None:
        self.finish_native_result()
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "interrupted-export-calls"
        export_globals = EVIDENCE["export_run"].__globals__
        append_export = export_globals["_append_export"]

        def interrupt_before_final_receipt(run, record) -> None:
            if record["status"] == "exported":
                raise RuntimeError("simulated interruption before final receipt")
            append_export(run, record)

        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            with patch.dict(export_globals, {"_append_export": interrupt_before_final_receipt}):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    EVIDENCE["export_run"](self.handle, self.binding(), helper=helper)

        self.assertEqual(counter.read_text(encoding="utf-8").splitlines(), ["artifact-write"])
        history_path = self.handle.run_directory / "export.jsonl"
        history = [json.loads(line) for line in history_path.read_text().splitlines()]
        self.assertEqual([entry["status"] for entry in history], ["pending"])
        self.assertTrue(self.handle.run_directory.exists())

        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            retried = EVIDENCE["export_run"](self.handle, self.binding(), helper=helper)
        self.assertEqual(retried.status, "exported")
        self.assertEqual([entry["status"] for entry in
                          (json.loads(line) for line in history_path.read_text().splitlines())],
                         ["pending", "pending", "exported"])

    def test_export_cache_rejects_malformed_export_receipts(self) -> None:
        self.finish_native_result()
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "receipt-calls"
        environment = {"EVIDENCE_COUNTER": str(counter)}
        with patch.dict(os.environ, environment):
            first = EVIDENCE["export_run"](
                self.handle, self.binding(), helper=helper, include_artifacts=True,
            )
        self.assertEqual(first.status, "exported")
        history_path = self.handle.run_directory / "export.jsonl"
        corruptions = (
            lambda rows: rows.pop(),
            lambda rows: rows[0].__setitem__("sha256", "f" * 64),
            lambda rows: rows[0].__setitem__(
                "size_bytes", rows[0]["size_bytes"] + 1,
            ),
            lambda rows: rows[0].__setitem__("locator", "tasks/wrong/object"),
        )
        for corrupt in corruptions:
            history = [
                json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()
            ]
            corrupt(history[-1]["exported_attachments"])
            history_path.write_text(
                "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":"))
                          for row in history) + "\n",
                encoding="utf-8",
            )
            before_retry = len(counter.read_text(encoding="utf-8").splitlines())
            with patch.dict(os.environ, environment):
                retried = EVIDENCE["export_run"](
                    self.handle, self.binding(), helper=helper, include_artifacts=True,
                )
            self.assertEqual(retried.status, "exported")
            self.assertEqual(
                len(counter.read_text(encoding="utf-8").splitlines()),
                before_retry + 2,
            )

        second = EVIDENCE["begin_run"](
            self.record_root, "device.tap", "mobile-native", {},
        )
        self.assertTrue(second.run_directory.exists())
        self.assertTrue(self.handle.run_directory.exists())

    def test_export_cache_isolated_by_destination_root(self) -> None:
        self.finish_native_result()
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "helper-calls"
        first_binding = self.binding()
        first_binding["project_root"] = str(self.root / "memory-one")
        second_binding = dict(first_binding, project_root=str(self.root / "memory-two"))
        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            first = EVIDENCE["export_run"](self.handle, first_binding, helper=helper)
            second = EVIDENCE["export_run"](self.handle, second_binding, helper=helper)
        self.assertEqual(first.status, "exported")
        self.assertEqual(second.status, "exported")
        self.assertEqual(counter.read_text().splitlines(), ["artifact-write", "artifact-write"])

    def test_workflow_revalidates_binding_before_cross_interface_cache_hit(self) -> None:
        self.finish_native_result()
        standalone = self.write_helper("project-memory", FAKE_STANDALONE)
        workflow = self.write_helper("workflow-project-memory", FAKE_WORKFLOW)
        counter = self.root / "helper-calls"
        standalone_binding = self.binding()
        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            first = EVIDENCE["export_run"](
                self.handle, standalone_binding, helper=standalone,
            )
        self.assertEqual(first.status, "exported")

        workspace = self.root / "workspace"
        workspace.mkdir()
        project = self.root / "r2" / "appsome"
        project.mkdir(parents=True)
        workflow_binding = {
            "workspace_path": str(workspace),
            "project_path": str(project),
            "project_uid": standalone_binding["project_uid"],
            "task_key": standalone_binding["task_key"],
            "task_uid": standalone_binding["task_uid"],
            "task_binding_sha256": standalone_binding["task_binding_sha256"],
        }
        env = {
            "EVIDENCE_COUNTER": str(counter),
            "EVIDENCE_TASK": workflow_binding["task_key"],
            "EVIDENCE_PROJECT": str(project),
            "EVIDENCE_PROJECT_UID": workflow_binding["project_uid"],
            "EVIDENCE_TASK_UID": workflow_binding["task_uid"],
            "EVIDENCE_BINDING_SHA": "c" * 64,
        }
        with patch.dict(os.environ, env):
            retry = EVIDENCE["export_run"](
                self.handle, workflow_binding, helper=workflow, interface="workflow",
            )
        self.assertEqual(retry.status, "blocked")
        self.assertEqual(retry.reason_code, "binding_mismatch")
        self.assertEqual(counter.read_text().splitlines(), ["artifact-write", "resolve-task"])

    def test_missing_binding_blocks_export_without_running_effect_or_helper(self) -> None:
        self.finish_native_result()
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "helper-calls"
        self.device_effects.append("effect-already-observed")
        with self.assertRaises(EVIDENCE["BindingError"]):
            EVIDENCE["export_run"](self.handle, {}, helper=helper)
        self.assertFalse(counter.exists())
        self.assertEqual(self.device_effects, ["effect-already-observed"])

    def test_workflow_export_verifies_exact_binding_before_attachment_write(self) -> None:
        self.finish_native_result()
        helper = self.write_helper("workflow-project-memory", FAKE_WORKFLOW)
        workspace = self.root / "workspace"
        workspace.mkdir()
        project = self.root / "r2" / "appsome"
        project.mkdir(parents=True)
        binding = {
            "workspace_path": str(workspace),
            "project_path": str(project),
            "project_uid": "project-uid",
            "task_key": "APP-7",
            "task_uid": "task-uid",
            "task_binding_sha256": "c" * 64,
        }
        counter = self.root / "workflow-calls"
        env = {
            "EVIDENCE_COUNTER": str(counter),
            "EVIDENCE_TASK": binding["task_key"],
            "EVIDENCE_PROJECT": str(project),
            "EVIDENCE_PROJECT_UID": binding["project_uid"],
            "EVIDENCE_TASK_UID": binding["task_uid"],
            "EVIDENCE_BINDING_SHA": binding["task_binding_sha256"],
        }
        with patch.dict(os.environ, env):
            result = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )
            cached = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )
        self.assertEqual(result.status, "exported")
        self.assertEqual(cached.status, "exported")
        self.assertEqual(
            counter.read_text().splitlines(),
            ["resolve-task", "artifact-write", "resolve-task"],
        )

    def test_retention_age_uses_creation_time_not_recent_export(self) -> None:
        root = self.root / "age-retention"
        first = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.make_started_at(first, 8 * 24 * 60 * 60)
        EVIDENCE["finish_run"](first, NATIVE["NativeResult"]("completed", "unknown"))
        EVIDENCE["release_run"](first)

        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "age-export-calls"
        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            result = EVIDENCE["export_run"](
                EVIDENCE["open_run"](root, first.invocation_id), self.binding(),
                helper=helper, max_age_seconds=31_536_000,
            )
        self.assertEqual(result.status, "exported")

        second = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {},
                                       max_age_seconds=60)
        self.assertFalse(first.run_directory.exists())
        self.assertTrue(second.run_directory.exists())
        self.assertEqual(self.device_effects, [])

    def test_retention_expires_at_the_exact_age_boundary(self) -> None:
        root = self.root / "age-boundary"
        first = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        now_ns = time.time_ns() + 1_000_000_000
        created_ns = now_ns - 60 * 1_000_000_000
        seconds, nanoseconds = divmod(created_ns, 1_000_000_000)
        created = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self.set_started_at(first, f"{created}.{nanoseconds:09d}Z")
        EVIDENCE["finish_run"](first, NATIVE["NativeResult"]("completed", "unknown"))
        EVIDENCE["release_run"](first)

        time_module = EVIDENCE["begin_run"].__globals__["time"]
        with patch.object(time_module, "time_ns", return_value=now_ns):
            second = EVIDENCE["begin_run"](
                root, "device.tap", "mobile-native", {}, max_age_seconds=60,
            )
        self.assertFalse(first.run_directory.exists())
        self.assertTrue(second.run_directory.exists())

    def test_byte_pressure_evicts_oldest_unknown_run_without_claiming_success(self) -> None:
        root = self.root / "unknown-pressure"
        first = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.add_pressure(first)
        EVIDENCE["finish_run"](first, NATIVE["NativeResult"]("completed", "unknown"))
        EVIDENCE["release_run"](first)

        second = EVIDENCE["begin_run"](
            root, "device.tap", "mobile-native", {}, max_bytes=1_048_576,
            max_age_seconds=31_536_000,
        )
        self.assertFalse(first.run_directory.exists())
        self.assertTrue(second.run_directory.exists())
        start = json.loads((second.run_directory / "started.json").read_text())
        self.assertEqual(start["outcome"]["effect"], "unknown")
        self.assertFalse(second.manifest_path.exists())
        self.assertEqual(self.device_effects, [])

    def test_byte_pressure_can_evict_pending_unknown_export(self) -> None:
        root = self.root / "pending-pressure"
        first = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.add_pressure(first)
        EVIDENCE["finish_run"](first, NATIVE["NativeResult"]("completed", "observed"))
        EVIDENCE["release_run"](first)
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "pending-export-calls"
        with patch.dict(os.environ, {
            "EVIDENCE_COUNTER": str(counter), "EVIDENCE_REPLY": "fail",
        }):
            result = EVIDENCE["export_run"](
                EVIDENCE["open_run"](root, first.invocation_id), self.binding(),
                helper=helper, max_bytes=1_048_576, max_age_seconds=31_536_000,
            )
        self.assertEqual(result.status, "unknown")
        self.assertTrue(first.run_directory.exists())

        second = EVIDENCE["begin_run"](
            root, "device.tap", "mobile-native", {}, max_bytes=1_048_576,
            max_age_seconds=31_536_000,
        )
        self.assertFalse(first.run_directory.exists())
        self.assertTrue(second.run_directory.exists())
        self.assertEqual(self.device_effects, [])

    def test_byte_pressure_evicts_oldest_record_first(self) -> None:
        root = self.root / "oldest-first"
        first = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.add_pressure(first, 400_000)
        EVIDENCE["finish_run"](first, NATIVE["NativeResult"]("completed", "unknown"))
        EVIDENCE["release_run"](first)
        second = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.add_pressure(second, 400_000)
        EVIDENCE["finish_run"](second, NATIVE["NativeResult"]("completed", "observed"))
        EVIDENCE["release_run"](second)

        third = EVIDENCE["begin_run"](
            root, "device.tap", "mobile-native", {}, max_bytes=1_048_576,
            max_age_seconds=31_536_000,
        )
        self.assertFalse(first.run_directory.exists())
        self.assertTrue(second.run_directory.exists())
        self.assertTrue(third.run_directory.exists())

    def test_byte_pressure_evicts_oldest_before_newer_run(self) -> None:
        root = self.root / "oldest-first"
        first = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.add_pressure(first, 350_000)
        EVIDENCE["finish_run"](first, NATIVE["NativeResult"]("completed", "unknown"))
        EVIDENCE["release_run"](first)
        second = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.add_pressure(second, 350_000)
        EVIDENCE["finish_run"](second, NATIVE["NativeResult"]("completed", "observed"))
        EVIDENCE["release_run"](second)

        third = EVIDENCE["begin_run"](
            root, "device.tap", "mobile-native", {}, max_bytes=1_048_576,
            max_age_seconds=31_536_000,
        )
        self.assertFalse(first.run_directory.exists())
        self.assertTrue(second.run_directory.exists())
        self.assertTrue(third.run_directory.exists())

    def test_active_reservation_rejects_capacity_before_dispatch(self) -> None:
        root = self.root / "capacity"
        active = EVIDENCE["begin_run"](
            root, "device.tap", "mobile-native", {}, max_bytes=1_048_576,
            max_age_seconds=31_536_000, reservation_bytes=900_000,
        )
        with self.assertRaises(EVIDENCE["RetentionLimitError"]):
            EVIDENCE["begin_run"](
                root, "device.tap", "mobile-native", {}, max_bytes=1_048_576,
                max_age_seconds=31_536_000,
            )
        invocations = [entry for entry in (root / "runs").iterdir()
                       if entry.name != ".retention.lock"]
        self.assertEqual(invocations, [active.run_directory])
        self.assertTrue((active.run_directory / "started.json").exists())
        self.assertEqual(self.device_effects, [])

    def test_abandoned_incomplete_run_expires_only_after_activity_lock_releases(self) -> None:
        root = self.root / "stale-incomplete"
        stale = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.make_started_at(stale, 2 * 24 * 60 * 60)
        current = EVIDENCE["begin_run"](
            root, "device.tap", "mobile-native", {}, max_age_seconds=60,
        )
        self.assertTrue(stale.run_directory.exists())
        EVIDENCE["release_run"](stale)

        later = EVIDENCE["begin_run"](
            root, "device.tap", "mobile-native", {}, max_age_seconds=60,
        )
        self.assertFalse(stale.run_directory.exists())
        self.assertTrue(current.run_directory.exists())
        self.assertTrue(later.run_directory.exists())

    def test_admission_is_not_limited_to_fifty_records(self) -> None:
        root = self.root / "many-records"
        for _ in range(51):
            handle = EVIDENCE["begin_run"](root, "docs.fetch", "google-android", {})
            EVIDENCE["finish_run"](
                handle, NATIVE["NativeResult"]("completed", "not_applicable"),
            )
            EVIDENCE["release_run"](handle)
        runs = [entry for entry in (root / "runs").iterdir()
                if entry.name != ".retention.lock"]
        self.assertEqual(len(runs), 51)

    def test_concurrent_admissions_serialize_capacity_reservations(self) -> None:
        root = self.root / "concurrent-capacity"
        barrier = threading.Barrier(6)
        baseline_locks = dict(EVIDENCE["_FD_LOCKS"])
        baseline_users = {
            key: slot.users for key, slot in EVIDENCE["_LOCK_REGISTRY"].items()
        }

        def admit(_index: int):
            barrier.wait()
            try:
                return EVIDENCE["begin_run"](
                    root, "docs.fetch", "google-android", {}, max_bytes=1_048_576,
                    max_age_seconds=31_536_000,
                )
            except EVIDENCE["RetentionLimitError"]:
                return None

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(admit, range(6)))
        admitted = [item for item in results if item is not None]
        self.assertEqual(len(admitted), 5)
        runs = [entry for entry in (root / "runs").iterdir()
                if entry.name != ".retention.lock"]
        self.assertEqual(len(runs), 5)
        for handle in admitted:
            EVIDENCE["release_run"](handle)
        self.assertEqual(EVIDENCE["_FD_LOCKS"], baseline_locks)
        self.assertEqual(
            {key: slot.users for key, slot in EVIDENCE["_LOCK_REGISTRY"].items()},
            baseline_users,
        )

    def test_concurrent_root_lock_creation_uses_exclusive_create(self) -> None:
        root = self.root / "root-lock-creation"
        root.mkdir(mode=0o700)
        barrier = threading.Barrier(8)

        def acquire(_index: int) -> None:
            barrier.wait()
            fd = EVIDENCE["_lock"](root, ".retention.lock")
            EVIDENCE["_unlock"](fd)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(acquire, range(8)))
        self.assertTrue((root / ".retention.lock").is_file())

    def test_export_pin_and_reservation_are_atomic_with_admission(self) -> None:
        root = self.root / "export-admission-race"
        first = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        EVIDENCE["finish_run"](first, NATIVE["NativeResult"]("completed", "unknown"))
        EVIDENCE["release_run"](first)
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        export_globals = EVIDENCE["export_run"].__globals__
        pinned, continue_export = threading.Event(), threading.Event()
        inventory_started = threading.Event()
        admission_started = threading.Event()
        admission_done, helper_entered, continue_helper = (
            threading.Event(), threading.Event(), threading.Event(),
        )
        original_run_info = export_globals["_run_info"]
        original_inventory = export_globals["_store_inventory"]
        export_results, export_errors = [], []
        admission_results, admission_errors = [], []
        pause_once = {"value": False}

        def gated_run_info(run_path, invocation_id):
            info = original_run_info(run_path, invocation_id)
            if run_path == first.run_directory and not pause_once["value"]:
                pause_once["value"] = True
                pinned.set()
                if not continue_export.wait(timeout=5):
                    raise AssertionError("export pin gate was not released")
            return info

        def track_inventory(runs):
            if threading.current_thread().name == "retention-admission":
                inventory_started.set()
            return original_inventory(runs)

        def slow_write(*_args, **_kwargs):
            helper_entered.set()
            if not continue_helper.wait(timeout=5):
                return False, "helper_timeout"
            return True, ""

        def run_export():
            try:
                with patch.dict(export_globals, {
                    "_run_info": gated_run_info,
                    "_store_inventory": track_inventory,
                    "_write_attachment": slow_write,
                }):
                    export_results.append(EVIDENCE["export_run"](
                        EVIDENCE["open_run"](root, first.invocation_id), self.binding(),
                        helper=helper,
                    ))
            except BaseException as error:
                export_errors.append(error)

        def run_admission():
            try:
                admission_started.set()
                admission_results.append(EVIDENCE["begin_run"](
                    root, "docs.fetch", "google-android", {},
                ))
            except BaseException as error:
                admission_errors.append(error)
            finally:
                admission_done.set()

        exporter = threading.Thread(target=run_export, name="retention-export")
        admission = None
        pinned_seen = admission_started_seen = helper_seen = False
        inventory_during_pin = False
        try:
            exporter.start()
            pinned_seen = pinned.wait(timeout=5)
            if pinned_seen:
                admission = threading.Thread(
                    target=run_admission, name="retention-admission",
                )
                admission.start()
                admission_started_seen = admission_started.wait(timeout=5)
                if admission_started_seen:
                    # In the broken ordering, inventory sees a pin without its
                    # reservation. Fixed code keeps root locked until publication.
                    inventory_during_pin = inventory_started.wait(timeout=0.2)
        finally:
            continue_export.set()
            continue_helper.set()
            exporter.join(timeout=5)
            if admission is not None:
                admission.join(timeout=5)
        helper_seen = helper_entered.is_set()
        self.assertTrue(pinned_seen)
        self.assertTrue(admission_started_seen)
        self.assertTrue(helper_seen)
        self.assertFalse(inventory_during_pin)
        self.assertTrue(admission_done.is_set())
        self.assertEqual(admission_errors, [])
        self.assertEqual(len(admission_results), 1)
        self.assertTrue(admission_results[0].run_directory.exists())
        self.assertFalse(exporter.is_alive())
        self.assertFalse(admission.is_alive())
        self.assertEqual(export_errors, [])
        self.assertEqual(export_results[0].status, "exported")

    def test_export_pin_blocks_pressure_eviction_until_helper_returns(self) -> None:
        root = self.root / "export-pinned"
        first = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.add_pressure(first)
        EVIDENCE["finish_run"](first, NATIVE["NativeResult"]("completed", "observed"))
        EVIDENCE["release_run"](first)
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        entered, resume = threading.Event(), threading.Event()
        export_globals = EVIDENCE["export_run"].__globals__

        def slow_write(*_args, **_kwargs):
            entered.set()
            if not resume.wait(timeout=5):
                return False, "helper_timeout"
            return True, ""

        export_results = []
        export_errors = []

        def run_export():
            try:
                with patch.dict(export_globals, {"_write_attachment": slow_write}):
                    export_results.append(EVIDENCE["export_run"](
                        EVIDENCE["open_run"](root, first.invocation_id), self.binding(),
                        helper=helper, max_bytes=1_048_576, max_age_seconds=31_536_000,
                    ))
            except BaseException as error:
                export_errors.append(error)

        worker = threading.Thread(target=run_export)
        worker.start()
        self.assertTrue(entered.wait(timeout=5))
        with self.assertRaises(EVIDENCE["RetentionLimitError"]):
            EVIDENCE["begin_run"](
                root, "device.tap", "mobile-native", {}, max_bytes=1_048_576,
                max_age_seconds=31_536_000,
            )
        self.assertTrue(first.run_directory.exists())
        resume.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(export_errors, [])
        self.assertEqual(export_results[0].status, "exported")
        self.assertTrue(first.run_directory.exists())

    def test_unsafe_store_entries_and_cleanup_errors_block_admission(self) -> None:
        root = self.root / "unsafe-store"
        run = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        EVIDENCE["release_run"](run)
        unrecognized = root / "runs" / "foreign-entry"
        unrecognized.write_text("foreign", encoding="utf-8")
        unrecognized.chmod(0o600)
        with self.assertRaisesRegex(EVIDENCE["EvidenceError"], "unexpected run-store entry"):
            EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        unrecognized.unlink()

        root = self.root / "unsafe-run"
        run = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        foreign_file = run.run_directory / "foreign.json"
        foreign_file.write_text("foreign", encoding="utf-8")
        foreign_file.chmod(0o600)
        EVIDENCE["release_run"](run)
        with self.assertRaisesRegex(EVIDENCE["EvidenceError"], "unrecognized run-store entry"):
            EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.assertTrue(foreign_file.exists())

        root = self.root / "symlink-run"
        runs = root / "runs"
        runs.mkdir(parents=True, mode=0o700)
        target = self.root / "foreign-target"
        target.mkdir(mode=0o700)
        sentinel = target / "keep.json"
        sentinel.write_text("keep", encoding="utf-8")
        sentinel.chmod(0o600)
        (runs / "00000000-0000-4000-8000-000000000002").symlink_to(
            target, target_is_directory=True,
        )
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.assertTrue(sentinel.exists())

        root = self.root / "cleanup-failure"
        old = EVIDENCE["begin_run"](root, "device.tap", "mobile-native", {})
        self.make_started_at(old, 2 * 24 * 60 * 60)
        self.add_pressure(old)
        EVIDENCE["finish_run"](old, NATIVE["NativeResult"]("completed", "unknown"))
        EVIDENCE["release_run"](old)
        globals_ = EVIDENCE["_remove_run"].__globals__
        with patch.object(globals_["shutil"], "rmtree", side_effect=OSError("simulated")):
            with self.assertRaisesRegex(EVIDENCE["EvidenceError"], "cannot completely remove"):
                EVIDENCE["begin_run"](
                    root, "device.tap", "mobile-native", {}, max_age_seconds=60,
                )
        self.assertTrue(old.run_directory.exists())


if __name__ == "__main__":
    unittest.main()
