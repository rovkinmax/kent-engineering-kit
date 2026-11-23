from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import importlib.util
import runpy
import sys
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from unittest.mock import patch
from test_android_tools_native import PNG, PNG_INVALID_CASES
from workflowkit.project_memory import ProjectMemory


ROOT = Path(__file__).resolve().parents[1]
NATIVE_SPEC = importlib.util.spec_from_file_location(
    "android_tools_native", ROOT / "templates/project/android_tools_native.py"
)
NATIVE_MODULE = importlib.util.module_from_spec(NATIVE_SPEC)
sys.modules[NATIVE_SPEC.name] = NATIVE_MODULE
NATIVE_SPEC.loader.exec_module(NATIVE_MODULE)
NATIVE = vars(NATIVE_MODULE)
EVIDENCE_SPEC = importlib.util.spec_from_file_location(
    "android_tools_evidence_tested", ROOT / "templates/project/android_tools_evidence.py"
)
EVIDENCE_MODULE = importlib.util.module_from_spec(EVIDENCE_SPEC)
sys.modules[EVIDENCE_SPEC.name] = EVIDENCE_MODULE
EVIDENCE_SPEC.loader.exec_module(EVIDENCE_MODULE)
EVIDENCE = vars(EVIDENCE_MODULE)


class SharedPngValidationTests(unittest.TestCase):
    def test_evidence_uses_native_validator(self):
        self.assertIs(EVIDENCE["validate_png_bytes"], NATIVE["validate_png_bytes"])
        self.assertNotIn("RunOutcome", EVIDENCE)
        self.assertNotIn("RunOutcome", EVIDENCE["__all__"])
        self.assertEqual(EVIDENCE["_png_dimensions"](PNG), (2, 3))
        for name, content in PNG_INVALID_CASES.items():
            with self.subTest(name=name), self.assertRaises(EVIDENCE["EvidenceError"]):
                EVIDENCE["_png_dimensions"](content)


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
if request.get("task_locator") is not None:
    raise SystemExit(2)
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


DURABLE_WORKFLOW = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, runpy, sys
sys.path.insert(0, os.environ["EVIDENCE_REPO_ROOT"])
from workflowkit.project_memory import ProjectMemory
import workflowkit.project_memory as core

assert sys.argv[1:] == ["workflow"]
raw = sys.stdin.buffer.read()
line, separator, body = raw.partition(b"\n")
if not separator:
    raise SystemExit(2)
request = json.loads(line)
state_path = pathlib.Path(os.environ["EVIDENCE_STATE"])
state = json.loads(state_path.read_text())
calls_path = pathlib.Path(os.environ["EVIDENCE_CALLS"])
with calls_path.open("a", encoding="utf-8") as calls:
    calls.write(json.dumps(request, sort_keys=True) + "\n")
locator = request.get("task_locator")
if locator not in state["tasks"]:
    raise SystemExit(2)
adapter = runpy.run_path(
    str(pathlib.Path(os.environ["EVIDENCE_REPO_ROOT"])
        / "templates/project/workflow-project-memory")
)
namespace = adapter["resolve_native"].__globals__
workspace = adapter["git_root"](request["workspace_path"])
project_id = adapter["native_project_id"](workspace)
native_task, script, session_id = adapter["native_task_context"](
    request, workspace, project_id,
)
if (native_task["summary"]["id"] != locator or script is not None
        or session_id != os.environ.get("KENT_SESSION_ID")):
    raise SystemExit(2)
namespace["PREADMISSION"] = {
    "mode": "r2",
    "standalone": False,
    "project_id": project_id,
    "workspace": str(workspace),
    "primary_root": state["workspace"],
    "task_id": locator,
    "primary_workspace_id": "workspace-fixture",
    "session_id": session_id,
}
def admitted_native_store(_core, _primary, _project_id, _workspace_id, summary, **_kwargs):
    task_key = summary["short_id"]
    task_path = (pathlib.Path(state["root"]) / state["project_key"] / "tasks"
                 / task_key / "task.json")
    return ProjectMemory(
        pathlib.Path(state["root"]), state["project_key"], state["project_uid"],
    ), task_key, {
        "path": str(task_path),
        "sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
    }
