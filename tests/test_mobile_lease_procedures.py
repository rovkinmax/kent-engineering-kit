from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "templates" / "project" / "emulator-resource-lock.sh"
FIXTURES = ROOT / "tests" / "fixtures" / "mobile-lease"
MISSING = object()
RESOURCE = "emulator-fixture"
FIXTURE_TOKEN = "synthetic-fixture-value"
CHECKPOINT_TOKEN = "synthetic-checkpoint-token"
SESSION_ONE = "11111111-1111-4111-8111-111111111111"
SESSION_TWO = "22222222-2222-4222-8222-222222222222"
NATIVE_TASK_ID = "task-b6672872-8aaf-4dc9-ba9c-dfab7393a5b4"
SCRIPT_CONTEXT_SHA256 = "a" * 64
FIXED_NOW = 1_800_000_000
RESOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SCRIPT_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def lease_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def parse_acquire_any_records(output: str) -> dict[str, str]:
    lines = output.splitlines()
    if len(lines) != 2:
        raise ValueError("acquire-any output must have exactly two records")
    parsed: dict[str, str] = {}
    for expected_key, line in zip(("resource", "token"), lines, strict=True):
        key, separator, value = line.partition("=")
        if not separator or key != expected_key or not value:
            raise ValueError("acquire-any output does not match its record contract")
        parsed[key] = value
    if not RESOURCE_PATTERN.fullmatch(parsed["resource"]):
        raise ValueError("acquire-any output contains an invalid resource")
    if not TOKEN_PATTERN.fullmatch(parsed["token"]):
        raise ValueError("acquire-any output contains an invalid token")
    return parsed


def parse_acquire_token(output: str) -> str:
    lines = output.splitlines()
    if len(lines) != 1 or not TOKEN_PATTERN.fullmatch(lines[0]):
        raise ValueError("acquire output must be exactly one bare token")
    return lines[0]


def bind_checkpoint(
    checkpoint: dict[str, object],
    *,
    native_task_id: str,
    task_short_id: str,
) -> dict[str, str]:
    if checkpoint.get("schema_version") != 1:
        raise ValueError("checkpoint schema is unsupported")
    if checkpoint.get("task_short_id") != task_short_id:
        raise ValueError("checkpoint Task short ID does not match native readback")
    stage_data = checkpoint.get("stage_data")
    if not isinstance(stage_data, dict):
        raise ValueError("checkpoint stage_data must be an object")
    for field, expected in (
        ("task_native_id", native_task_id),
        ("task_short_id", task_short_id),
    ):
        if field in stage_data and stage_data[field] != expected:
            raise ValueError(f"checkpoint {field} conflicts with native readback")

    owner_id = stage_data.get("lease_owner_id", task_short_id)
    if not isinstance(owner_id, str) or owner_id not in {
        task_short_id,
        native_task_id,
    }:
        raise ValueError("checkpoint lease owner is not the verified Task")
    resource = stage_data.get("lock_resource", "")
    token = stage_data.get("lock_token", "")
    if not isinstance(resource, str) or (
        resource and not RESOURCE_PATTERN.fullmatch(resource)
    ):
        raise ValueError("checkpoint resource is invalid")
    if not isinstance(token, str) or (
        token and not TOKEN_PATTERN.fullmatch(token)
    ):
        raise ValueError("checkpoint token is invalid")

    return {
        "task_native_id": native_task_id,
        "task_short_id": task_short_id,
        "lease_owner_id": str(owner_id),
        "lock_resource": resource,
        "lock_token": token,
    }


def parse_locked_status(output: str, *, expected_resource: str) -> dict[str, str] | None:
    lines = output.splitlines()
    if lines == ["unlocked"]:
        return None
    if not lines or lines[0] != "locked":
        raise ValueError("resource status has an unknown state")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        key, separator, value = line.strip().partition("=")
        if not separator:
            raise ValueError("resource status contains a malformed record")
        if key == "age_seconds":
            continue
        if key in fields:
            raise ValueError("resource status contains duplicate metadata")
        fields[key] = value
    if fields.get("resource") != expected_resource:
        raise ValueError("resource status does not identify the requested resource")
    if not fields.get("task_id") or fields.get("token") != "<redacted>":
        raise ValueError("resource status lacks safe owner metadata")
    return fields


class MobileLeaseRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.private_root = Path(
            os.environ.get(
                "KENT_MOBILE_LEASE_TEST_ROOT",
                str(ROOT / "build" / "kent-workflow" / "mobile-lease"),
            )
        ).resolve()
        self.private_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=self.private_root)
        self.temporary_root = Path(self.temporary.name)
        self.runtime_root = self.temporary_root / "locks"
        self.fake_bin = self.temporary_root / "bin"
        self.fake_bin.mkdir()
        self.write_executable(
            self.fake_bin / "date",
            "#!/bin/sh\n[ \"${1:-}\" = +%s ] || exit 64\necho "
            f"{FIXED_NOW}\n",
        )
        self.write_executable(
            self.fake_bin / "uuidgen",
            "#!/bin/sh\necho synthetic-generated-value\n",
        )
        self.write_executable(
            self.fake_bin / "adb",
            "#!/bin/sh\necho unexpected adb access >&2\nexit 70\n",
        )
        self.write_fake_kent()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def write_executable(path: Path, source: str) -> None:
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)

    def write_fake_kent(self) -> None:
        task_fixture = FIXTURES / "native-task-ken-6.json"
        session_fixture = self.temporary_root / "session.txt"
        session_fixture.write_text(f"{SESSION_ONE}\n", encoding="utf-8")
        self.resumed_session = self.temporary_root / "session-resumed.txt"
        self.resumed_session.write_text(f"{SESSION_TWO}\n", encoding="utf-8")
        self.write_executable(
            self.fake_bin / "kent",
            """#!/bin/sh
case "$*" in
  "task show KEN-6 --json") cat "$KENT_TEST_TASK_JSON" ;;
  "project "*) echo native-mobile-project ;;
  "task show KEN-6 --project native-mobile-project --json") cat "$KENT_TEST_IDENTITY_JSON" ;;
  "session-id") cat "$KENT_TEST_SESSION_FILE" ;;
  *) exit 64 ;;
esac
""",
        )
        self.fake_kent_environment = {
            "KENT_TEST_TASK_JSON": str(task_fixture),
            "KENT_TEST_SESSION_FILE": str(session_fixture),
        }

    def run_adapter(
        self,
        *arguments: str,
        task_id: str | None | object = "KEN-6",
        session_id: str | None | object = SESSION_ONE,
        backend: str = "auto",
        stdin_text: str | None = None,
        path_prefix: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        prefix = path_prefix or self.fake_bin
        environment = os.environ.copy()
        environment.update(
            {
                "KENT_RESOURCE_LOCK_DIR": str(self.runtime_root),
                "KENT_RESOURCE_LOCK_OWNER_PID": "424242",
                "KENT_RESOURCE_LOCK_BACKEND": backend,
                "PATH": f"{prefix}{os.pathsep}{os.environ['PATH']}",
            }
        )
        for key, value in (
            ("KENT_TASK_ID", task_id),
            ("KENT_SESSION_ID", session_id),
        ):
            if value is MISSING or value is None:
                environment.pop(key, None)
            else:
                environment[key] = str(value)
        return subprocess.run(
            [str(ADAPTER), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            input=stdin_text,
        )

    def script_request(
        self,
        *,
        task_id: str = NATIVE_TASK_ID,
        context_sha256: str = SCRIPT_CONTEXT_SHA256,
        pid: int | None = None,
        token: str | None = None,
        owner_sha256: str | None = None,
    ) -> str:
        fields = [
            "schema=mobile-script-lease-request-v1",
            f"task_id={task_id}",
            f"context_sha256={context_sha256}",
            f"pid={os.getpid() if pid is None else pid}",
        ]
        if token is not None or owner_sha256 is not None:
            fields.extend((f"token={token or ''}", f"owner_sha256={owner_sha256 or ''}"))
        return "\n".join(fields) + "\n"

    def acquire_script(self, resource: str = RESOURCE) -> dict[str, str]:
        result = self.run_adapter(
            "acquire-script",
            resource,
            "0",
            stdin_text=self.script_request(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(
            [line.split("=", 1)[0] for line in lines],
            ["schema", "resource", "token", "owner_sha256"],
        )
        return dict(line.split("=", 1) for line in lines)

    def assert_backend_lease_round_trip(self, backend: str) -> None:
        acquired = self.run_adapter(
            "acquire", RESOURCE, "0", "7200", backend=backend
        )
        self.assertEqual(acquired.returncode, 0)
        token = parse_acquire_token(acquired.stdout)
        resumed = self.run_adapter(
            "resume-owned", RESOURCE, session_id=SESSION_TWO, backend=backend
        )
        self.assertEqual(resumed.returncode, 0)
        self.assertTrue(parse_acquire_token(resumed.stdout) == token)
        foreign = self.run_adapter(
            "resume-owned", RESOURCE, task_id="KEN-7", backend=backend
        )
        self.assertNotEqual(foreign.returncode, 0)
        released = self.run_adapter("release", RESOURCE, token, backend=backend)
        self.assertEqual(released.returncode, 0)
        status = self.run_adapter("status", RESOURCE, backend=backend)
        self.assertEqual(status.returncode, 0)
        self.assertEqual(status.stdout.strip(), "unlocked")

    @unittest.skipUnless(
        platform.system() == "Linux" and shutil.which("flock"),
        "canonical flock backend is Linux-only",
    )
    def test_flock_backend_lease_round_trip(self) -> None:
        self.assert_backend_lease_round_trip("flock")

    @unittest.skipUnless(
        platform.system() == "Darwin" and shutil.which("lockf"),
        "canonical lockf backend is Darwin-only",
    )
    def test_lockf_backend_lease_round_trip(self) -> None:
        self.assert_backend_lease_round_trip("lockf")

    def test_noncanonical_or_mixed_path_backend_fails_closed(self) -> None:
        unsupported = "flock" if platform.system() == "Darwin" else "lockf"
        refused = self.run_adapter(
            "acquire", RESOURCE, "0", "7200", backend=unsupported
        )
        self.assertEqual(refused.returncode, 69)
        self.assertFalse(self.runtime_root.exists())

        backend = "lockf" if platform.system() == "Darwin" else "flock"
        shadow = self.temporary_root / "shadow"
        shadow.mkdir()
        self.write_executable(
            shadow / backend,
            "#!/bin/sh\necho shadow-backend-ran >&2\nexit 91\n",
        )
        mixed = self.run_adapter(
            "acquire", RESOURCE, "0", "7200", path_prefix=shadow
        )
        self.assertEqual(mixed.returncode, 69)
        self.assertIn("path_mismatch", mixed.stderr)
        self.assertNotIn("shadow-backend-ran", mixed.stderr)
        self.assertFalse(self.runtime_root.exists())

    def test_script_acquire_release_and_inspect_use_private_shared_record(self) -> None:
        result = self.acquire_script()
        self.assertEqual(result["schema"], "mobile-script-lease-result-v1")
        self.assertEqual(result["resource"], RESOURCE)
        self.assertTrue(SCRIPT_TOKEN_PATTERN.fullmatch(result["token"]))

        owner_file = self.lock_path() / "owner"
        owner_bytes = owner_file.read_bytes()
        self.assertEqual(
            hashlib.sha256(owner_bytes).hexdigest(),
            result["owner_sha256"],
        )
        fields = dict(
            line.split("=", 1)
            for line in owner_bytes.decode("ascii").splitlines()
        )
        self.assertEqual(
            tuple(fields),
            (
                "owner_schema",
                "owner_kind",
                "token",
                "resource",
                "task_id",
                "context_sha256",
                "pid",
                "process_platform",
                "boot_id",
                "process_start",
                "pgid",
                "cwd",
                "created_at",
            ),
        )
        self.assertEqual(fields["owner_schema"], "mobile-script-owner-v1")
        self.assertEqual(fields["owner_kind"], "script")
        self.assertEqual(fields["task_id"], NATIVE_TASK_ID)
        self.assertEqual(fields["context_sha256"], SCRIPT_CONTEXT_SHA256)
        self.assertEqual(fields["pid"], str(os.getpid()))
        self.assertNotIn("session_id", fields)

        guard = self.runtime_root / ".guards" / f"mobile-{RESOURCE}.guard"
        guard_inode = guard.stat().st_ino
        status = self.run_adapter("status", RESOURCE)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("token=<redacted>", status.stdout)
        self.assertNotIn(result["token"], status.stdout)

        inspected = self.run_adapter("inspect-script-recovery", RESOURCE)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(
            inspected.stdout.splitlines(),
            [
                "schema=mobile-script-recovery-v1",
                f"resource={RESOURCE}",
                "owner_kind=ScriptOwnerV1",
                f"owner_sha256={result['owner_sha256']}",
            ],
        )
        self.assertNotIn(result["token"], inspected.stdout)

        released = self.run_adapter(
            "release-script",
            RESOURCE,
            stdin_text=self.script_request(
                token=result["token"],
                owner_sha256=result["owner_sha256"],
            ),
        )
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(released.stdout, "")
        self.assertFalse(self.lock_path().exists())
        self.assertTrue(guard.is_file())
        self.assertEqual(guard.stat().st_ino, guard_inode)
        self.assertEqual(guard.stat().st_size, 0)

        absent = self.run_adapter("inspect-script-recovery", RESOURCE)
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertEqual(
            absent.stdout.splitlines(),
            [
                "schema=mobile-script-recovery-v1",
                f"resource={RESOURCE}",
                "owner_kind=Absent",
                "owner_sha256=none",
            ],
        )

    def test_script_inspection_does_not_create_any_runtime_state(self) -> None:
        inspected = self.run_adapter(
            "inspect-script-recovery",
            "emulator-inspect-absent",
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(
            inspected.stdout.splitlines(),
            [
                "schema=mobile-script-recovery-v1",
                "resource=emulator-inspect-absent",
                "owner_kind=Absent",
                "owner_sha256=none",
            ],
        )
        self.assertFalse(self.runtime_root.exists())

    def test_script_request_rejects_duplicate_unknown_reordered_and_oversized(self) -> None:
        valid = self.script_request()
        malformed = (
            valid.replace(
                "task_id=",
                "unknown=value\ntask_id=",
            ),
            valid.replace(
                f"task_id={NATIVE_TASK_ID}\n",
                f"task_id={NATIVE_TASK_ID}\ntask_id={NATIVE_TASK_ID}\n",
            ),
            "\n".join(reversed(valid.splitlines())) + "\n",
            valid + "unexpected=value\n",
            valid + ("x" * 1025),
            valid.replace("\n", "\r\n"),
            valid.replace("task_id=", "\x00task_id="),
        )
        for request in malformed:
            with self.subTest(request_size=len(request)):
                result = self.run_adapter(
                    "acquire-script",
                    RESOURCE,
                    "0",
                    stdin_text=request,
                )
                self.assertEqual(result.returncode, 64)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.runtime_root.exists())

        invalid_task = self.run_adapter(
            "acquire-script",
            RESOURCE,
            "0",
            stdin_text=self.script_request(task_id="KEN-6"),
        )
        self.assertEqual(invalid_task.returncode, 64)
        self.assertFalse(self.runtime_root.exists())

    def test_script_acquisition_blocks_valid_session_owner_without_rewrite(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        original = owner.read_bytes()
        acquired = self.run_adapter(
            "acquire-script",
            RESOURCE,
            "0",
            stdin_text=self.script_request(),
        )
        self.assertEqual(acquired.returncode, 75)
        self.assertEqual(owner.read_bytes(), original)

    def test_script_acquisition_preserves_unknown_legacy_owner(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        owner.write_bytes(
            owner.read_bytes().replace(
                f"session_id={SESSION_ONE}\n".encode("ascii"),
                b"session_id=unknown\n",
            )
        )
        original = owner.read_bytes()

        acquired = self.run_adapter(
            "acquire-script",
            RESOURCE,
            "0",
            stdin_text=self.script_request(),
        )
        self.assertEqual(acquired.returncode, 75)
        self.assertEqual(owner.read_bytes(), original)

        inspected = self.run_adapter("inspect-script-recovery", RESOURCE)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertIn("owner_kind=UnknownOwner\n", inspected.stdout)
        self.assertIn("owner_sha256=unknown\n", inspected.stdout)

    def test_session_operations_cannot_relabel_or_release_script_owner(self) -> None:
        result = self.acquire_script()
        owner = self.lock_path() / "owner"
        original = owner.read_bytes()

        for arguments in (
            ("acquire", RESOURCE, "0", "0"),
            ("resume", RESOURCE, result["token"]),
            ("resume-owned", RESOURCE),
            ("release", RESOURCE, result["token"]),
        ):
            with self.subTest(operation=arguments[0]):
                refused = self.run_adapter(*arguments)
                self.assertNotEqual(refused.returncode, 0)
                self.assertEqual(owner.read_bytes(), original)

        # A valid but old Script record remains Script-owned and is not TTL
        # reclaimed by the existing Session acquisition path.
        aged = original.replace(b"created_at=1800000000\n", b"created_at=1\n")
        owner.write_bytes(aged)
        (owner.parent / "created_at").write_text("1\n", encoding="ascii")
        owner.chmod(0o600)
        (owner.parent / "created_at").chmod(0o600)
        original = owner.read_bytes()
        refused = self.run_adapter("acquire", RESOURCE, "0", "0")
        self.assertEqual(refused.returncode, 75)
        self.assertEqual(owner.read_bytes(), original)

        digest = hashlib.sha256(original).hexdigest()
        released = self.run_adapter(
            "release-script",
            RESOURCE,
            stdin_text=self.script_request(
                token=result["token"],
                owner_sha256=digest,
            ),
        )
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_script_release_mismatches_preserve_owner_and_process_start(self) -> None:
        result = self.acquire_script()
        owner = self.lock_path() / "owner"
        original = owner.read_bytes()
        invalid_requests = (
            self.script_request(
                task_id="task-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                token=result["token"],
                owner_sha256=result["owner_sha256"],
            ),
            self.script_request(
                context_sha256="b" * 64,
                token=result["token"],
                owner_sha256=result["owner_sha256"],
            ),
            self.script_request(
                pid=os.getpid() + 1,
                token=result["token"],
                owner_sha256=result["owner_sha256"],
            ),
            self.script_request(
                token="B" * 43,
                owner_sha256=result["owner_sha256"],
            ),
            self.script_request(
                token=result["token"],
                owner_sha256="c" * 64,
            ),
        )
        for request in invalid_requests:
            with self.subTest(request=request.splitlines()[1:]):
                refused = self.run_adapter(
                    "release-script",
                    RESOURCE,
                    stdin_text=request,
                )
                self.assertEqual(refused.returncode, 75)
                self.assertEqual(owner.read_bytes(), original)

        changed = original.replace(
            f"process_start={dict(line.split('=', 1) for line in original.decode().splitlines())['process_start']}\n".encode(),
            b"process_start=1\n",
        )
        owner.write_bytes(changed)
        digest = hashlib.sha256(changed).hexdigest()
        refused_identity = self.run_adapter(
            "release-script",
            RESOURCE,
            stdin_text=self.script_request(
                token=result["token"],
                owner_sha256=digest,
            ),
        )
        self.assertEqual(refused_identity.returncode, 75)
        self.assertIn("process_identity_mismatch", refused_identity.stderr)
        self.assertEqual(owner.read_bytes(), changed)

    def test_script_acquire_any_selects_one_resource_and_reuses_shared_owner(self) -> None:
        first = self.acquire_script("emulator-script-first")
        request = self.script_request()
        selected = self.run_adapter(
            "acquire-script-any",
            "emulator-script-first",
            "emulator-script-second",
            "--",
            "0",
            stdin_text=request,
        )
        self.assertEqual(selected.returncode, 0, selected.stderr)
        fields = dict(line.split("=", 1) for line in selected.stdout.splitlines())
        self.assertEqual(fields["resource"], "emulator-script-second")
        self.assertTrue(SCRIPT_TOKEN_PATTERN.fullmatch(fields["token"]))
        self.assertTrue(self.lock_path("emulator-script-first").exists())
        self.assertTrue(self.lock_path("emulator-script-second").exists())

        for resource, lease in (
            ("emulator-script-first", first),
            ("emulator-script-second", fields),
        ):
            released = self.run_adapter(
                "release-script",
                resource,
                stdin_text=self.script_request(
                    token=lease["token"],
                    owner_sha256=lease["owner_sha256"],
                ),
            )
            self.assertEqual(released.returncode, 0, released.stderr)

    def test_script_crashed_owner_is_preserved_without_guessed_takeover(self) -> None:
        child_source = r"""
import os
import subprocess
import sys

adapter, runtime_root, fake_bin = sys.argv[1:]
environment = os.environ.copy()
environment["KENT_RESOURCE_LOCK_DIR"] = runtime_root
environment["PATH"] = fake_bin + os.pathsep + environment["PATH"]
context = "a" * 64
request = "\n".join((
    "schema=mobile-script-lease-request-v1",
    "task_id=task-b6672872-8aaf-4dc9-ba9c-dfab7393a5b4",
    "context_sha256=" + context,
    "pid=" + str(os.getpid()),
    "",
))
acquired = subprocess.run(
    [adapter, "acquire-script", "emulator-fixture", "0"],
    check=False,
    capture_output=True,
    text=True,
    env=environment,
    input=request,
)
print(os.getpid(), acquired.returncode)
sys.exit(acquired.returncode)
"""
        environment = os.environ.copy()
        environment["KENT_RESOURCE_LOCK_DIR"] = str(self.runtime_root)
        environment["PATH"] = (
            f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}"
        )
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                child_source,
                str(ADAPTER),
                str(self.runtime_root),
                str(self.fake_bin),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=15,
        )
        self.assertEqual(crashed.returncode, 0, crashed.stderr)
        crashed_pid, acquire_status = crashed.stdout.split()
        self.assertEqual(acquire_status, "0")

        owner = self.lock_path() / "owner"
        original = owner.read_bytes()
        fields = dict(
            line.split("=", 1)
            for line in original.decode("ascii").splitlines()
        )
        self.assertEqual(fields["pid"], crashed_pid)
        self.assertEqual(fields["owner_kind"], "script")
        original_hash = hashlib.sha256(original).hexdigest()
        dead_owner_release = self.run_adapter(
            "release-script",
            RESOURCE,
            stdin_text=self.script_request(
                pid=int(crashed_pid),
                token=fields["token"],
                owner_sha256=original_hash,
            ),
        )
        self.assertEqual(dead_owner_release.returncode, 75)
        self.assertEqual(owner.read_bytes(), original)

        session_attempt = self.run_adapter("acquire", RESOURCE, "0", "0")
        self.assertEqual(session_attempt.returncode, 75)
        script_attempt = self.run_adapter(
            "acquire-script",
            RESOURCE,
            "0",
            stdin_text=self.script_request(),
        )
        self.assertEqual(script_attempt.returncode, 75)
        self.assertEqual(owner.read_bytes(), original)

        inspected = self.run_adapter("inspect-script-recovery", RESOURCE)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertIn("owner_kind=ScriptOwnerV1\n", inspected.stdout)
        self.assertIn(f"owner_sha256={original_hash}\n", inspected.stdout)
        self.assertNotIn(fields["token"], inspected.stdout)

    @unittest.skipUnless(
        platform.system() == "Darwin",
        "Darwin guard creation contention regression",
    )
    def test_parallel_first_guard_initialization_survives_create_race(self) -> None:
        resource = "emulator-first-guard-create-race"
        worker_source = r"""
import json
import os
import subprocess
import sys

adapter, runtime_root, fake_bin, resource = sys.argv[1:]
if sys.stdin.readline().strip() != "start":
    sys.exit(64)
environment = os.environ.copy()
environment.update({
    "KENT_RESOURCE_LOCK_DIR": runtime_root,
    "KENT_RESOURCE_LOCK_OWNER_PID": "424242",
    "KENT_TASK_ID": "KEN-6",
    "KENT_SESSION_ID": "11111111-1111-4111-8111-111111111111",
    "PATH": fake_bin + os.pathsep + environment["PATH"],
})
acquired = subprocess.run(
    [adapter, "acquire", resource, "0", "7200"],
    check=False,
    capture_output=True,
    text=True,
    env=environment,
)
print(json.dumps({
    "returncode": acquired.returncode,
    "stdout": acquired.stdout,
    "stderr": acquired.stderr,
}), flush=True)
"""
        for attempt in range(20):
            with self.subTest(attempt=attempt):
                self.runtime_root = self.temporary_root / f"guard-race-{attempt}"
                environment = os.environ.copy()
                environment.update(
                    {
                        "KENT_RESOURCE_LOCK_DIR": str(self.runtime_root),
                        "KENT_RESOURCE_LOCK_OWNER_PID": "424242",
                        "KENT_TASK_ID": "KEN-6",
                        "KENT_SESSION_ID": SESSION_ONE,
                        "PATH": (
                            f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}"
                        ),
                    }
                )
                workers: list[subprocess.Popen[str]] = []
                try:
                    for _ in range(2):
                        workers.append(
                            subprocess.Popen(
                                [
                                    sys.executable,
                                    "-c",
                                    worker_source,
                                    str(ADAPTER),
                                    str(self.runtime_root),
                                    str(self.fake_bin),
                                    resource,
                                ],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                env=environment,
                            )
                        )
                    for worker in workers:
                        assert worker.stdin is not None
                        worker.stdin.write("start\n")
                        worker.stdin.close()
                        worker.stdin = None

                    results = []
                    for worker in workers:
                        stdout, stderr = worker.communicate(timeout=30)
                        self.assertEqual(worker.returncode, 0, stderr)
                        results.append(json.loads(stdout))

                    self.assertCountEqual(
                        (result["returncode"] for result in results),
                        (0, 75),
                        f"parallel first guard initialization: {results}",
                    )
                    self.assertFalse(
                        any(
                            "resource_lock_guard_identity_invalid"
                            in result["stderr"]
                            for result in results
                        ),
                        f"guard creation contention was misclassified: {results}",
                    )
                    winner = next(
                        result for result in results if result["returncode"] == 0
                    )
                    token = parse_acquire_token(winner["stdout"])
                    guard = (
                        self.runtime_root
                        / ".guards"
                        / f"mobile-{resource}.guard"
                    )
                    guard_inode = guard.stat().st_ino
                    released = self.run_adapter("release", resource, token)
                    self.assertEqual(released.returncode, 0, released.stderr)
                    self.assertEqual(guard.stat().st_ino, guard_inode)
                    self.assertEqual(
                        self.run_adapter("status", resource).stdout.strip(),
                        "unlocked",
                    )
                finally:
                    for worker in workers:
                        if worker.poll() is None:
                            worker.terminate()
                            worker.wait(timeout=5)
                        for stream in (
                            worker.stdin,
                            worker.stdout,
                            worker.stderr,
                        ):
                            if stream is not None:
                                stream.close()

    def test_session_and_script_processes_contend_on_one_shared_guard(self) -> None:
        resource = "emulator-mixed-process-contention"
        worker_source = r"""
import os
import subprocess
import sys

adapter, runtime_root, fake_bin, kind = sys.argv[1:]
resource = "emulator-mixed-process-contention"
if sys.stdin.readline().strip() != "start":
    sys.exit(64)
environment = os.environ.copy()
environment["KENT_RESOURCE_LOCK_DIR"] = runtime_root
environment["KENT_TASK_ID"] = "KEN-6"
environment["KENT_SESSION_ID"] = "11111111-1111-4111-8111-111111111111"
environment["PATH"] = fake_bin + os.pathsep + environment["PATH"]
context = "a" * 64
if kind == "session":
    acquired = subprocess.run(
        [adapter, "acquire", resource, "0", "7200"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
else:
    request = "\n".join((
        "schema=mobile-script-lease-request-v1",
        "task_id=task-b6672872-8aaf-4dc9-ba9c-dfab7393a5b4",
        "context_sha256=" + context,
        "pid=" + str(os.getpid()),
        "",
    ))
    acquired = subprocess.run(
        [adapter, "acquire-script", resource, "0"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        input=request,
    )
print(
    acquired.returncode,
    acquired.stderr.strip().replace("\n", " "),
    flush=True,
)
if acquired.returncode != 0:
    sys.exit(acquired.returncode)
if sys.stdin.readline().strip() != "release":
    sys.exit(64)
if kind == "session":
    token = acquired.stdout.strip()
    released = subprocess.run(
        [adapter, "release", resource, token],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
else:
    fields = dict(line.split("=", 1) for line in acquired.stdout.splitlines())
    request = "\n".join((
        "schema=mobile-script-lease-request-v1",
        "task_id=task-b6672872-8aaf-4dc9-ba9c-dfab7393a5b4",
        "context_sha256=" + context,
        "pid=" + str(os.getpid()),
        "token=" + fields["token"],
        "owner_sha256=" + fields["owner_sha256"],
        "",
    ))
    released = subprocess.run(
        [adapter, "release-script", resource],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        input=request,
    )
print(released.returncode, flush=True)
sys.exit(released.returncode)
"""
        environment = os.environ.copy()
        environment["KENT_RESOURCE_LOCK_DIR"] = str(self.runtime_root)
        environment["PATH"] = (
            f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}"
        )
        workers: dict[str, subprocess.Popen[str]] = {}
        statuses: dict[str, int] = {}
        errors: dict[str, str] = {}
        selector = selectors.DefaultSelector()
        try:
            for kind in ("session", "script"):
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        worker_source,
                        str(ADAPTER),
                        str(self.runtime_root),
                        str(self.fake_bin),
                        kind,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env=environment,
                )
                workers[kind] = worker
            for worker in workers.values():
                assert worker.stdin is not None
                worker.stdin.write("start\n")
                worker.stdin.flush()
            for kind, worker in workers.items():
                assert worker.stdout is not None
                selector.register(worker.stdout, selectors.EVENT_READ, kind)

            deadline = time.monotonic() + 30
            while len(statuses) < len(workers):
                ready = selector.select(max(0, deadline - time.monotonic()))
                if not ready:
                    self.fail("mixed lease contenders did not finish acquisition")
                for key, _ in ready:
                    line = key.fileobj.readline()
                    if not line:
                        self.fail("a mixed lease contender exited without a result")
                    status, _, error = line.rstrip("\n").partition(" ")
                    statuses[key.data] = int(status)
                    errors[key.data] = error
                    selector.unregister(key.fileobj)

            self.assertCountEqual(
                statuses.values(),
                (0, 75),
                f"contender statuses={statuses}, errors={errors}",
            )
            winner_kind = next(kind for kind, status in statuses.items() if status == 0)
            guard = (
                self.runtime_root
                / ".guards"
                / f"mobile-{resource}.guard"
            )
            guard_inode = guard.stat().st_ino

            winner = workers[winner_kind]
            assert winner.stdin is not None
            winner.stdin.write("release\n")
            winner.stdin.flush()
            for worker in workers.values():
                worker.wait(timeout=15)
            assert winner.stdout is not None
            self.assertEqual(winner.stdout.readline().strip(), "0")
            for worker in workers.values():
                assert worker.stderr is not None
                self.assertEqual(worker.stderr.read(), "")

            inspected = self.run_adapter(
                "inspect-script-recovery",
                resource,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertIn("owner_kind=Absent\n", inspected.stdout)
            self.assertEqual(guard.stat().st_ino, guard_inode)
            self.assertEqual(
                self.run_adapter("status", resource).stdout.strip(),
                "unlocked",
            )
        finally:
            selector.close()
            for worker in workers.values():
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=5)
                for stream in (worker.stdin, worker.stdout, worker.stderr):
                    if stream is not None:
                        stream.close()

    def test_partial_duplicate_and_unsafe_owners_are_preserved(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        original = owner.read_bytes()
        owner.write_bytes(
            original.replace(
                b"token=synthetic-fixture-value\n",
                b"token=synthetic-fixture-value\ntoken=duplicate-value\n",
            )
        )
        duplicate = owner.read_bytes()
        acquired = self.run_adapter("acquire", RESOURCE, "0", "0")
        self.assertEqual(acquired.returncode, 75)
        self.assertEqual(owner.read_bytes(), duplicate)
        script_acquired = self.run_adapter(
            "acquire-script",
            RESOURCE,
            "0",
            stdin_text=self.script_request(),
        )
        self.assertEqual(script_acquired.returncode, 75)
        self.assertEqual(owner.read_bytes(), duplicate)
        status = self.run_adapter("status", RESOURCE)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("owner_kind=UnknownOwner", status.stdout)
        self.assertNotIn("synthetic-fixture-value", status.stdout)

        owner.unlink()
        target = self.temporary_root / "unsafe-owner-target"
        target.write_bytes(original)
        owner.symlink_to(target)
        unsafe = self.run_adapter("resume-owned", RESOURCE)
        self.assertEqual(unsafe.returncode, 75)
        self.assertTrue(owner.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        script_unsafe = self.run_adapter(
            "acquire-script",
            RESOURCE,
            "0",
            stdin_text=self.script_request(),
        )
        self.assertEqual(script_unsafe.returncode, 75)
        self.assertTrue(owner.is_symlink())
        self.assertEqual(target.read_bytes(), original)

        owner.unlink()
        owner.write_bytes(original)
        (owner.parent / "created_at").unlink()
        partial = self.run_adapter("acquire", RESOURCE, "0", "0")
        self.assertEqual(partial.returncode, 75)
        self.assertEqual(owner.read_bytes(), original)
        self.assertFalse((owner.parent / "created_at").exists())
        script_partial = self.run_adapter(
            "acquire-script",
            RESOURCE,
            "0",
            stdin_text=self.script_request(),
        )
        self.assertEqual(script_partial.returncode, 75)
        self.assertEqual(owner.read_bytes(), original)
        self.assertFalse((owner.parent / "created_at").exists())

        owner.unlink()
        interrupted_temporary = owner.parent / ".tmp-000000000000000000000000"
        interrupted_temporary.write_bytes(b"created_at=1800000000\n")
        interrupted_temporary.chmod(0o600)
        interrupted = self.run_adapter(
            "acquire-script",
            RESOURCE,
            "0",
            stdin_text=self.script_request(),
        )
        self.assertEqual(interrupted.returncode, 75)
        self.assertEqual(
            [path.name for path in owner.parent.iterdir()],
            [interrupted_temporary.name],
        )
        self.assertEqual(
            interrupted_temporary.read_bytes(),
            b"created_at=1800000000\n",
        )
        inspect_interrupted = self.run_adapter(
            "inspect-script-recovery",
            RESOURCE,
        )
        self.assertEqual(inspect_interrupted.returncode, 0)
        self.assertIn("owner_kind=UnknownOwner\n", inspect_interrupted.stdout)
        self.assertIn("owner_sha256=unknown\n", inspect_interrupted.stdout)

    def test_seeded_runtime_is_private_under_permissive_umask(self) -> None:
        previous_umask = os.umask(0o022)
        try:
            owner = self.seed_lock(task_id="KEN-6")
        finally:
            os.umask(previous_umask)
        runtime_stat = self.runtime_root.stat()
        self.assertEqual(runtime_stat.st_mode & 0o7777, 0o700)
        self.assertEqual(runtime_stat.st_uid, os.getuid())
        self.assertEqual(owner.parent.stat().st_mode & 0o7777, 0o700)
        status = self.run_adapter("status", RESOURCE)
        self.assertEqual(status.returncode, 0, status.stderr)

    def lock_path(self, resource: str = RESOURCE) -> Path:
        return self.runtime_root / f"mobile-{resource}.lock"

    def seed_lock(
        self,
        *,
        task_id: str,
        resource: str = RESOURCE,
        token: str = FIXTURE_TOKEN,
        created_at: int | None = None,
    ) -> Path:
        self.runtime_root.mkdir(mode=0o700, exist_ok=True)
        directory = self.lock_path(resource)
        directory.mkdir(parents=True)
        directory.chmod(0o700)
        (directory / "owner").write_text(
            "\n".join(
                (
                    f"token={token}",
                    f"resource={resource}",
                    "pid=424242",
                    "cwd=/synthetic/mobile-lease",
                    f"created_at={FIXED_NOW if created_at is None else created_at}",
                    f"task_id={task_id}",
                    f"session_id={SESSION_ONE}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (directory / "owner").chmod(0o600)
        (directory / "created_at").write_text(
            f"{FIXED_NOW if created_at is None else created_at}\n",
            encoding="utf-8",
        )
        (directory / "created_at").chmod(0o600)
        return directory / "owner"

    def read_native_identity(
        self,
        *,
        session_file: Path | None = None,
    ) -> dict[str, str]:
        # Seed inherited identity independently of diagnostic CLI output.
        inherited_session = session_file.read_text().strip() if session_file else SESSION_ONE
        workspace = self.temporary_root / "identity-workspace"
        workspace.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        scripts = workspace / ".kent/scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        for source, name in (
            (ROOT / "templates/project/workflow-evidence-ledger", "workflow-evidence-ledger"),
            (ROOT / "workflowkit/runtime.py", "workflow_runtime_contracts.py"),
        ):
            shutil.copyfile(source, scripts / name)
        fixture = workspace / "native-identity.json"
        fixture.write_text(json.dumps({
            "summary": {"id": NATIVE_TASK_ID, "short_id": "KEN-6", "project_id": "native-mobile-project"},
            "project": {},
            "source_workspace": {
                "root_path": str(workspace.resolve()), "workspace_id": "native-mobile-primary", "is_primary": True,
            },
            "current_nodes": [{"node_id": "smoke", "session_id": SESSION_ONE},
                              {"node_id": "smoke", "session_id": SESSION_TWO}],
        }))
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
                **self.fake_kent_environment,
                "KENT_SESSION_ID": inherited_session,
                "KENT_TEST_IDENTITY_JSON": str(fixture),
            }
        )
        if session_file is not None:
            environment["KENT_TEST_SESSION_FILE"] = str(session_file)
        identity = subprocess.run(
            [sys.executable, str(scripts / "workflow-evidence-ledger"), "identity",
             "--task", "KEN-6", "--workspace", str(workspace.resolve())],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if identity.returncode != 0:
            raise ValueError("stubbed native identity readback failed")
        observed = json.loads(identity.stdout)
        native_task_id = observed["task_id"]
        task_short_id = observed["task_short_id"]
        session_id = observed["session_id"]
        if (
            native_task_id != NATIVE_TASK_ID
            or task_short_id != "KEN-6"
            or not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                session_id,
            )
        ):
            raise ValueError("stubbed native identity mapping is invalid")
        return {
            "task_native_id": native_task_id,
            "task_short_id": task_short_id,
            "session_id": session_id,
        }

    def find_owned_resources(
        self,
        *,
        eligible_resources: list[str],
        native_task_id: str,
        task_short_id: str,
    ) -> list[tuple[str, str]]:
        inventory = self.run_adapter("status")
        if inventory.returncode != 0:
            raise ValueError("resource inventory failed")
        owned: list[tuple[str, str]] = []
        eligible = set(eligible_resources)
        for entry in inventory.stdout.splitlines():
            name = Path(entry).name
            if not name.startswith("mobile-") or not name.endswith(".lock"):
                raise ValueError("resource inventory contains an invalid lock path")
            resource = name[len("mobile-") : -len(".lock")]
            if resource not in eligible:
                continue
            status = self.run_adapter("status", resource)
            if status.returncode != 0:
                raise ValueError("resource status readback failed")
            metadata = parse_locked_status(
                status.stdout,
                expected_resource=resource,
            )
            if metadata is None:
                continue
            if metadata["task_id"] in {task_short_id, native_task_id}:
                owned.append((resource, metadata["task_id"]))
        return owned

    def resume_unique_inventory_match(
        self,
        *,
        eligible_resources: list[str],
        native_task_id: str,
        task_short_id: str,
        session_id: str,
    ) -> dict[str, str] | None:
        matches = self.find_owned_resources(
            eligible_resources=eligible_resources,
            native_task_id=native_task_id,
            task_short_id=task_short_id,
        )
        if len(matches) > 1:
            raise ValueError("multiple same-Task leases make recovery ambiguous")
        if not matches:
            return None
        resource, owner_id = matches[0]
        resumed = self.run_adapter(
            "resume-owned",
            resource,
            task_id=owner_id,
            session_id=session_id,
        )
        if resumed.returncode != 0:
            raise ValueError("guarded recovery of the proven lease failed")
        return {
            "resource": resource,
            "lease_owner_id": owner_id,
            "lock_token": parse_acquire_token(resumed.stdout),
        }

    def test_acquire_rejects_missing_task_before_runtime_creation(self) -> None:
        result = self.run_adapter(
            "acquire", RESOURCE, "0", "7200", task_id=MISSING
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "acquire accepted a missing Task identity",
        )
        self.assertFalse(
            self.runtime_root.exists(),
            "missing identity created a runtime or guard directory",
        )

    def test_status_inventory_without_runtime_root_is_empty(self) -> None:
        result = self.run_adapter("status")
        self.assertEqual(
            result.returncode,
            0,
            "status inventory failed when the runtime root was absent",
        )
        self.assertEqual(result.stdout, "", "empty inventory emitted lock paths")
        self.assertFalse(
            self.runtime_root.exists(),
            "status inventory created a runtime or guard directory",
        )

    def test_acquire_rejects_missing_session_before_runtime_creation(self) -> None:
        result = self.run_adapter(
            "acquire", RESOURCE, "0", "7200", session_id=MISSING
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "acquire accepted a missing Session identity",
        )
        self.assertFalse(
            self.runtime_root.exists(),
            "missing identity created a runtime or guard directory",
        )

    def test_acquire_any_rejects_missing_task_before_runtime_creation(self) -> None:
        result = self.run_adapter(
            "acquire-any",
            RESOURCE,
            "--",
            "0",
            "7200",
            task_id=MISSING,
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "acquire-any accepted a missing Task identity",
        )
        self.assertFalse(
            self.runtime_root.exists(),
            "acquire-any created a runtime or guard directory without identity",
        )

    def test_acquire_rejects_unknown_identity_before_runtime_creation(self) -> None:
        result = self.run_adapter(
            "acquire",
            RESOURCE,
            "0",
            "7200",
            task_id="unknown",
            session_id="unknown",
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "acquire accepted unknown ownership metadata",
        )
        self.assertFalse(
            self.runtime_root.exists(),
            "unknown identity created a runtime or guard directory",
        )

    def test_acquire_rejects_malformed_task_before_runtime_creation(self) -> None:
        result = self.run_adapter(
            "acquire", RESOURCE, "0", "7200", task_id="task-1"
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "acquire accepted a malformed Task identity",
        )
        self.assertFalse(
            self.runtime_root.exists(),
            "malformed Task identity created a runtime or guard directory",
        )

    def test_acquire_rejects_malformed_session_before_runtime_creation(self) -> None:
        result = self.run_adapter(
            "acquire", RESOURCE, "0", "7200", session_id="session-1"
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "acquire accepted a malformed Session identity",
        )
        self.assertFalse(
            self.runtime_root.exists(),
            "malformed Session identity created a runtime or guard directory",
        )

    def test_internal_try_acquire_rejects_missing_identity(self) -> None:
        result = self.run_adapter(
            "__locked",
            "try-acquire",
            RESOURCE,
            "7200",
            task_id=MISSING,
            session_id=MISSING,
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "guarded try-acquire accepted missing identity",
        )
        self.assertFalse(
            self.runtime_root.exists(),
            "guarded try-acquire created runtime or lock directories without identity",
        )

    def test_acquire_rejects_unknown_caller_before_expired_lock_reclamation(
        self,
    ) -> None:
        owner = self.seed_lock(task_id="KEN-6", created_at=1)
        original = owner.read_text(encoding="utf-8")
        result = self.run_adapter(
            "acquire",
            RESOURCE,
            "0",
            "0",
            task_id="unknown",
            session_id=SESSION_TWO,
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "acquire reclaimed a stale lock with an unknown caller identity",
        )
        self.assertTrue(
            owner.read_text(encoding="utf-8") == original,
            "failed admission changed the stale lease metadata",
        )

    def test_resume_owned_rejects_mismatched_resource_metadata(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        owner.write_text(owner.read_text().replace(
            f"resource={RESOURCE}\n", "resource=emulator-other\n",
        ))
        original = owner.read_bytes()
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertNotEqual(
            result.returncode,
            0,
            "resume-owned accepted resource metadata for another resource",
        )
        self.assertEqual(owner.read_bytes(), original)

    def test_resume_owned_rejects_duplicate_token_metadata(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        original = owner.read_text(encoding="utf-8")
        owner.write_text(
            original.replace(
                f"token={FIXTURE_TOKEN}\n",
                f"token={FIXTURE_TOKEN}\ntoken=second-synthetic-value\n",
            ),
            encoding="utf-8",
        )
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertNotEqual(
            result.returncode,
            0,
            "resume-owned accepted duplicate token fields",
        )

    def test_resume_owned_rejects_duplicate_task_metadata(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        original = owner.read_text(encoding="utf-8")
        owner.write_text(
            original.replace(
                "task_id=KEN-6\n",
                "task_id=KEN-6\ntask_id=KEN-6\n",
            ),
            encoding="utf-8",
        )
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertNotEqual(
            result.returncode,
            0,
            "resume-owned accepted duplicate Task fields",
        )

    def test_resume_owned_rejects_malformed_task_metadata(self) -> None:
        owner = self.seed_lock(task_id="task-1")
        original = owner.read_text(encoding="utf-8")
        result = self.run_adapter(
            "resume-owned",
            RESOURCE,
            task_id="KEN-6",
            session_id=SESSION_TWO,
        )
        self.assertEqual(
            result.returncode,
            75,
            "resume-owned did not reject malformed Task metadata",
        )
        self.assertTrue(
            "resource_lock_owner_metadata_invalid" in result.stderr,
            "malformed Task metadata was not rejected by owner validation",
        )
        self.assertTrue(
            owner.read_text(encoding="utf-8") == original,
            "failed recovery changed malformed ownership metadata",
        )

    def test_resume_owned_rejects_duplicate_resource_metadata(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        owner.write_text(owner.read_text().replace(
            f"resource={RESOURCE}\n",
            f"resource={RESOURCE}\nresource=emulator-other\n",
        ))
        original = owner.read_bytes()
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertNotEqual(
            result.returncode,
            0,
            "resume-owned accepted duplicate resource fields",
        )
        self.assertEqual(owner.read_bytes(), original)

    def test_token_resume_does_not_rewrite_foreign_task_owner(self) -> None:
        owner = self.seed_lock(task_id="FOREIGN-9")
        result = self.run_adapter(
            "resume", RESOURCE, FIXTURE_TOKEN, session_id=SESSION_TWO
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "token resume overwrote an existing foreign Task owner",
        )
        self.assertTrue(
            "task_id=FOREIGN-9" in owner.read_text(encoding="utf-8"),
            "failed token resume changed foreign ownership metadata",
        )

    def test_token_resume_does_not_rewrite_unknown_task_owner(self) -> None:
        owner = self.seed_lock(task_id="unknown")
        result = self.run_adapter(
            "resume", RESOURCE, FIXTURE_TOKEN, session_id=SESSION_TWO
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "token resume converted unknown ownership to the current Task",
        )
        self.assertTrue(
            "task_id=unknown" in owner.read_text(encoding="utf-8"),
            "failed token resume changed unknown ownership metadata",
        )

    def test_resume_owned_rejects_missing_session_without_rewrite(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        original = owner.read_text(encoding="utf-8")
        result = self.run_adapter(
            "resume-owned", RESOURCE, session_id=MISSING
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "resume-owned accepted a missing current Session",
        )
        self.assertTrue(
            owner.read_text(encoding="utf-8") == original,
            "failed recovery changed owner metadata",
        )

    def test_resume_owned_preserves_unknown_historical_session(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        contents = owner.read_text(encoding="utf-8").replace(
            f"session_id={SESSION_ONE}\n",
            "session_id=unknown\n",
        )
        owner.write_text(contents, encoding="utf-8")
        original = owner.read_bytes()
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertEqual(result.returncode, 75)
        self.assertEqual(
            owner.read_bytes(),
            original,
            "unknown historical Session metadata was rewritten",
        )

    def test_token_resume_rejects_missing_task_before_creating_lock(self) -> None:
        result = self.run_adapter(
            "resume", RESOURCE, FIXTURE_TOKEN, task_id=MISSING
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "token resume created an unbound lease without a Task identity",
        )
        self.assertFalse(
            self.lock_path().exists(),
            "token resume created a lock before validating identity",
        )

    def test_exact_ttl_boundary_stays_busy(self) -> None:
        owner = self.seed_lock(task_id="KEN-6", created_at=FIXED_NOW - 60)
        original = owner.read_text(encoding="utf-8")
        result = self.run_adapter(
            "acquire", RESOURCE, "0", "60", session_id=SESSION_TWO
        )
        self.assertEqual(
            result.returncode,
            75,
            "lease at the exact caller TTL boundary was replaced",
        )
        self.assertTrue(
            owner.read_text(encoding="utf-8") == original,
            "busy acquisition changed the existing lease",
        )

    def test_resume_first_refreshes_expired_same_task_lease(self) -> None:
        owner = self.seed_lock(task_id="KEN-6", created_at=FIXED_NOW - 61)
        resumed = self.run_adapter(
            "resume-owned", RESOURCE, session_id=SESSION_TWO
        )
        self.assertEqual(
            resumed.returncode,
            0,
            "exact-owner resume failed before replacement",
        )
        self.assertTrue(
            resumed.stdout.strip() == FIXTURE_TOKEN,
            "resume-owned did not retain the existing token",
        )
        self.assertEqual(
            (owner.parent / "created_at").read_text(encoding="utf-8").strip(),
            str(FIXED_NOW),
            "resume-first did not refresh the lease timestamp",
        )
        contender = self.run_adapter(
            "acquire", RESOURCE, "0", "0", session_id=SESSION_TWO
        )
        self.assertEqual(
            contender.returncode,
            75,
            "acquisition replaced the lease refreshed by resume-first",
        )
        released = self.run_adapter(
            "release", RESOURCE, resumed.stdout.strip(), session_id=SESSION_TWO
        )
        self.assertEqual(released.returncode, 0, "resumed lease release failed")

    def test_replacement_first_rejects_stale_resume_and_release_tokens(self) -> None:
        owner = self.seed_lock(task_id="KEN-6", created_at=FIXED_NOW - 61)
        original_token = FIXTURE_TOKEN
        replacement = self.run_adapter(
            "acquire", RESOURCE, "0", "60", session_id=SESSION_TWO
        )
        self.assertEqual(
            replacement.returncode,
            0,
            "expired lease was not replaced after caller TTL",
        )
        self.assertTrue(
            replacement.stdout.strip()
            and replacement.stdout.strip() != original_token,
            "replacement did not produce a distinct token",
        )
        stale_resume = self.run_adapter(
            "resume",
            RESOURCE,
            original_token,
            session_id=SESSION_TWO,
        )
        self.assertNotEqual(
            stale_resume.returncode,
            0,
            "stale resume token recovered a completed replacement",
        )
        stale_release = self.run_adapter(
            "release",
            RESOURCE,
            original_token,
            session_id=SESSION_TWO,
        )
        self.assertNotEqual(
            stale_release.returncode,
            0,
            "stale release token removed a completed replacement",
        )
        status = self.run_adapter("status", RESOURCE, session_id=SESSION_TWO)
        self.assertEqual(
            status.stdout.splitlines()[0],
            "locked",
            "stale token operation changed the replacement lock state",
        )
        self.assertTrue(
            "task_id=KEN-6" in owner.read_text(encoding="utf-8"),
            "stale token operation changed the replacement owner",
        )
        released = self.run_adapter(
            "release",
            RESOURCE,
            replacement.stdout.strip(),
            session_id=SESSION_TWO,
        )
        self.assertEqual(released.returncode, 0, "replacement release failed")

    def test_unknown_owner_expiry_is_blocked_and_preserved(self) -> None:
        owner = self.seed_lock(
            task_id="unknown",
            created_at=1,
        )
        original = owner.read_bytes()
        result = self.run_adapter("acquire", RESOURCE, "0", "0")
        self.assertEqual(
            result.returncode,
            75,
            "unknown historical owner was reclaimed by caller TTL",
        )
        self.assertEqual(
            owner.read_bytes(),
            original,
            "blocked unknown-owner expiry changed the original record",
        )

    def test_returned_token_does_not_prove_unknown_task_ownership(self) -> None:
        acquired = self.run_adapter("acquire", RESOURCE, "0", "7200")
        self.assertEqual(
            acquired.returncode,
            0,
            "synthetic acquisition failed before historical recovery",
        )
        self.assertTrue(acquired.stdout.strip(), "synthetic acquisition emitted no token")
        owner = self.lock_path() / "owner"
        owner_text = owner.read_text(encoding="utf-8").replace(
            "task_id=KEN-6\n",
            "task_id=unknown\n",
        )
        owner.write_text(owner_text, encoding="utf-8")
        original = owner.read_text(encoding="utf-8")
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertNotEqual(
            result.returncode,
            0,
            "resume-owned treated an acquisition result as Task ownership proof",
        )
        self.assertTrue(
            owner.read_text(encoding="utf-8") == original,
            "blocked historical recovery changed the unknown-owner fixture",
        )

    def test_bare_acquire_output_is_not_acquire_any_records(self) -> None:
        acquired = self.run_adapter("acquire", RESOURCE, "0", "7200")
        self.assertEqual(
            acquired.returncode,
            0,
            "synthetic acquisition failed before output characterization",
        )
        parse_acquire_token(acquired.stdout)
        with self.assertRaises(ValueError):
            parse_acquire_any_records(acquired.stdout)
        released = self.run_adapter("release", RESOURCE, acquired.stdout.strip())
        self.assertEqual(
            released.returncode,
            0,
            "synthetic bare-token lease could not be released",
        )

    def test_discarded_stdout_can_be_recovered_before_checkpoint(self) -> None:
        acquired = self.run_adapter("acquire", RESOURCE, "0", "7200")
        self.assertEqual(
            acquired.returncode,
            0,
            "synthetic acquisition failed before stdout could be discarded",
        )
        self.assertTrue(acquired.stdout.strip(), "synthetic acquisition emitted no token")
        owner_file = self.lock_path() / "owner"
        owner_token = next(
            line.removeprefix("token=")
            for line in owner_file.read_text(encoding="utf-8").splitlines()
            if line.startswith("token=")
        )
        acquired.stdout = ""
        recovered = self.run_adapter(
            "resume-owned", RESOURCE, session_id=SESSION_TWO
        )
        self.assertEqual(
            recovered.returncode,
            0,
            "same-Task recovery failed after simulated lost stdout",
        )
        self.assertTrue(
            recovered.stdout.strip() == owner_token,
            "recovered token did not match the discarded acquisition output",
        )
        released = self.run_adapter(
            "release", RESOURCE, recovered.stdout.strip(), session_id=SESSION_TWO
        )
        self.assertEqual(
            released.returncode,
            0,
            "synthetic recovered lease could not be released",
        )

    def test_public_identity_ignores_diagnostic_output_and_requires_inherited_uuid(self):
        diagnostic = self.temporary_root / "session-diagnostic.txt"
        diagnostic.write_text(f"Short ID: 123\nUUID: {SESSION_ONE}\nLog path: /fixture/events.jsonl\n")
        # Deliberately poison diagnostic output; identity never invokes it.
        self.fake_kent_environment["KENT_TEST_SESSION_FILE"] = str(diagnostic)
        self.assertEqual(self.read_native_identity()["session_id"], SESSION_ONE)
        for output in ("123\n", "UUID: 123\n",
                       f"Short ID: 123\nUUID: {SESSION_ONE}\nLog path: /fixture\nUUID: {SESSION_TWO}\n"):
            diagnostic.write_text(output)
            with self.assertRaises(ValueError):
                self.read_native_identity(session_file=diagnostic)

    def test_native_task_uuid_is_accepted_as_verified_identity_input(self) -> None:
        task = json.loads(
            (FIXTURES / "native-task-ken-6.json").read_text(encoding="utf-8")
        )["summary"]
        session_id = SESSION_ONE
        native_task_id = task["id"]
        acquired = self.run_adapter(
            "acquire",
            RESOURCE,
            "0",
            "7200",
            task_id=native_task_id,
            session_id=session_id,
        )
        self.assertEqual(
            acquired.returncode,
            0,
            "syntactically valid native Task UUID was rejected",
        )
        owner = self.lock_path() / "owner"
        self.assertTrue(
            f"task_id={native_task_id}" in owner.read_text(encoding="utf-8"),
            "native Task identity was not retained in owner metadata",
        )
        released = self.run_adapter(
            "release",
            RESOURCE,
            acquired.stdout.strip(),
            task_id=native_task_id,
            session_id=session_id,
        )
        self.assertEqual(
            released.returncode,
            0,
            "synthetic native-ID lease could not be released",
        )

    def test_acquire_output_contract_fixtures_parse_separately(self) -> None:
        contract = lease_fixture("acquire-output-contract.json")
        bare_token = parse_acquire_token(str(contract["acquire"]))
        selection = parse_acquire_any_records(str(contract["acquire_any"]))
        self.assertEqual(bare_token, "synthetic-bare-token")
        self.assertEqual(
            selection,
            {
                "resource": RESOURCE,
                "token": "synthetic-selection-token",
            },
        )
        with self.assertRaises(ValueError):
            parse_acquire_token(str(contract["acquire_any"]))
        with self.assertRaises(ValueError):
            parse_acquire_any_records(str(contract["acquire"]))
        with self.assertRaises(ValueError):
            parse_acquire_any_records(
                "resource=emulator-a\nresource=emulator-b\n"
            )

        acquired = self.run_adapter("acquire", RESOURCE, "0", "7200")
        self.assertEqual(acquired.returncode, 0)
        token = parse_acquire_token(acquired.stdout)
        selection_result = self.run_adapter(
            "acquire-any",
            "emulator-selected",
            "emulator-unused",
            "--",
            "0",
            "7200",
        )
        self.assertEqual(selection_result.returncode, 0)
        parsed_selection = parse_acquire_any_records(selection_result.stdout)
        self.assertEqual(parsed_selection["resource"], "emulator-selected")
        self.assertEqual(
            self.run_adapter("release", RESOURCE, token).returncode,
            0,
        )
        self.assertEqual(
            self.run_adapter(
                "release",
                parsed_selection["resource"],
                parsed_selection["token"],
            ).returncode,
            0,
        )

    def test_retained_checkpoint_token_resumes_same_lease_across_sessions(self) -> None:
        checkpoint = lease_fixture("checkpoint-retained.json")
        identity = self.read_native_identity(
            session_file=self.resumed_session
        )
        binding = bind_checkpoint(
            checkpoint,
            native_task_id=identity["task_native_id"],
            task_short_id=identity["task_short_id"],
        )
        owner = self.seed_lock(
            task_id=binding["lease_owner_id"],
            resource=binding["lock_resource"],
            token=binding["lock_token"],
        )
        resumed = self.run_adapter(
            "resume",
            binding["lock_resource"],
            binding["lock_token"],
            task_id=binding["lease_owner_id"],
            session_id=identity["session_id"],
        )
        self.assertEqual(resumed.returncode, 0)
        self.assertEqual(parse_acquire_token(resumed.stdout), CHECKPOINT_TOKEN)
        owner_text = owner.read_text(encoding="utf-8")
        self.assertIn(f"task_id={identity['task_short_id']}", owner_text)
        self.assertIn(f"session_id={identity['session_id']}", owner_text)
        self.assertEqual(
            self.run_adapter(
                "release",
                binding["lock_resource"],
                binding["lock_token"],
                task_id=binding["lease_owner_id"],
                session_id=identity["session_id"],
            ).returncode,
            0,
        )

    def test_partial_checkpoint_enriches_native_mapping_then_recovers(self) -> None:
        checkpoint = lease_fixture("checkpoint-partial.json")
        identity = self.read_native_identity(
            session_file=self.resumed_session
        )
        binding = bind_checkpoint(
            checkpoint,
            native_task_id=identity["task_native_id"],
            task_short_id=identity["task_short_id"],
        )
        self.assertEqual(binding["task_native_id"], NATIVE_TASK_ID)
        self.assertEqual(binding["task_short_id"], "KEN-6")
        self.assertEqual(binding["lease_owner_id"], "KEN-6")
        self.assertEqual(binding["lock_resource"], RESOURCE)
        self.assertEqual(binding["lock_token"], "")
        owner = self.seed_lock(
            task_id=binding["lease_owner_id"],
            resource=binding["lock_resource"],
        )
        resumed = self.run_adapter(
            "resume-owned",
            binding["lock_resource"],
            task_id=binding["lease_owner_id"],
            session_id=identity["session_id"],
        )
        self.assertEqual(resumed.returncode, 0)
        recovered_token = parse_acquire_token(resumed.stdout)
        binding["lock_token"] = recovered_token
        self.assertEqual(recovered_token, FIXTURE_TOKEN)
        self.assertIn(f"task_id={identity['task_short_id']}", owner.read_text())
        self.assertIn(f"session_id={identity['session_id']}", owner.read_text())
        self.assertEqual(
            self.run_adapter(
                "release",
                binding["lock_resource"],
                binding["lock_token"],
                task_id=binding["lease_owner_id"],
                session_id=identity["session_id"],
            ).returncode,
            0,
        )

    def test_legacy_native_id_owner_is_recovered_only_after_mapping(self) -> None:
        checkpoint = lease_fixture("checkpoint-legacy-native.json")
        identity = self.read_native_identity(
            session_file=self.resumed_session
        )
        binding = bind_checkpoint(
            checkpoint,
            native_task_id=identity["task_native_id"],
            task_short_id=identity["task_short_id"],
        )
        self.assertEqual(binding["lease_owner_id"], NATIVE_TASK_ID)
        owner = self.seed_lock(
            task_id=binding["lease_owner_id"],
            resource=binding["lock_resource"],
        )
        resumed = self.run_adapter(
            "resume-owned",
            binding["lock_resource"],
            task_id=binding["lease_owner_id"],
            session_id=identity["session_id"],
        )
        self.assertEqual(resumed.returncode, 0)
        token = parse_acquire_token(resumed.stdout)
        owner_text = owner.read_text(encoding="utf-8")
        self.assertIn(f"task_id={NATIVE_TASK_ID}", owner_text)
        self.assertIn(f"session_id={identity['session_id']}", owner_text)
        self.assertEqual(
            self.run_adapter(
                "release",
                binding["lock_resource"],
                token,
                task_id=binding["lease_owner_id"],
                session_id=identity["session_id"],
            ).returncode,
            0,
        )

    def test_conflicting_and_foreign_checkpoints_block_without_mutation(self) -> None:
        identity = self.read_native_identity(
            session_file=self.resumed_session
        )
        for fixture_name, owner_id in (
            ("checkpoint-conflicting.json", "KEN-6"),
            ("checkpoint-foreign.json", "PUB-70"),
        ):
            with self.subTest(fixture=fixture_name):
                checkpoint = lease_fixture(fixture_name)
                stage_data = checkpoint["stage_data"]
                assert isinstance(stage_data, dict)
                owner = self.seed_lock(
                    task_id=owner_id,
                    resource=str(stage_data["lock_resource"]),
                    token="synthetic-foreign-checkpoint-token",
                )
                original = owner.read_text(encoding="utf-8")
                with self.assertRaises(ValueError):
                    bind_checkpoint(
                        checkpoint,
                        native_task_id=identity["task_native_id"],
                        task_short_id=identity["task_short_id"],
                    )
                self.assertEqual(
                    owner.read_text(encoding="utf-8"),
                    original,
                    "rejected checkpoint altered an existing lease",
                )

    def test_no_resource_inventory_recovers_one_proven_same_task_lease(self) -> None:
        candidates = lease_fixture("eligible-resources.json")[
            "eligible_resources"
        ]
        identity = self.read_native_identity(
            session_file=self.resumed_session
        )
        self.seed_lock(
            task_id=identity["task_short_id"],
            resource="emulator-secondary",
        )
        recovered = self.resume_unique_inventory_match(
            eligible_resources=list(candidates),
            native_task_id=identity["task_native_id"],
            task_short_id=identity["task_short_id"],
            session_id=identity["session_id"],
        )
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered["resource"], "emulator-secondary")
        self.assertEqual(recovered["lease_owner_id"], "KEN-6")
        self.assertEqual(recovered["lock_token"], FIXTURE_TOKEN)
        owner = self.lock_path("emulator-secondary") / "owner"
        self.assertIn(f"session_id={identity['session_id']}", owner.read_text())
        self.assertEqual(
            self.run_adapter(
                "release",
                recovered["resource"],
                recovered["lock_token"],
                task_id=recovered["lease_owner_id"],
                session_id=identity["session_id"],
            ).returncode,
            0,
        )

    def test_no_resource_inventory_blocks_multiple_same_task_leases(self) -> None:
        candidates = lease_fixture("eligible-resources.json")[
            "eligible_resources"
        ]
        identity = self.read_native_identity()
        owner_files = [
            self.seed_lock(task_id="KEN-6", resource=resource)
            for resource in candidates
        ]
        originals = [
            owner.read_text(encoding="utf-8") for owner in owner_files
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.resume_unique_inventory_match(
                eligible_resources=list(candidates),
                native_task_id=identity["task_native_id"],
                task_short_id=identity["task_short_id"],
                session_id=identity["session_id"],
            )
        self.assertEqual(
            [owner.read_text(encoding="utf-8") for owner in owner_files],
            originals,
            "ambiguous candidates were mutated during recovery",
        )

    def test_foreign_inventory_candidate_is_not_adopted(self) -> None:
        candidates = lease_fixture("eligible-resources.json")[
            "eligible_resources"
        ]
        identity = self.read_native_identity(
            session_file=self.resumed_session
        )
        foreign_owner = self.seed_lock(
            task_id="PUB-70",
            resource="emulator-fixture",
            token="synthetic-foreign-checkpoint-token",
        )
        original = foreign_owner.read_text(encoding="utf-8")
        recovered = self.resume_unique_inventory_match(
            eligible_resources=list(candidates),
            native_task_id=identity["task_native_id"],
            task_short_id=identity["task_short_id"],
            session_id=identity["session_id"],
        )
        self.assertIsNone(
            recovered,
            "sole foreign occupancy was incorrectly treated as ownership",
        )
        acquired = self.run_adapter(
            "acquire-any",
            *candidates,
            "--",
            "0",
            "7200",
            task_id=identity["task_short_id"],
            session_id=identity["session_id"],
        )
        self.assertEqual(acquired.returncode, 0)
        selection = parse_acquire_any_records(acquired.stdout)
        self.assertEqual(selection["resource"], "emulator-secondary")
        self.assertEqual(foreign_owner.read_text(encoding="utf-8"), original)
        self.assertEqual(
            self.run_adapter(
                "release",
                selection["resource"],
                selection["token"],
                task_id=identity["task_short_id"],
                session_id=identity["session_id"],
            ).returncode,
            0,
        )


if __name__ == "__main__":
    unittest.main()
