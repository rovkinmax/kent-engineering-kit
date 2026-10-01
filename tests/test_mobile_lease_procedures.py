from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
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
FIXED_NOW = 1_800_000_000
RESOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def incident_fixture(name: str) -> dict[str, object]:
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
        self.private_root = ROOT / "build" / "kent-workflow" / "KEN-6"
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
        session_fixture = FIXTURES / "native-session.txt"
        self.write_executable(
            self.fake_bin / "kent",
            """#!/bin/sh
case "$*" in
  "task show KEN-6 --json") cat "$KENT_TEST_TASK_JSON" ;;
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
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "KENT_RESOURCE_LOCK_DIR": str(self.runtime_root),
                "KENT_RESOURCE_LOCK_OWNER_PID": "424242",
                "KENT_RESOURCE_LOCK_BACKEND": backend,
                "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
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
        )

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

    @unittest.skipUnless(shutil.which("flock"), "flock backend unavailable")
    def test_flock_backend_lease_round_trip(self) -> None:
        self.assert_backend_lease_round_trip("flock")

    @unittest.skipUnless(shutil.which("lockf"), "lockf backend unavailable")
    def test_lockf_backend_lease_round_trip(self) -> None:
        self.assert_backend_lease_round_trip("lockf")

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
        directory = self.lock_path(resource)
        directory.mkdir(parents=True)
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
        (directory / "created_at").write_text(
            f"{FIXED_NOW if created_at is None else created_at}\n",
            encoding="utf-8",
        )
        return directory / "owner"

    def read_native_identity(
        self,
        *,
        session_file: Path | None = None,
    ) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
                **self.fake_kent_environment,
            }
        )
        if session_file is not None:
            environment["KENT_TEST_SESSION_FILE"] = str(session_file)
        task = subprocess.run(
            ["kent", "task", "show", "KEN-6", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        session = subprocess.run(
            ["kent", "session-id"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if task.returncode != 0 or session.returncode != 0:
            raise ValueError("stubbed native identity readback failed")
        summary = json.loads(task.stdout)["summary"]
        native_task_id = summary["id"]
        task_short_id = summary["short_id"]
        session_id = session.stdout.strip()
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
        target = self.lock_path()
        target.mkdir(parents=True)
        (target / "owner").write_text(
            "\n".join(
                (
                    f"token={FIXTURE_TOKEN}",
                    "resource=emulator-other",
                    "task_id=KEN-6",
                    f"session_id={SESSION_ONE}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (target / "created_at").write_text(f"{FIXED_NOW}\n")
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertNotEqual(
            result.returncode,
            0,
            "resume-owned accepted resource metadata for another resource",
        )
        self.assertTrue(
            "resource=emulator-other"
            in (target / "owner").read_text(encoding="utf-8"),
            "failed recovery rewrote the resource metadata",
        )

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
        target = self.lock_path()
        target.mkdir(parents=True)
        (target / "owner").write_text(
            "\n".join(
                (
                    f"token={FIXTURE_TOKEN}",
                    f"resource={RESOURCE}",
                    "resource=emulator-other",
                    "task_id=KEN-6",
                    f"session_id={SESSION_ONE}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (target / "created_at").write_text(f"{FIXED_NOW}\n")
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertNotEqual(
            result.returncode,
            0,
            "resume-owned accepted duplicate resource fields",
        )

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

    def test_resume_owned_accepts_unknown_historical_session(self) -> None:
        owner = self.seed_lock(task_id="KEN-6")
        contents = owner.read_text(encoding="utf-8").replace(
            f"session_id={SESSION_ONE}\n",
            "session_id=unknown\n",
        )
        owner.write_text(contents, encoding="utf-8")
        result = self.run_adapter("resume-owned", RESOURCE, session_id=SESSION_TWO)
        self.assertEqual(
            result.returncode,
            0,
            "unknown historical Session metadata blocked proven Task recovery",
        )
        self.assertTrue(
            result.stdout.strip() == FIXTURE_TOKEN,
            "recovery did not preserve the same-Task lease token",
        )
        self.assertTrue(
            f"session_id={SESSION_TWO}"
            in owner.read_text(encoding="utf-8"),
            "successful recovery did not record the current Session",
        )
        released = self.run_adapter(
            "release", RESOURCE, result.stdout.strip(), session_id=SESSION_TWO
        )
        self.assertEqual(
            released.returncode,
            0,
            "synthetic recovered lease could not be released",
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

    def test_osm_78_unknown_owner_expiry_sequence_is_preserved(self) -> None:
        fixture = incident_fixture("osm-78.json")
        self.assertFalse(fixture["checkpoint_token_present"])
        self.assertEqual(fixture["historical_owner_task_id"], "unknown")
        owner = self.seed_lock(
            task_id=str(fixture["historical_owner_task_id"]),
            created_at=1,
        )
        result = self.run_adapter("acquire", RESOURCE, "0", "0")
        self.assertEqual(
            result.returncode,
            0,
            "normal caller-TTL replacement no longer succeeds after expiry",
        )
        self.assertTrue(result.stdout.strip(), "fresh acquisition emitted no token")
        self.assertTrue(
            "task_id=KEN-6" in owner.read_text(encoding="utf-8"),
            "normal replacement did not record the supplied Task identity",
        )
        released = self.run_adapter("release", RESOURCE, result.stdout.strip())
        self.assertEqual(
            released.returncode,
            0,
            "synthetic replacement could not be released",
        )

    def test_pub_70_returned_token_does_not_prove_unknown_task_ownership(self) -> None:
        fixture = incident_fixture("pub-70.json")
        self.assertTrue(fixture["acquisition_returned_token"])
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
            f"task_id={fixture['historical_owner_task_id']}\n",
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

    def test_osm_79_bare_acquire_output_is_not_acquire_any_records(self) -> None:
        fixture = incident_fixture("osm-79.json")
        acquired = self.run_adapter("acquire", RESOURCE, "0", "7200")
        self.assertEqual(
            acquired.returncode,
            0,
            "synthetic acquisition failed before output characterization",
        )
        self.assertEqual(fixture["acquire_stdout_shape"], "bare_token")
        self.assertEqual(fixture["consumer_expected_shape"], "resource_token_records")
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

    def test_interruption_before_checkpoint_uses_retained_owner_metadata(self) -> None:
        acquired = self.run_adapter("acquire", RESOURCE, "0", "7200")
        self.assertEqual(
            acquired.returncode,
            0,
            "synthetic acquisition failed before the interruption point",
        )
        self.assertFalse(
            (self.temporary_root / "checkpoint.json").exists(),
            "fixture unexpectedly persisted a checkpoint before interruption",
        )
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
            "same-Task owner metadata did not recover an interrupted acquisition",
        )
        self.assertTrue(
            recovered.stdout.strip() == owner_token,
            "recovery after interruption did not return the retained lease",
        )
        released = self.run_adapter(
            "release", RESOURCE, recovered.stdout.strip(), session_id=SESSION_TWO
        )
        self.assertEqual(
            released.returncode,
            0,
            "synthetic interrupted lease could not be released",
        )

    def test_native_task_and_session_readbacks_are_stubbed(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
                **self.fake_kent_environment,
            }
        )
        task = subprocess.run(
            ["kent", "task", "show", "KEN-6", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        session = subprocess.run(
            ["kent", "session-id"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(task.returncode, 0, "fake native Task readback failed")
        self.assertEqual(session.returncode, 0, "fake Session readback failed")
        summary = json.loads(task.stdout)["summary"]
        self.assertEqual(
            summary["id"],
            "task-b6672872-8aaf-4dc9-ba9c-dfab7393a5b4",
        )
        self.assertEqual(summary["short_id"], "KEN-6")
        self.assertEqual(session.stdout.strip(), SESSION_ONE)

    def test_native_task_uuid_is_accepted_as_verified_identity_input(self) -> None:
        task = json.loads(
            (FIXTURES / "native-task-ken-6.json").read_text(encoding="utf-8")
        )["summary"]
        session_id = (FIXTURES / "native-session.txt").read_text(
            encoding="utf-8"
        ).strip()
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
        contract = incident_fixture("acquire-output-contract.json")
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
        checkpoint = incident_fixture("checkpoint-retained.json")
        identity = self.read_native_identity(
            session_file=FIXTURES / "native-session-resumed.txt"
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
        checkpoint = incident_fixture("checkpoint-partial.json")
        identity = self.read_native_identity(
            session_file=FIXTURES / "native-session-resumed.txt"
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
        checkpoint = incident_fixture("checkpoint-legacy-native.json")
        identity = self.read_native_identity(
            session_file=FIXTURES / "native-session-resumed.txt"
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
            session_file=FIXTURES / "native-session-resumed.txt"
        )
        for fixture_name, owner_id in (
            ("checkpoint-conflicting.json", "KEN-6"),
            ("checkpoint-foreign.json", "PUB-70"),
        ):
            with self.subTest(fixture=fixture_name):
                checkpoint = incident_fixture(fixture_name)
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
        candidates = incident_fixture("eligible-resources.json")[
            "eligible_resources"
        ]
        identity = self.read_native_identity(
            session_file=FIXTURES / "native-session-resumed.txt"
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
        candidates = incident_fixture("eligible-resources.json")[
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
        candidates = incident_fixture("eligible-resources.json")[
            "eligible_resources"
        ]
        identity = self.read_native_identity(
            session_file=FIXTURES / "native-session-resumed.txt"
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