namespace["admitted_native_store"] = admitted_native_store
fields = adapter["workflow_request_fields"](request)
core._closed(request, fields, "r2-workflow-request-v1")
if request["operation"] == "artifact-write":
    if (len(body) != request.get("byte_size")
            or hashlib.sha256(body).hexdigest() != request.get("sha256")):
        raise SystemExit(2)
elif body:
    raise SystemExit(2)
if request["operation"] == "artifact-read":
    digest = request.get("sha256")
    counts = state.setdefault("read_counts", {})
    counts[digest] = counts.get(digest, 0) + 1
    if (state.get("fail_next_read")
            or digest == state.get("fail_read_always_digest")
            or (digest == state.get("fail_read_digest")
                and counts[digest] == state.get("fail_read_occurrence"))):
        state["fail_next_read"] = False
        state_path.write_text(json.dumps(state, sort_keys=True))
        raise SystemExit(9)
    if state.get("fail_read_occurrence") is not None:
        state_path.write_text(json.dumps(state, sort_keys=True))
result = adapter["workflow_dispatch"](
    core, request, body if request["operation"] == "artifact-write" else None,
)
if request["operation"] == "resolve-task":
    if state.get("switch_after_resolve"):
        state["owner_task_id"] = state["switch_after_resolve"]
        state["switch_after_resolve"] = None
        state_path.write_text(json.dumps(state, sort_keys=True))
    print(json.dumps({"schema": "r2-workflow-response-v1", "result": result}))
    raise SystemExit(0)
if request["operation"] == "artifact-write":
    if state.get("fail_next_write_receipt"):
        state["fail_next_write_receipt"] = False
        state_path.write_text(json.dumps(state, sort_keys=True))
        raise SystemExit(9)
header, content = result
sys.stdout.buffer.write(
    json.dumps(header, sort_keys=True).encode("utf-8") + b"\n" + content,
)
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
        self.assertEqual(
            EVIDENCE["reservation_for_operation"]("device.input", 4096),
            EVIDENCE["RESERVATION_METADATA_BYTES"],
        )
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["reservation_for_operation"]("unknown.operation", 4096)
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["reservation_for_operation"]("device.tap", 4095)

    def test_input_manifest_keeps_only_dispatch_categories_and_safe_context(self) -> None:
        value = "stage-input-private-canary"
        handle = EVIDENCE["begin_run"](
            self.record_root,
            "device.input",
            "mobile-native",
            {
                "device_serial": "emulator-5554",
                "lease_id": "lease-123",
                "generation": 4,
            },
        )
        EVIDENCE["finish_run"](
            handle,
            {
                "transport": "completed",
                "effect": "unknown",
                "error": None,
                "data": {"dispatched": True, "text": value},
                "artifacts": [],
            },
        )
        manifest = json.loads(handle.manifest_path.read_text())

        self.assertEqual(manifest["operation"], "device.input")
        self.assertEqual(manifest["context"], {
            "device_serial": "emulator-5554", "lease_id": "lease-123", "generation": 4,
        })
        self.assertEqual(manifest["outcome"], {
            "transport": "completed", "effect": "unknown", "error": None, "data": {},
        })
        self.assertNotIn(value, json.dumps(manifest))

        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "input-export-calls"
        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            exported = EVIDENCE["export_run"](handle, self.binding(), helper=helper)
        self.assertEqual(exported.status, "exported")
        history = (handle.run_directory / "export.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(value, history)
        self.assertEqual(counter.read_text(encoding="utf-8").splitlines(), ["artifact-write"])

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
        if name == "kent-project-memory":
            helper = self.root / ".kent/ordinary-r2/bin/kent-project-memory"
            helper.parent.mkdir(parents=True, exist_ok=True)
        else:
            helper = self.root / name
        helper.write_text(content, encoding="utf-8")
        helper.chmod(0o700)
        return helper

    def workflow_fixture(self, *, switch_after_resolve: str | None = None,
                         fail_next_write_receipt: bool = False,
                         fail_next_read: bool = False):
        root = self.root / "r2-store"
        store = ProjectMemory.initialize(root, "appsome", "project-uid")
        task_ids = {"native-task-A": "APP-A", "native-task-B": "APP-B"}
        task_uids = {"APP-A": "task-uid-A", "APP-B": "task-uid-B"}
        for task_id, task_key in task_ids.items():
            store.initialize_task(
                task_key, task_uids[task_key],
                [{"namespace": "kent", "project_id": "fixture", "task_id": task_id}],
            )
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        native_bin = self.root / "native-bin"
        native_bin.mkdir()
        state_path = self.root / "r2-state.json"
        calls_path = self.root / "r2-calls.jsonl"
        native_tasks = {
            task_id: {
                "summary": {
                    "id": task_id,
                    "project_id": "fixture-project",
                    "short_id": task_key,
                    "workflow_id": "fixture-workflow",
                },
                "source_workspace": {
                    "root_path": str(workspace),
                    "is_primary": True,
                    "workspace_id": "workspace-fixture",
                },
                "project": {"default_workspace_id": "workspace-fixture"},
                "worktree_path": None,
                "live_sessions": [],
                "current_nodes": [],
            }
            for task_id, task_key in task_ids.items()
        }
        state = {
            "root": str(root),
            "project_key": "appsome",
            "project_uid": "project-uid",
            "workspace": str(workspace),
            "tasks": task_ids,
            "task_uids": task_uids,
            "native_tasks": native_tasks,
            "owner_task_id": "native-task-A",
            "switch_after_resolve": switch_after_resolve,
            "fail_next_write_receipt": fail_next_write_receipt,
            "fail_next_read": fail_next_read,
        }
        state_path.write_text(json.dumps(state, sort_keys=True))
        fake_kent = native_bin / "kent"
        fake_kent.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "state = json.loads(Path(os.environ['EVIDENCE_STATE']).read_text())\n"
            "args = sys.argv[1:]\n"
            "if len(args) == 2 and args[0] == 'project':\n"
            "    print('fixture-project')\n"
            "elif (len(args) == 6 and args[:2] == ['task', 'show']\n"
            "      and args[3:] == ['--project', 'fixture-project', '--json']):\n"
            "    task_id = args[2]\n"
            "    if task_id not in state['native_tasks']:\n"
            "        raise SystemExit(2)\n"
            "    task = dict(state['native_tasks'][task_id])\n"
            "    task['live_sessions'] = ([{'session_id': 'session-A'}]\n"
            "        if task_id == state['owner_task_id'] else [])\n"
            "    print(json.dumps(task))\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        fake_kent.chmod(0o700)
        helper = self.write_helper("kent-project-memory", DURABLE_WORKFLOW)
        binding = {
            "workspace_path": str(workspace),
            "project_path": str(store.path),
            "project_uid": store.project_uid,
            "task_key": "APP-A",
            "task_uid": task_uids["APP-A"],
            "task_binding_sha256": hashlib.sha256(
                (store.path / "tasks/APP-A/task.json").read_bytes()
            ).hexdigest(),
            "native_task_id": "native-task-A",
        }
        environment = {
            "EVIDENCE_REPO_ROOT": str(ROOT),
            "EVIDENCE_STATE": str(state_path),
            "EVIDENCE_CALLS": str(calls_path),
            "HOME": str(self.root),
            "PATH": str(native_bin) + os.pathsep + os.environ.get("PATH", ""),
            "KENT_SESSION_ID": "session-A",
        }
        return store, helper, binding, state_path, calls_path, environment

    def workflow_calls(self, path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def workflow_export_subprocess(self, binding, helper: Path, env, invocation_id: str):
        program = """
import json, sys
import android_tools_evidence as evidence
handle = evidence.open_run(sys.argv[1], sys.argv[2])
result = evidence.export_run(
    handle, json.loads(sys.argv[3]), helper=sys.argv[4], interface="workflow",
    include_artifacts=True,
)
print(json.dumps({
    "status": result.status,
    "manifest_sha256": result.manifest_sha256,
    "attachment_sha256s": result.attachment_sha256s,
    "attempt": result.attempt,
    "reason_code": result.reason_code,
}))
"""
        child_env = {
            **env,
            "PYTHONPATH": str(ROOT / "templates/project") + os.pathsep + str(ROOT / "tests"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [
                sys.executable, "-c", program, str(self.record_root), invocation_id,
                json.dumps(binding), str(helper),
            ],
            cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_legacy_workflow_binding_and_helper_remain_supported(self) -> None:
        workspace = self.root / "legacy-workspace"
        project = self.root / "legacy-r2/appsome"
        workspace.mkdir()
        project.mkdir(parents=True)
        legacy_helper = self.root / "legacy-tools/workflow-project-memory"
        legacy_helper.parent.mkdir()
        legacy_helper.write_text(FAKE_WORKFLOW, encoding="utf-8")
        legacy_helper.chmod(0o700)
        binding = {
            "workspace_path": str(workspace),
            "project_path": str(project),
            "project_uid": "project-uid",
            "task_key": "APP-7",
            "task_uid": "task-uid",
            "task_binding_sha256": "a" * 64,
        }

        _, args = EVIDENCE["_binding"](binding, "workflow")
        self.assertEqual(args["task_key"], "APP-7")
        self.assertNotIn("native_task_id", args)
        self.assertEqual(EVIDENCE["_helper"](legacy_helper, "workflow"), legacy_helper)

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

    def finish_native_result(self, *, effect: str = "observed",
                             duplicate_artifact: bool = False) -> Path:
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
        artifacts = (artifact,)
        if duplicate_artifact:
            duplicate = self.handle.staging_directory / "screen-copy.png"
            duplicate.write_bytes(image)
            duplicate.chmod(0o600)
            artifacts += ({**artifact, "path": str(duplicate)},)
        result = NATIVE["NativeResult"](
            transport="completed",
            effect=effect,
            error=None,
            data={
                "visible": True,
                "selector": {"field": "text", "value": "do-not-persist-this-text"},
                "text": "do-not-persist-this-doc-text",
            },
            artifacts=artifacts,
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

    def test_explicit_export_refuses_managed_record_without_mutation_or_helper(self) -> None:
        context = {"attempt_id": "00000000-0000-4000-8000-000000000001",
                   "generation": 1, "lease_id": "lease", "device_serial": "emulator-5554"}
        self.finish_native_result()
        managed = EVIDENCE["begin_run"](self.record_root, "device.key", "mobile-native", context)
        EVIDENCE["finish_run"](managed, NATIVE["NativeResult"]("completed", "observed"))
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "managed-helper-calls"

        def snapshot():
            return {path.name: path.read_bytes() for path in managed.run_directory.iterdir()
                    if path.is_file()}

        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}):
            retained = snapshot()
            with self.assertRaisesRegex(EVIDENCE["BindingError"], "managed export"):
                EVIDENCE["export_run"](managed, self.binding(), helper=helper)
            self.assertEqual(snapshot(), retained)
            EVIDENCE["release_run"](managed)
            reopened = EVIDENCE["open_run"](self.record_root, managed.invocation_id)
            retained = snapshot()
            with self.assertRaisesRegex(EVIDENCE["BindingError"], "managed export"):
                EVIDENCE["export_run"](reopened, self.binding(), helper=helper)
            self.assertEqual(snapshot(), retained)
            root_lock = managed.run_directory.parent / ".retention.lock"
            (managed.run_directory / ".active.lock").unlink()
            retained = snapshot()
            with self.assertRaisesRegex(EVIDENCE["BindingError"], "managed export"):
                EVIDENCE["export_run"](reopened, self.binding(), helper=helper)
            self.assertEqual(snapshot(), retained)
            root_lock.unlink()
            with self.assertRaisesRegex(EVIDENCE["BindingError"], "managed export"):
                EVIDENCE["export_run"](reopened, self.binding(), helper=helper)
            self.assertEqual(snapshot(), retained)
            self.assertFalse(root_lock.exists())
            self.assertFalse(counter.exists())
            legacy = EVIDENCE["export_run"](self.handle, self.binding(), helper=helper)
        self.assertEqual(legacy.status, "exported")

    def test_historical_png_export_does_not_revalidate_or_rewrite(self) -> None:
        # The former evidence parser admitted header/end-only PNGs.
        staged = EVIDENCE["stage_artifact"](
            self.handle, "historical.png", PNG_INVALID_CASES["missing_idat"], "image/png",
        )
        with patch.object(EVIDENCE_MODULE, "validate_png_bytes", return_value=(2, 3)):
            manifest_path = EVIDENCE["finish_run"](
                self.handle, NATIVE["NativeResult"]("completed", "observed"),
                artifacts=[staged],
            )
        original = manifest_path.read_bytes()
        helper = self.write_helper("project-memory", FAKE_STANDALONE)
        counter = self.root / "historical-helper-calls"
        with patch.dict(os.environ, {"EVIDENCE_COUNTER": str(counter)}), patch.object(
            EVIDENCE_MODULE, "validate_png_bytes",
            side_effect=AssertionError("historical export must not invoke new admission"),
        ):
            result = EVIDENCE["export_run"](
                self.handle, self.binding(), helper=helper, include_artifacts=True,
            )
        self.assertEqual(result.status, "exported")
        self.assertEqual(manifest_path.read_bytes(), original)

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
        with patch.dict(os.environ, env), patch.object(Path, "home", return_value=self.root):
            retry = EVIDENCE["export_run"](
                self.handle, workflow_binding, helper=workflow, interface="workflow",
            )
        self.assertEqual(retry.status, "blocked")
        self.assertEqual(retry.reason_code, "binding_mismatch")
        self.assertEqual(counter.read_text().splitlines(), ["artifact-write", "resolve-task"])

    def test_legacy_workflow_export_keeps_protocol_and_cache_behavior(self) -> None:
        self.finish_native_result()
        helper = self.write_helper("workflow-project-memory", FAKE_WORKFLOW)
        workspace = self.root / "legacy-workspace"
        workspace.mkdir()
        project = self.root / "legacy-r2/appsome"
        project.mkdir(parents=True)
        binding = {
            "workspace_path": str(workspace),
            "project_path": str(project),
            "project_uid": "project-uid",
            "task_key": "APP-7",
            "task_uid": "task-uid",
            "task_binding_sha256": "c" * 64,
        }
        counter = self.root / "legacy-workflow-calls"
        environment = {
            "EVIDENCE_COUNTER": str(counter),
            "EVIDENCE_TASK": binding["task_key"],
            "EVIDENCE_PROJECT": str(project),
            "EVIDENCE_PROJECT_UID": binding["project_uid"],
            "EVIDENCE_TASK_UID": binding["task_uid"],
            "EVIDENCE_BINDING_SHA": binding["task_binding_sha256"],
        }
        with patch.dict(os.environ, environment):
            first = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )
            cached = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )

        self.assertEqual(first.status, "exported")
        self.assertEqual(cached.status, "exported")
        self.assertEqual(
            counter.read_text(encoding="utf-8").splitlines(),
            ["resolve-task", "artifact-write", "resolve-task"],
        )

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
        store, helper, binding, _, calls_path, env = self.workflow_fixture()
        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            result = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )
            cached = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )
        self.assertEqual(result.status, "exported")
        self.assertEqual(cached.status, "exported")
        calls = self.workflow_calls(calls_path)
        self.assertEqual(
            [item["operation"] for item in calls],
            ["resolve-task", "artifact-write", "artifact-read", "resolve-task",
             "artifact-read"],
        )
        self.assertTrue(all(item["task_locator"] == binding["native_task_id"] for item in calls))
        self.assertEqual(sum(item["operation"] == "artifact-write" for item in calls), 1)
        attachments = [
            store.read_attachment("APP-A", digest)
            for digest in result.attachment_sha256s
        ]
        envelope = json.loads(attachments[0])
        self.assertNotIn("native_task_id", envelope)
        self.assertNotIn(binding["native_task_id"], attachments[0].decode("utf-8"))
        exported_record = json.loads(
            (self.handle.run_directory / "export.jsonl")
            .read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertNotIn("native_task_id", json.dumps(exported_record["binding"]))
        _, private_binding = EVIDENCE["_binding"](binding, "workflow")
        self.assertEqual(
            exported_record["binding_sha256"],
            EVIDENCE["_sha"](EVIDENCE["_json"]({
                "interface": "workflow", "binding": private_binding,
            })),
        )
        changed_binding = dict(private_binding, native_task_id="other-native-task")
        self.assertNotEqual(
            exported_record["binding_sha256"],
            EVIDENCE["_sha"](EVIDENCE["_json"]({
                "interface": "workflow", "binding": changed_binding,
            })),
        )

    def test_workflow_duplicate_artifacts_write_and_verify_unique_objects_once(self) -> None:
        self.finish_native_result(duplicate_artifact=True)
        _, helper, binding, _, calls_path, env = self.workflow_fixture()
        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            first = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
                include_artifacts=True,
            )

        self.assertEqual(first.status, "exported")
        self.assertEqual(len(first.attachment_sha256s), 3)
        self.assertEqual(first.attachment_sha256s[1], first.attachment_sha256s[2])
        first_calls = self.workflow_calls(calls_path)
        self.assertEqual(
            [item["operation"] for item in first_calls],
            ["resolve-task", "artifact-write", "artifact-read",
             "artifact-write", "artifact-read"],
        )
        self.assertEqual(
            sum(item["operation"] == "artifact-write" for item in first_calls), 2,
        )
        record = json.loads(
            (self.handle.run_directory / "export.jsonl")
            .read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(
            [item["sha256"] for item in record["exported_attachments"]],
            list(first.attachment_sha256s),
        )

        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            cached = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
                include_artifacts=True,
            )
        self.assertEqual(cached.status, "exported")
        self.assertEqual(cached.attempt, first.attempt)
        calls = self.workflow_calls(calls_path)
        self.assertEqual(
            [item["operation"] for item in calls],
            ["resolve-task", "artifact-write", "artifact-read",
             "artifact-write", "artifact-read", "resolve-task",
             "artifact-read", "artifact-read"],
        )
        self.assertEqual(
            [item["sha256"] for item in calls if item["operation"] == "artifact-read"][-2:],
            list(dict.fromkeys(first.attachment_sha256s)),
        )

    def test_workflow_duplicate_artifact_read_failure_cannot_poison_cached_result(self) -> None:
        self.finish_native_result(duplicate_artifact=True)
        _, helper, binding, state_path, calls_path, env = self.workflow_fixture()
        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            first = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
                include_artifacts=True,
            )
        self.assertEqual(first.status, "exported")

        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["fail_read_digest"] = first.attachment_sha256s[1]
        state["fail_read_occurrence"] = 2
        state["read_counts"] = {}
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            cached = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
                include_artifacts=True,
            )

        self.assertEqual(cached.status, "exported")
        self.assertEqual(cached.reason_code, "")
        self.assertEqual(cached.attempt, first.attempt)
        history = [
            json.loads(line)
            for line in (self.handle.run_directory / "export.jsonl")
            .read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(history[-1]["status"], "exported")
        self.assertEqual(history[-1]["reason_code"], "")
        self.assertEqual(history[-1]["attempt"], first.attempt)
        retry_reads = [
            item["sha256"] for item in self.workflow_calls(calls_path)
            if item["operation"] == "artifact-read"
        ][-2:]
        self.assertEqual(
            retry_reads, list(dict.fromkeys(first.attachment_sha256s)),
        )

        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["fail_read_always_digest"] = first.attachment_sha256s[1]
        state["read_counts"] = {}
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            failed = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
                include_artifacts=True,
            )
        self.assertEqual(failed.status, "unknown")
        self.assertTrue(failed.reason_code)
        history = [
            json.loads(line)
            for line in (self.handle.run_directory / "export.jsonl")
            .read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(history[-1]["status"], "unknown")
        self.assertTrue(history[-1]["reason_code"])

    def test_workflow_binding_requires_private_native_task_locator(self) -> None:
        _, helper, binding, _, _, _ = self.workflow_fixture()
        identity, args = EVIDENCE["_binding"](binding, "workflow")
        self.assertNotIn("native_task_id", identity)
        self.assertEqual(args["native_task_id"], "native-task-A")
        legacy_binding = {
            key: value for key, value in binding.items() if key != "native_task_id"
        }
        _, legacy_args = EVIDENCE["_binding"](legacy_binding, "workflow")
        self.assertNotIn("native_task_id", legacy_args)
        with patch.object(Path, "home", return_value=self.root), self.assertRaises(
            EVIDENCE["BindingError"],
        ):
            EVIDENCE["export_run"](
                self.handle, legacy_binding, helper=helper, interface="workflow",
            )
        with self.assertRaises(EVIDENCE["EvidenceError"]):
            EVIDENCE["_binding"](
                {**binding, "native_task_id": "../native-task-A"}, "workflow",
            )
        with self.assertRaises(EVIDENCE["BindingError"]):
            EVIDENCE["_binding"](
                {**self.binding(), "native_task_id": "native-task-A"}, "standalone",
            )

    def test_workflow_helper_is_exact_private_installed_file(self) -> None:
        with patch.object(Path, "home", return_value=self.root):
            helper = self.write_helper("kent-project-memory", "#!/bin/sh\nexit 0\n")
            self.assertEqual(EVIDENCE["_helper"](helper, "workflow"), helper)

            legacy = self.write_helper("workflow-project-memory", "#!/bin/sh\nexit 0\n")
            self.assertEqual(EVIDENCE["_helper"](legacy, "workflow"), legacy)

            alias = self.root / "alias/kent-project-memory"
            alias.parent.mkdir()
            alias.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            alias.chmod(0o700)
            with self.assertRaises(EVIDENCE["BindingError"]):
                EVIDENCE["_helper"](alias, "workflow")

            helper.unlink()
            target = self.root / "target-helper"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o700)
            helper.symlink_to(target)
            with self.assertRaises(EVIDENCE["EvidenceError"]):
                EVIDENCE["_helper"](helper, "workflow")

            helper.unlink()
            helper.mkdir()
            with self.assertRaises(EVIDENCE["EvidenceError"]):
                EVIDENCE["_helper"](helper, "workflow")

    def test_workflow_a_to_b_reassignment_blocks_before_wrong_task_write(self) -> None:
        self.finish_native_result()
        store, helper, binding, _, calls_path, env = self.workflow_fixture(
            switch_after_resolve="native-task-B",
        )
        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            result = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )

        self.assertNotEqual(result.status, "exported")
        self.assertFalse((store.path / "tasks/APP-B/artifacts").exists())
        calls = self.workflow_calls(calls_path)
        self.assertEqual([item["operation"] for item in calls], ["resolve-task", "artifact-write"])
        self.assertTrue(all(item["task_locator"] == "native-task-A" for item in calls))

    def test_workflow_missing_session_blocks_before_attachment_write(self) -> None:
        self.finish_native_result()
        store, helper, binding, _, calls_path, env = self.workflow_fixture()
        env["KENT_SESSION_ID"] = ""
        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            result = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )

        self.assertEqual(result.status, "blocked")
        self.assertFalse((store.path / "tasks/APP-A/artifacts").exists())
        self.assertEqual(
            [item["operation"] for item in self.workflow_calls(calls_path)],
            ["resolve-task"],
        )

    def test_workflow_wrong_workspace_blocks_before_attachment_write(self) -> None:
        self.finish_native_result()
        store, helper, binding, _, calls_path, env = self.workflow_fixture()
        wrong_workspace = self.root / "wrong-workspace"
        wrong_workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(wrong_workspace)], check=True)
        binding["workspace_path"] = str(wrong_workspace)
        with patch.dict(os.environ, env, clear=True), patch.object(
            Path, "home", return_value=self.root,
        ):
            result = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )

        self.assertEqual(result.status, "blocked")
        self.assertFalse((store.path / "tasks/APP-A/artifacts").exists())
        self.assertEqual(
            [item["operation"] for item in self.workflow_calls(calls_path)],
            ["resolve-task"],
        )

    def test_workflow_lost_write_receipt_recovers_by_durable_readback(self) -> None:
        self.finish_native_result()
        store, helper, binding, _, calls_path, env = self.workflow_fixture(
            fail_next_write_receipt=True,
        )
        invocation_id = self.handle.invocation_id
        EVIDENCE["release_run"](self.handle)
        self.handles.remove(self.handle)
        first = self.workflow_export_subprocess(binding, helper, env, invocation_id)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(len(first["attachment_sha256s"]), 2)
        stored = store.read_attachment("APP-A", first["attachment_sha256s"][0])
        self.assertEqual(
            json.loads(stored)["schema"], "android-tools-evidence-export-v1",
        )

        recovered = self.workflow_export_subprocess(binding, helper, env, invocation_id)
        self.assertEqual(recovered["status"], "exported")
        self.assertEqual(recovered["attachment_sha256s"], first["attachment_sha256s"])
        calls = self.workflow_calls(calls_path)
        self.assertEqual(
            [item["operation"] for item in calls],
            ["resolve-task", "artifact-write", "resolve-task", "artifact-read",
             "artifact-read", "artifact-write", "artifact-read"],
        )
        self.assertEqual(sum(item["operation"] == "artifact-write" for item in calls), 2)
        self.assertTrue(all(item["task_locator"] == "native-task-A" for item in calls))
        for digest in recovered["attachment_sha256s"]:
            content = store.read_attachment("APP-A", digest)
            self.assertEqual(hashlib.sha256(content).hexdigest(), digest)

    def test_workflow_read_failure_then_retry_avoids_duplicate_write(self) -> None:
        self.finish_native_result()
        _, helper, binding, _, calls_path, env = self.workflow_fixture(fail_next_read=True)
        with patch.dict(os.environ, env), patch.object(Path, "home", return_value=self.root):
            first = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )
        self.assertEqual(first.status, "unknown")
        with patch.dict(os.environ, env), patch.object(Path, "home", return_value=self.root):
            recovered = EVIDENCE["export_run"](
                self.handle, binding, helper=helper, interface="workflow",
            )
        self.assertEqual(recovered.status, "exported")
        operations = [item["operation"] for item in self.workflow_calls(calls_path)]
        self.assertEqual(operations.count("artifact-write"), 1)
        self.assertEqual(operations.count("artifact-read"), 2)

    def test_workflow_artifact_read_accepts_maximum_and_rejects_one_over(self) -> None:
        _, _, binding, _, _, _ = self.workflow_fixture()
        _, args = EVIDENCE["_binding"](binding, "workflow")
        helper = self.root / "unused-helper"
        content = b"x" * EVIDENCE["MAX_ARTIFACT_BYTES"]
        digest = hashlib.sha256(content).hexdigest()
        receipt = {
            "schema": "r2-artifact-response-v1", "operation": "artifact-read",
            "object_sha256": digest, "byte_size": len(content),
            "object_path": EVIDENCE["_attachment_path"](args, digest),
        }
        output = (
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
            + b"\n" + content
        )
        with patch.object(EVIDENCE_MODULE, "_call", return_value=(0, output)) as call:
            readback, reason = EVIDENCE["_read_attachment"](helper, args, digest)
        self.assertEqual(readback, content)
        self.assertEqual(reason, "")
        self.assertEqual(
            call.call_args.kwargs["max_output_bytes"],
            EVIDENCE["MAX_HELPER_OUTPUT_BYTES"] + EVIDENCE["MAX_ARTIFACT_BYTES"] + 1,
        )

        too_large = content + b"x"
        too_large_digest = hashlib.sha256(too_large).hexdigest()
        oversized = dict(
            receipt,
            object_sha256=too_large_digest,
            byte_size=len(too_large),
            object_path=EVIDENCE["_attachment_path"](args, too_large_digest),
        )
        output = (json.dumps(oversized, sort_keys=True, separators=(",", ":")).encode()
                  + b"\n" + too_large)
        with patch.object(EVIDENCE_MODULE, "_call", return_value=(0, output)):
            readback, reason = EVIDENCE["_read_attachment"](helper, args, too_large_digest)
        self.assertIsNone(readback)
        self.assertEqual(reason, "invalid_receipt")

    def test_workflow_artifact_read_rejects_malformed_receipts_and_content(self) -> None:
        _, _, binding, _, _, _ = self.workflow_fixture()
        _, args = EVIDENCE["_binding"](binding, "workflow")
        content = b"\x00binary\xff"
        digest = hashlib.sha256(content).hexdigest()
        receipt = {
            "schema": "r2-artifact-response-v1", "operation": "artifact-read",
            "object_sha256": digest, "byte_size": len(content),
            "object_path": EVIDENCE["_attachment_path"](args, digest),
        }
        encode = lambda item, body=content: (
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n" + body
        )
        invalid = (
            (encode({**receipt, "operation": "artifact-write"}), "receipt_mismatch"),
            (encode({**receipt, "object_sha256": "f" * 64}), "receipt_mismatch"),
            (encode({**receipt, "byte_size": len(content) + 1}), "readback_mismatch"),
            (encode({**receipt, "object_path": str(self.root / "wrong-object")}),
             "receipt_mismatch"),
            (encode(receipt, b"changed"), "readback_mismatch"),
            (b"not-json\n" + content, "invalid_receipt"),
            (b'{"schema":"r2-artifact-response-v1"}', "invalid_receipt"),
        )
        for output, expected_reason in invalid:
            with self.subTest(expected_reason=expected_reason, output_size=len(output)):
                readback, reason = EVIDENCE["_workflow_artifact_receipt"](
                    output, operation="artifact-read", args=args, digest=digest,
                    expected_size=None,
                )
                self.assertIsNone(readback)
                self.assertEqual(reason, expected_reason)

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
