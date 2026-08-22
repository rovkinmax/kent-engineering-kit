"""Closed, recoverable operational boundaries for the Kit release tools.

The module intentionally keeps plans declarative.  Kent and Git commands are
constructed here from typed plan values; a plan can never smuggle in a shell,
an executable, or an arbitrary probe.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import time
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from .release import canonical_bytes as release_canonical_bytes
from .release import ReleaseSpec
from .profile import ProjectProfile
from .revision import RevisionPreflightError, preflight_project_revision


SHA1_RE = r"^[0-9a-f]{40}$"
SHA256_RE = r"^[0-9a-f]{64}$"
UUID_RE = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
MAX_LIST = 256
MAX_COMMAND = 64
MAX_OUTPUT = 256 * 1024
JOURNAL_SCHEMA = "kit-operation-journal-v1"
PHASES = {
    "prepared",
    "in_progress",
    "complete",
    "verified",
    "activation_committed",
    "primary_promoted",
    "role_adopted",
    "rolled_back",
}
JOURNAL_FIELDS = {
    "schema",
    "operation",
    "plan_sha256",
    "phase",
    "effects",
    "members",
    "preimage",
    "inventory",
    "inventory_sha256",
    "preflight",
}


class OperationError(RuntimeError):
    """Base class for deterministic operational failures."""


class PlanValidationError(OperationError):
    """Raised for malformed, ambiguous, or digest-mismatched plans."""


class JournalError(OperationError):
    """Raised for malformed or contradictory journal state."""


class EffectBlocked(OperationError):
    """Raised when an effect cannot be settled without guessing."""


class EffectFailed(OperationError):
    """Raised when a confirmed effect exits unsuccessfully."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return release_canonical_bytes(value)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise PlanValidationError(f"value is not canonical JSON: {error}") from error


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _duplicate_free(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _closed(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanValidationError(f"{label} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanValidationError(f"{label} has unknown fields: {unknown}")
    return value


def _required(value: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in value:
        raise PlanValidationError(f"{label} is missing {key!r}")
    return value[key]


def _string(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise PlanValidationError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise PlanValidationError(f"{label} contains NUL")
    return value


def _digest(value: Any, label: str, pattern: str) -> str:
    value = _string(value, label)
    import re

    if not re.fullmatch(pattern, value):
        raise PlanValidationError(f"{label} has an invalid digest")
    return value


def _sha1(value: Any, label: str) -> str:
    return _digest(value, label, SHA1_RE)


def _sha256(value: Any, label: str) -> str:
    return _digest(value, label, SHA256_RE)


def _uuid(value: Any, label: str) -> str:
    return _digest(value, label, UUID_RE).lower()


def _repository(value: Any, label: str) -> str:
    value = _string(value, label)
    import re

    if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
        raise PlanValidationError(f"{label} must be owner/name")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    raw = _string(value, label)
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise PlanValidationError(f"{label} must be absolute without '..'")
    return path


def _bounded_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlanValidationError(f"{label} must be a list")
    if len(value) > MAX_LIST:
        raise PlanValidationError(f"{label} exceeds {MAX_LIST} entries")
    return value


def _unique(values: Iterable[str], label: str) -> None:
    values = list(values)
    if len(values) != len(set(values)):
        raise PlanValidationError(f"{label} contains duplicate identities")


def _argv(value: Any, label: str) -> list[str]:
    values = _bounded_list(value, label)
    if not values or len(values) > MAX_COMMAND:
        raise PlanValidationError(f"{label} must contain 1..{MAX_COMMAND} items")
    return [_string(item, f"{label}[{index}]") for index, item in enumerate(values)]


@dataclass(frozen=True)
class LoadedPlan:
    schema: str
    value: dict[str, Any]
    raw: bytes
    sha256: str


def load_plan(
    path: Path,
    *,
    schema: str,
    expected_sha256: str,
    mutation: bool = False,
    confirm: str | None = None,
) -> LoadedPlan:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise PlanValidationError("plan path must not be a symlink")
    raw = path.read_bytes()
    expected = _sha256(expected_sha256, "expected_plan_sha256")
    actual = sha256_bytes(raw)
    if actual != expected:
        raise PlanValidationError("plan digest mismatch")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_free,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PlanValidationError(f"invalid JSON constant {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlanValidationError(f"plan is not valid JSON: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise PlanValidationError(f"expected schema {schema!r}")
    if canonical_bytes(value) != raw:
        raise PlanValidationError("plan bytes are not canonical JSON")
    if mutation and confirm != expected:
        raise PlanValidationError("mutation requires --confirm equal to plan digest")
    return LoadedPlan(schema, value, raw, actual)


def _safe_state_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise JournalError("state directory must be absolute without '..'")
    if path.exists() and path.is_symlink():
        raise JournalError("state directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise JournalError("state directory must be a private user-owned directory")
    return path


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class OperationJournal:
    """One deterministic journal and advisory lock per operation."""

    def __init__(self, state_dir: Path, operation: str, plan: LoadedPlan):
        self.state_dir = _safe_state_dir(state_dir)
        self.operation = _string(operation, "operation")
        import re

        if not re.fullmatch(r"[a-z0-9-]{1,64}", self.operation):
            raise JournalError("operation name is not a safe journal stem")
        self.plan = plan
        self.lock_path = self.state_dir / ".operations.lock"
        self.path = self.state_dir / f"{self.operation}.journal.json"
        self.temp_path = self.state_dir / f"{self.operation}.journal.tmp"
        self._lock_fd: int | None = None
        self.state: dict[str, Any] | None = None
        self.invocation_id = hashlib.sha256(
            f"{os.getpid()}:{time.monotonic_ns()}".encode()
        ).hexdigest()

    def __enter__(self) -> "OperationJournal":
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self._lock_fd = os.open(self.lock_path, flags, 0o600)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            raise JournalError("operation lock is held by another invocation") from error
        if self.path.exists():
            self.state = self._read()
            if self.state.get("plan_sha256") != self.plan.sha256:
                self.__exit__(None, None, None)
                raise JournalError("journal belongs to a different plan")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def _read(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise JournalError("journal path must not be a symlink")
        raw = self.path.read_bytes()
        try:
            state = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_free)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JournalError(f"journal is not valid JSON: {error}") from error
        if not isinstance(state, dict) or state.get("schema") != JOURNAL_SCHEMA:
            raise JournalError("journal schema is invalid")
        if state.get("operation") != self.operation:
            raise JournalError("journal operation is invalid")
        if state.get("plan_sha256") != self.plan.sha256:
            raise JournalError("journal plan digest is invalid")
        if state.get("phase") not in PHASES:
            raise JournalError("journal phase is invalid")
        if set(state) - JOURNAL_FIELDS:
            raise JournalError("journal contains unknown fields")
        if canonical_bytes(state) + b"\n" != raw:
            raise JournalError("journal readback is not canonical")
        return state

    def persist(self, state: Mapping[str, Any]) -> None:
        if self._lock_fd is None:
            raise JournalError("journal lock is not held")
        data = dict(state)
        data.update(
            {
                "schema": JOURNAL_SCHEMA,
                "operation": self.operation,
                "plan_sha256": self.plan.sha256,
            }
        )
        if set(data) - JOURNAL_FIELDS:
            raise JournalError("journal contains unknown fields")
        if data.get("phase") not in PHASES:
            raise JournalError("journal phase is not closed")
        encoded = canonical_bytes(data) + b"\n"
        if self.temp_path.exists():
            raise JournalError("deterministic journal temporary file already exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.temp_path, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(self.temp_path, self.path)
        _fsync_directory(self.state_dir)
        self.state = self._read()

    def require_phase(self, allowed: set[str]) -> dict[str, Any]:
        if self.state is None or self.state.get("phase") not in allowed:
            raise JournalError("journal phase is not allowed")
        return self.state


def _safe_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["PATH"] = os.defpath
    for key in list(env):
        if key.startswith(("GIT_", "KENT_", "SSH_")):
            env.pop(key, None)
    if extra:
        for key, value in extra.items():
            if not isinstance(key, str) or not key.isidentifier():
                raise PlanValidationError(f"invalid environment key: {key!r}")
            env[key] = _string(value, f"environment.{key}")
    return env


_CHILD_GATE = r"""
import base64, json, os, sys
release_fd = int(sys.argv[1])
command = json.loads(base64.b64decode(sys.argv[2]).decode())
cwd = sys.argv[3]
env = json.loads(base64.b64decode(sys.argv[4]).decode())
if not os.read(release_fd, 1):
    raise SystemExit(125)
os.close(release_fd)
os.chdir(cwd)
os.execve(command[0], command, env)
"""

_GUARDIAN = r"""
import base64, json, os, selectors, signal, subprocess, sys
lock_fd = int(sys.argv[1])
report_fd = int(sys.argv[2])
control_fd = int(sys.argv[3])
command = json.loads(base64.b64decode(sys.argv[4]).decode())
cwd = sys.argv[5]
env = json.loads(base64.b64decode(sys.argv[6]).decode())
limit = int(sys.argv[7])
stdin_bytes = base64.b64decode(sys.argv[8])
gate_read, gate_write = os.pipe()
gate = subprocess.Popen(
    [sys.executable, "-c", sys.argv[9], str(gate_read),
     base64.b64encode(json.dumps(command, separators=(",", ":")).encode()).decode(),
     cwd, base64.b64encode(json.dumps(env, separators=(",", ":")).encode()).decode()],
    cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, close_fds=True, pass_fds=(lock_fd, gate_read),
    start_new_session=True)
os.close(gate_read)
os.write(report_fd, (json.dumps({"pid": gate.pid}) + "\n").encode())
os.close(report_fd)
def stop(signum, _frame):
    try:
        gate.terminate()
        gate.wait(timeout=5)
    except Exception:
        try:
            gate.kill()
            gate.wait(timeout=2)
        except Exception:
            pass
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
if not os.read(control_fd, 1):
    stop(signal.SIGTERM, None)
os.close(control_fd)
os.write(gate_write, b"1")
os.close(gate_write)
if stdin_bytes:
    gate.stdin.write(stdin_bytes)
gate.stdin.close()
streams = {gate.stdout: bytearray(), gate.stderr: bytearray()}
buffers = {gate.stdout: streams[gate.stdout], gate.stderr: streams[gate.stderr]}
selector = selectors.DefaultSelector()
for stream in streams:
    selector.register(stream, selectors.EVENT_READ)
while streams:
    for key, _ in selector.select(0.1):
        chunk = key.fileobj.read1(65536)
        if not chunk:
            selector.unregister(key.fileobj)
            streams.pop(key.fileobj, None)
        elif len(buffers[key.fileobj]) < limit:
            buffers[key.fileobj].extend(chunk[:limit-len(buffers[key.fileobj])])
    if gate.poll() is not None and not streams:
        break
gate.wait()
sys.stdout.buffer.write(bytes(buffers.get(gate.stdout, b""))[-limit:])
sys.stderr.buffer.write(bytes(buffers.get(gate.stderr, b""))[-limit:])
sys.stdout.flush()
sys.stderr.flush()
raise SystemExit(gate.returncode)
"""


def _bounded_communicate(
    process: subprocess.Popen[bytes],
    timeout: float,
    limit: int = MAX_OUTPUT,
) -> tuple[bytes, bytes]:
    selector = selectors.DefaultSelector()
    streams: dict[Any, bytearray] = {}
    buffers: dict[Any, bytearray] = {}
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            streams[stream] = bytearray()
            buffers[stream] = streams[stream]
            selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    while streams:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        for key, _ in selector.select(min(0.1, remaining)):
            chunk = key.fileobj.read1(65536)
            if not chunk:
                selector.unregister(key.fileobj)
                streams.pop(key.fileobj, None)
            elif len(streams[key.fileobj]) < limit:
                streams[key.fileobj].extend(chunk[: limit - len(streams[key.fileobj])])
    process.wait(timeout=max(0.1, deadline - time.monotonic()))
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    return bytes(buffers.get(process.stdout, b"")), bytes(buffers.get(process.stderr, b""))


@dataclass(frozen=True)
class EffectResult:
    command_digest: str
    returncode: int
    stdout_sha256: str
    stderr_sha256: str
    guardian_pid: int | None
    child_pid: int | None
    settlement: str | None = None


def run_effect(
    journal: OperationJournal,
    *,
    effect_key: str,
    command: Sequence[str],
    cwd: Path,
    timeout: float = 30.0,
    preimage_sha256: str | None = None,
    postimage_sha256: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    stdin_bytes: bytes | None = None,
    current_sha256: Callable[[], str] | None = None,
) -> EffectResult:
    if timeout <= 0 or timeout > 300:
        raise PlanValidationError("effect timeout must be between 0 and 300 seconds")
    command = _argv(list(command), "effect command")
    executable = Path(command[0])
    if (
        not executable.is_absolute()
        or executable.is_symlink()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise PlanValidationError("effect executable must be a regular executable file")
    cwd = _absolute_path(str(cwd), "effect cwd")
    if not cwd.is_dir() or cwd.is_symlink():
        raise PlanValidationError("effect cwd must be a regular directory")
    if stdin_bytes is not None and len(stdin_bytes) > MAX_OUTPUT:
        raise PlanValidationError("effect stdin is too large")
    for name, value in (
        ("preimage_sha256", preimage_sha256),
        ("postimage_sha256", postimage_sha256),
    ):
        if value is not None:
            _sha256(value, name)
    if current_sha256 is not None and not callable(current_sha256):
        raise PlanValidationError("current readback must be callable")
    env = _safe_env(extra_env)
    digest = canonical_sha256(command)
    state = journal.state or {}
    effects = dict(state.get("effects") or {})
    previous = effects.get(effect_key)
    if isinstance(previous, dict):
        status = previous.get("status")
        if status == "settled_preimage":
            if previous.get("settled_invocation") == journal.invocation_id:
                raise JournalError("same-cycle effect replay is forbidden")
        elif status not in {None, "settled_preimage"}:
            raise JournalError(f"effect {effect_key!r} is already {status!r}")
    effects[effect_key] = {
        "status": "attempted",
        "attempt": int((previous or {}).get("attempt", 0)) + 1,
        "command_digest": digest,
        "preimage_sha256": preimage_sha256,
        "postimage_sha256": postimage_sha256,
        "stdin_sha256": sha256_bytes(stdin_bytes) if stdin_bytes is not None else None,
        "child": None,
    }
    journal.persist({**state, "phase": state.get("phase", "in_progress"), "effects": effects})
    lock_fd = journal._lock_fd
    if lock_fd is None:
        raise JournalError("effect requires the held operation lock")
    read_fd, write_fd = os.pipe()
    control_read, control_write = os.pipe()
    encoded_command = base64.b64encode(canonical_bytes(command)).decode()
    encoded_env = base64.b64encode(canonical_bytes(env)).decode()
    if stdin_bytes is None:
        stdin_bytes = b""
    encoded_stdin = base64.b64encode(stdin_bytes).decode()
    guardian = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _GUARDIAN,
            str(lock_fd),
            str(write_fd),
            str(control_read),
            encoded_command,
            str(cwd),
            encoded_env,
            str(MAX_OUTPUT),
            encoded_stdin,
            _CHILD_GATE,
        ],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(lock_fd, write_fd, control_read),
        start_new_session=True,
        close_fds=True,
    )
    os.close(write_fd)
    os.close(control_read)
    child_pid: int | None = None
    acknowledged = False
    acknowledgement_error: EffectBlocked | None = None
    try:
        acknowledgement = selectors.DefaultSelector()
        acknowledgement.register(read_fd, selectors.EVENT_READ)
        if not acknowledgement.select(timeout):
            acknowledgement_error = EffectBlocked(
                "effect guardian acknowledgement timed out"
            )
        else:
            data = os.read(read_fd, 4096)
            if not data:
                acknowledgement_error = EffectBlocked(
                    "effect guardian acknowledgement was lost"
                )
            else:
                child_pid = int(json.loads(data.splitlines()[0].decode())["pid"])
                if not _pid_alive(child_pid):
                    acknowledgement_error = EffectBlocked(
                        "effect child is not alive after acknowledgement"
                    )
                else:
                    acknowledged = True
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        acknowledgement_error = EffectBlocked(
            "effect guardian acknowledgement was invalid"
        )
        acknowledgement_error.__cause__ = error
    finally:
        os.close(read_fd)
    if not acknowledged:
        try:
            guardian.terminate()
            guardian.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                guardian.kill()
                guardian.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        os.close(control_write)
        effects[effect_key]["status"] = "unresolved"
        journal.persist({**journal.state, "effects": effects})
        raise acknowledgement_error or EffectBlocked(
            "effect guardian acknowledgement was not durable"
        )
    effects[effect_key]["child"] = {"guardian_pid": guardian.pid, "child_pid": child_pid}
    journal.persist({**journal.state, "effects": effects})
    try:
        os.write(control_write, b"1")
    finally:
        os.close(control_write)
    try:
        stdout, stderr = _bounded_communicate(guardian, timeout)
    except subprocess.TimeoutExpired as error:
        guardian.terminate()
        try:
            guardian.wait(timeout=5)
        except subprocess.TimeoutExpired:
            guardian.kill()
            guardian.wait(timeout=2)
        effects[effect_key]["status"] = "unresolved"
        journal.persist({**journal.state, "effects": effects})
        raise EffectBlocked(f"effect {effect_key!r} timed out") from error
    result = EffectResult(
        digest,
        guardian.returncode or 0,
        sha256_bytes(stdout),
        sha256_bytes(stderr),
        guardian.pid,
        child_pid,
    )
    effects[effect_key]["result"] = {
        "returncode": result.returncode,
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
    }
    effects[effect_key]["status"] = "unresolved"
    journal.persist({**journal.state, "effects": effects})
    if current_sha256 is None:
        if result.returncode:
            raise EffectFailed(f"effect {effect_key!r} exited {result.returncode}")
        raise EffectBlocked("effect requires an exact current-state readback")
    settlement = _settle_effect(
        journal,
        effect_key,
        current_sha256(),
        preimage_sha256,
        postimage_sha256,
    )
    result = EffectResult(
        result.command_digest,
        result.returncode,
        result.stdout_sha256,
        result.stderr_sha256,
        result.guardian_pid,
        result.child_pid,
        settlement,
    )
    if result.returncode and settlement not in {"preimage", "postimage"}:
        raise EffectFailed(f"effect {effect_key!r} exited {result.returncode}")
    return result


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _settle_effect(
    journal: OperationJournal,
    effect_key: str,
    current: str,
    preimage_sha256: str | None,
    postimage_sha256: str | None,
) -> str:
    state = journal.state or {}
    entry = (state.get("effects") or {}).get(effect_key)
    if not isinstance(entry, dict):
        raise JournalError(f"effect {effect_key!r} is not journaled")
    if entry.get("preimage_sha256") != preimage_sha256:
        raise JournalError("effect preimage identity drifted")
    if entry.get("postimage_sha256") != postimage_sha256:
        raise JournalError("effect postimage identity drifted")
    if postimage_sha256 and current == postimage_sha256:
        entry["status"] = "verified"
        journal.persist({**state, "effects": state["effects"]})
        return "postimage"
    if preimage_sha256 and current == preimage_sha256:
        entry["status"] = "settled_preimage"
        entry["settled_invocation"] = journal.invocation_id
        journal.persist({**state, "effects": state["effects"]})
        return "preimage"
    entry["status"] = "ambiguous"
    journal.persist({**state, "effects": state["effects"]})
    raise EffectBlocked("effect completion is ambiguous")


def recover_effect(
    journal: OperationJournal,
    *,
    effect_key: str,
    preimage_sha256: str | None,
    postimage_sha256: str | None,
    current_sha256: Callable[[], str],
) -> str:
    state = journal.require_phase({"prepared", "in_progress", "activation_committed"})
    entry = (state.get("effects") or {}).get(effect_key)
    if not isinstance(entry, dict):
        raise JournalError(f"effect {effect_key!r} is not awaiting settlement")
    if entry.get("status") == "settled_preimage":
        if entry.get("settled_invocation") == journal.invocation_id:
            raise JournalError("same-cycle settled effect replay is forbidden")
        raise JournalError("settled preimage requires a new effect attempt")
    if entry.get("status") not in {
        "attempted",
        "unresolved",
        "failed",
    }:
        raise JournalError("effect is not awaiting settlement")
    return _settle_effect(
        journal,
        effect_key,
        current_sha256(),
        preimage_sha256,
        postimage_sha256,
    )


def _run(command: Sequence[str], *, cwd: Path, timeout: float = 30.0) -> tuple[int, bytes, bytes]:
    argv = _argv(list(command), "internal command")
    if not Path(argv[0]).is_absolute():
        raise OperationError("internal command executable must be absolute")
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=_safe_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    try:
        out, err = _bounded_communicate(process, timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait(timeout=2)
        raise OperationError("internal command timed out") from error
    return process.returncode or 0, out, err


def _json_command(command: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    code, out, err = _run(command, cwd=cwd)
    if code:
        raise OperationError(err.decode(errors="replace") or "command failed")
    try:
        value = json.loads(out.decode("utf-8"), object_pairs_hook=_duplicate_free)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OperationError("internal command did not return JSON") from error
    if not isinstance(value, dict):
        raise OperationError("internal command returned a non-object")
    return value


def _kent_path(value: Any, label: str = "kent") -> Path:
    path = _absolute_path(value, label)
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise PlanValidationError(f"{label} is not an executable file")
    return path


def _verify_executable(path: Path, digest: str) -> None:
    if sha256_bytes(path.read_bytes()) != _sha256(digest, "kent_sha256"):
        raise PlanValidationError("Kent executable bytes do not match the plan")


def _kent_json(kent: Path, args: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    return _json_command([str(kent), *args], cwd=cwd)


def _kent_optional(kent: Path, args: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    code, out, err = _run([str(kent), *args], cwd=cwd)
    if code == 0:
        try:
            value = json.loads(out.decode("utf-8"), object_pairs_hook=_duplicate_free)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OperationError("Kent command did not return JSON") from error
        if not isinstance(value, dict):
            raise OperationError("Kent command returned a non-object")
        return value
    text = (err + out).decode("utf-8", errors="replace").lower()
    if "not found" in text or "no such" in text or "unknown workflow" in text:
        return {"present": False}
    raise OperationError(err.decode(errors="replace") or "Kent command failed")


def _git(root: Path, *args: str, check: bool = True) -> str:
    command = ["/usr/bin/git", "-C", str(root), *args]
    code, out, err = _run(command, cwd=root)
    if check and code:
        raise OperationError(err.decode(errors="replace") or out.decode(errors="replace"))
    return out.decode().strip()


def _repository_identity(root: Path) -> str:
    remote = _git(root, "config", "--get", "remote.origin.url", check=False)
    if not remote:
        raise OperationError(f"{root} has no origin identity")
    import re

    match = re.search(r"(?:github\.com[:/])([^/:\s]+/[^/\s]+?)(?:\.git)?$", remote)
    if not match:
        raise OperationError(f"{root} has an unrecognized origin identity")
    return match.group(1)


def _atomic_write(path: Path, data: bytes) -> None:
    path = _absolute_path(str(path), "report_path")
    if path.is_symlink():
        raise JournalError("report path must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(lock_fd)
        raise JournalError("report lock is held by another writer") from error
    temporary = path.parent / f".{path.name}.tmp"
    try:
        if temporary.exists():
            raise JournalError("deterministic report temporary file already exists")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        if path.read_bytes() != data:
            raise JournalError("report readback mismatch")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def verify_release_portfolio(
    plan: LoadedPlan,
    *,
    report_path: Path | None = None,
    write_report: bool = False,
) -> dict[str, Any]:
    if report_path is not None and not write_report:
        raise PlanValidationError("report_path is allowed only with write_report")
    data = _closed(plan.value, {"schema", "kit", "projects", "report_path"}, "portfolio plan")
    kit = _closed(_required(data, "kit", "portfolio plan"), {"root", "repository", "commit"}, "kit")
    kit_root = _absolute_path(_required(kit, "root", "kit"), "kit.root")
    kit_repo = _repository(_required(kit, "repository", "kit"), "kit.repository")
    kit_commit = _sha1(_required(kit, "commit", "kit"), "kit.commit")
    projects = _bounded_list(_required(data, "projects", "portfolio plan"), "projects")
    if len(projects) != 4:
        raise PlanValidationError("portfolio plan must bind exactly four projects")
    records = []
    identities = []
    _git(kit_root, "cat-file", "-e", f"{kit_commit}^{{commit}}")
    if _repository_identity(kit_root) != kit_repo:
        raise OperationError("Kit repository identity does not match the plan")
    for index, raw in enumerate(projects):
        item = _closed(raw, {"root", "repository", "commit"}, f"projects[{index}]")
        root = _absolute_path(_required(item, "root", f"projects[{index}]"), "project.root")
        repository = _repository(
            _required(item, "repository", f"projects[{index}]"),
            "project.repository",
        )
        commit = _sha1(_required(item, "commit", f"projects[{index}]"), "project.commit")
        identities.append(repository)
        if _repository_identity(root) != repository:
            raise OperationError("project repository identity does not match the plan")
        try:
            result = preflight_project_revision(root, commit)
        except RevisionPreflightError as error:
            raise OperationError(str(error)) from error
        if result.commit_oid != commit:
            raise OperationError("selected project commit changed during preflight")
        if result.release_preview is None or result.runtime_source_inputs is None:
            raise OperationError("schema-4 release/runtime inputs are incomplete")
        source_digests = _selected_source_digests(root, commit)
        records.append(
            {
                "repository": repository,
                "commit": commit,
                "profile_sha256": source_digests["profile_sha256"],
                "release_spec_sha256": source_digests["release_spec_sha256"],
                "source_manifest_sha256": source_digests["source_manifest_sha256"],
                "snapshot_sha256": source_digests["snapshot_sha256"],
                "builder_sha256": source_digests["builder_sha256"],
                "release_preview_sha256": (
                    canonical_sha256(result.release_preview)
                    if result.release_preview is not None
                    else None
                ),
                "runtime_source_inputs_sha256": (
                    result.runtime_source_inputs.selected_runtime_source_inputs_sha256
                    if result.runtime_source_inputs is not None
                    else None
                ),
            }
        )
    _unique(identities, "projects")
    report = {
        "schema": "release-portfolio-report-v2",
        "plan_sha256": plan.sha256,
        "kit": {"repository": kit_repo, "commit": kit_commit},
        "projects": sorted(records, key=lambda item: item["repository"]),
        "ready": True,
    }
    if write_report:
        if "report_path" not in data:
            raise PlanValidationError("write_report requires a plan-bound report_path")
        target = _absolute_path(data["report_path"], "report_path")
        if report_path is not None and Path(report_path) != target:
            raise PlanValidationError("report path differs from the plan binding")
        _atomic_write(target, canonical_bytes(report) + b"\n")
    elif report_path is not None:
        raise PlanValidationError("report_path is allowed only with write_report")
    return report


def _blob_digest(root: Path, commit: str, path: str) -> str:
    out = _blob_bytes(root, commit, path)
    return sha256_bytes(out)


def _blob_bytes(root: Path, commit: str, path: str) -> bytes:
    code, out, err = _run(
        ["/usr/bin/git", "-C", str(root), "show", f"{commit}:{path}"], cwd=root
    )
    if code:
        raise OperationError(err.decode(errors="replace") or f"missing {path}")
    return out


def _selected_source_digests(root: Path, commit: str) -> dict[str, Any]:
    profile_path = ".kent/workflow-profile.toml"
    profile_bytes = _blob_bytes(root, commit, profile_path)
    try:
        profile = ProjectProfile.from_toml(
            root,
            profile_bytes.decode("utf-8"),
            source=f"{commit}:{profile_path}",
            check_files=False,
        )
        if profile.schema_version != 4:
            raise OperationError("portfolio requires schema-4 projects")
        spec_path = profile.release.spec_path
        spec_bytes = _blob_bytes(root, commit, spec_path)
        spec = ReleaseSpec.from_toml(
            spec_bytes.decode("utf-8"), profile=profile
        )
        manifest_path = spec.source_manifest.path
        manifest_bytes = _blob_bytes(root, commit, manifest_path)
        snapshot_bytes = _blob_bytes(root, commit, profile.release.snapshot_path)
        builder_bytes = (
            _blob_bytes(root, commit, profile.release.builder_path)
            if profile.release.builder_path
            else None
        )
    except (AttributeError, UnicodeDecodeError, ValueError, KeyError) as error:
        raise OperationError("selected release source is not schema-4 complete") from error
    return {
        "profile_sha256": sha256_bytes(profile_bytes),
        "release_spec_sha256": sha256_bytes(spec_bytes),
        "source_manifest_sha256": sha256_bytes(manifest_bytes),
        "snapshot_sha256": sha256_bytes(snapshot_bytes),
        "builder_sha256": sha256_bytes(builder_bytes) if builder_bytes else None,
    }


def _reject_raw_protocol_fields(value: Mapping[str, Any], label: str) -> None:
    forbidden = {
        "command",
        "commands",
        "argv",
        "probe",
        "absence_probe",
        "sql",
        "script",
        "shell",
        "executable",
    }
    found = sorted(forbidden & set(value))
    if found:
        raise PlanValidationError(f"{label} contains forbidden protocol fields: {found}")


def _session_manifest(path: Path) -> list[dict[str, Any]]:
    root = path
    if not root.is_dir() or root.is_symlink():
        raise OperationError("retained Session directory is absent or unsafe")
    result: list[dict[str, Any]] = []
    total_bytes = 0
    for entry in sorted(root.rglob("*")):
        relative = entry.relative_to(root)
        if any(part in {".", ".."} for part in relative.parts):
            raise OperationError("invalid Session manifest path")
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise OperationError("Session manifest refuses symlinks")
        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
        record: dict[str, Any] = {
            "path": relative.as_posix(),
            "type": kind,
            "mode": stat.S_IMODE(info.st_mode),
            "bytes": info.st_size if kind == "file" else 0,
        }
        if kind == "file":
            if info.st_size > MAX_OUTPUT:
                raise OperationError("Session manifest file exceeds the bound")
            total_bytes += info.st_size
            if total_bytes > MAX_OUTPUT:
                raise OperationError("Session manifest exceeds the byte bound")
            record["sha256"] = sha256_bytes(entry.read_bytes())
        result.append(record)
    if len(result) > MAX_LIST:
        raise OperationError("Session manifest is too large")
    return result


def _session_path(session: Mapping[str, Any], roots: Sequence[Path]) -> Path:
    if session.get("relative") is not None:
        root = _absolute_path(session.get("root"), "session.root")
        relative = Path(_string(session["relative"], "session.relative"))
        if relative.is_absolute() or ".." in relative.parts:
            raise OperationError("Session path traversal is forbidden")
        path = root / relative
    else:
        path = _absolute_path(session.get("path"), "session.path")
    path = path.absolute()
    matching_root = next(
        (
            root.absolute()
            for root in roots
            if path == root.absolute() or root.absolute() in path.parents
        ),
        None,
    )
    if matching_root is None:
        raise EffectBlocked("Session path is outside the declared roots")
    relative_parts = path.relative_to(matching_root).parts
    current = matching_root
    if current.is_symlink():
        raise EffectBlocked("Session root is a symlink")
    for part in relative_parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            raise EffectBlocked("Session path is absent") from None
        if stat.S_ISLNK(info.st_mode):
            raise EffectBlocked("Session path contains a symlink")
    if not current.is_dir():
        raise EffectBlocked("Session path is not a directory")
    return current


KENT_SCHEMA_IDENTITY = "kent-2.6.1"


def _typed_member(member: Mapping[str, Any], index: int) -> dict[str, Any]:
    _reject_raw_protocol_fields(member, f"members[{index}]")
    allowed = {
        "workflow_id",
        "revision",
        "links",
        "default",
        "tasks",
        "sessions",
        "worktrees",
        "retained",
        "absent",
        "delete_preview",
    }
    item = _closed(member, allowed, f"members[{index}]")
    workflow_id = _uuid(_required(item, "workflow_id", f"members[{index}]"), "workflow_id")
    revision = _sha1(_required(item, "revision", f"members[{index}]"), "revision")
    tasks = _bounded_list(item.get("tasks", []), f"members[{index}].tasks")
    for task in tasks:
        task = _closed(
            task,
            {"id", "terminal", "current_node", "approval_pending", "status"},
            "task",
        )
        _string(_required(task, "id", "task.id"), "task.id")
        if "status" in task:
            _string(task["status"], "task.status")
        if task.get("terminal") is not True:
            raise PlanValidationError("D9 refuses a nonterminal task")
        if task.get("current_node") not in (None, ""):
            raise PlanValidationError("D9 refuses an active Current Node")
        if task.get("approval_pending") is True:
            raise PlanValidationError("D9 refuses a pending approval")
    _unique(
        [_string(task.get("id"), "task.id") for task in tasks],
        "task identities",
    )
    for key in ("links", "sessions", "worktrees", "retained", "absent"):
        _bounded_list(item.get(key, []), f"members[{index}].{key}")
    if not isinstance(item.get("links"), list) or not isinstance(
        item.get("default"), (str, type(None), dict)
    ):
        raise PlanValidationError("D9 links/default inventory is not typed")
    for link in item.get("links", []):
        if not isinstance(link, dict):
            raise PlanValidationError("D9 link records must be objects")
        _closed(link, {"project_id", "workflow_id", "is_default"}, "D9 link")
        _uuid(_required(link, "project_id", "D9 link"), "D9 link.project_id")
        _uuid(_required(link, "workflow_id", "D9 link"), "D9 link.workflow_id")
        if not isinstance(_required(link, "is_default", "D9 link"), bool):
            raise PlanValidationError("D9 link.is_default must be boolean")
    if isinstance(item.get("default"), dict):
        _closed(item["default"], {"project_id", "workflow_id"}, "D9 default")
        _uuid(_required(item["default"], "project_id", "D9 default"), "default.project_id")
        _uuid(_required(item["default"], "workflow_id", "D9 default"), "default.workflow_id")
    for resource_name in ("retained", "absent"):
        for resource in item.get(resource_name, []):
            if not isinstance(resource, dict):
                raise PlanValidationError(f"D9 {resource_name} records must be objects")
            _closed(resource, {"kind", "id", "path", "sha256"}, f"D9 {resource_name}")
            _string(_required(resource, "kind", f"D9 {resource_name}"), f"{resource_name}.kind")
            _string(_required(resource, "id", f"D9 {resource_name}"), f"{resource_name}.id")
            if resource.get("path") is not None:
                _absolute_path(resource["path"], f"{resource_name}.path")
            if resource.get("sha256") is not None:
                _sha256(resource["sha256"], f"{resource_name}.sha256")
    preview = _closed(
        _required(item, "delete_preview", f"members[{index}]"),
        {"workflow_id", "sha256"},
        f"members[{index}].delete_preview",
    )
    if _uuid(preview.get("workflow_id"), "delete_preview.workflow_id") != workflow_id:
        raise PlanValidationError("delete preview workflow identity drifted")
    _sha256(_required(preview, "sha256", "delete_preview"), "delete_preview.sha256")
    for worktree in item.get("worktrees", []):
        if not isinstance(worktree, dict):
            raise PlanValidationError("worktree records must be objects")
        _closed(
            worktree,
            {"path", "branch", "head", "dirty", "owner_session", "retained"},
            f"members[{index}].worktree",
        )
        if worktree.get("dirty") is True or worktree.get("owner_session"):
            raise PlanValidationError("D9 refuses a dirty or live-owned worktree")
        if "path" in worktree:
            _absolute_path(worktree["path"], "worktree.path")
    for session in item.get("sessions", []):
        if not isinstance(session, dict):
            raise PlanValidationError("Session records must be objects")
        _closed(
            session,
            {"id", "status", "path", "relative", "root", "manifest", "retained", "task_id"},
            f"members[{index}].session",
        )
        _string(_required(session, "id", "session.id"), "session.id")
        _string(_required(session, "status", "session.status"), "session.status")
        if session.get("path") is not None:
            _absolute_path(session["path"], "session.path")
        if session.get("relative") is not None:
            _string(session["relative"], "session.relative")
        if session.get("root") is not None:
            _absolute_path(session["root"], "session.root")
        if session.get("manifest") is not None:
            canonical_bytes(session["manifest"])
    return {
        **item,
        "workflow_id": workflow_id,
        "revision": revision,
        "tasks": tasks,
    }


def _validate_d9_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(
        plan.value,
        {"schema", "project_id", "state_dir", "kent", "database", "members"},
        "retirement plan",
    )
    project_id = _uuid(_required(data, "project_id", "retirement plan"), "project_id")
    state_dir = _absolute_path(_required(data, "state_dir", "retirement plan"), "state_dir")
    kent = _closed(_required(data, "kent", "retirement plan"), {"path", "sha256"}, "kent")
    kent_path = _kent_path(_required(kent, "path", "kent"), "kent.path")
    kent_sha = _sha256(_required(kent, "sha256", "kent"), "kent.sha256")
    _verify_executable(kent_path, kent_sha)
    database = _closed(
        _required(data, "database", "retirement plan"),
        {"path", "schema", "project_root", "session_roots"},
        "database",
    )
    database_path = _absolute_path(_required(database, "path", "database"), "database.path")
    schema_identity = _string(_required(database, "schema", "database"), "database.schema")
    if schema_identity != KENT_SCHEMA_IDENTITY:
        raise PlanValidationError("unsupported Kent persistence schema")
    project_root = _absolute_path(_required(database, "project_root", "database"), "database.project_root")
    session_roots = [_absolute_path(item, "session_root") for item in _bounded_list(
        _required(database, "session_roots", "database"), "database.session_roots"
    )]
    members = [_typed_member(raw, index) for index, raw in enumerate(
        _bounded_list(_required(data, "members", "retirement plan"), "members")
    )]
    if not members:
        raise PlanValidationError("retirement plan has no members")
    _unique([item["workflow_id"] for item in members], "workflow identities")
    return {
        "project_id": project_id,
        "state_dir": state_dir,
        "kent": kent_path,
        "kent_sha256": kent_sha,
        "database": database_path,
        "schema": schema_identity,
        "project_root": project_root,
        "session_roots": session_roots,
        "members": members,
    }


def _kent_pages(
    kent: Path,
    args: Sequence[str],
    *,
    cwd: Path,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _kent_json(
            kent,
            [*args, "--offset", str(offset), "--limit", "100", "--json"],
            cwd=cwd,
        )
        pages.append(page)
        rows = page.get("items", page.get("workflows", page.get("tasks", [])))
        if not isinstance(rows, list) or len(rows) < 100:
            return pages
        offset += 100
        if offset > MAX_LIST * 100:
            raise OperationError("Kent pagination exceeded the bounded inventory")


def _page_rows(pages: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict):
            raise OperationError("Kent pagination returned a non-object page")
        values: Any = []
        for key in keys:
            if key in page:
                values = page[key]
                break
        if not isinstance(values, list):
            raise OperationError("Kent pagination returned non-list rows")
        rows.extend(row for row in values if isinstance(row, dict))
    return rows


def _d9_read_inventory(
    parsed: Mapping[str, Any],
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the complete source-owned read protocol, never a plan command."""
    kent = parsed["kent"]
    _verify_executable(kent, parsed["kent_sha256"])
    project = parsed["project_id"]
    cwd = parsed["project_root"]
    workflow_pages = _kent_pages(
        kent, ["workflow", "list", "--project", project], cwd=cwd
    )
    workflow_rows = _page_rows(workflow_pages, ("items", "workflows"))
    result: dict[str, Any] = {
        "projects": _kent_json(kent, ["project", "list", "--json"], cwd=cwd),
        "workflows": workflow_pages,
        "worktrees": _kent_json(kent, ["worktree", "list", "--json"], cwd=cwd),
        "database_schema": parsed["schema"],
    }
    for member in parsed["members"]:
        _d9_check_git_resources(member)
        _d9_check_session_manifests(member, parsed["session_roots"])
        wid = member["workflow_id"]
        listed = any(
            row.get("id", row.get("workflow_id")) == wid for row in workflow_rows
        )
        if not listed:
            reference_member = reference.get(wid, {}) if isinstance(reference, Mapping) else {}
            reference_sqlite = reference_member.get("sqlite", {}) if isinstance(reference_member, Mapping) else {}
            retained_session_ids = [
                row.get("session_id")
                for row in reference_sqlite.get("sessions", [])
                if isinstance(row, dict) and isinstance(row.get("session_id"), str)
            ]
            retained_task_ids = [
                task["id"]
                for task in member.get("tasks", [])
                if isinstance(task, dict) and isinstance(task.get("id"), str)
            ]
            retained_session_ids.extend(
                session.get("id", session.get("session_id"))
                for session in reference_member.get("sessions", [])
                if isinstance(session, dict)
                and isinstance(session.get("id", session.get("session_id")), str)
            )
            result[wid] = {
                "workflow": {"present": False, "workflow_id": wid},
                "tasks": [],
                "task_rows": [],
                "task_show": [],
                "sessions": [],
                "sqlite": _sqlite_snapshot(
                    parsed["database"], parsed["schema"],
                    retained_session_ids, retained_task_ids,
                ),
            }
            continue
        tasks = _kent_pages(
            kent,
            ["task", "list", "--project", project, "--workflow", wid],
            cwd=cwd,
        )
        task_rows = [
            row for row in _page_rows(tasks, ("items", "tasks"))
        ]
        task_ids = {
            str(task["id"])
            for task in member.get("tasks", [])
            if isinstance(task, dict) and isinstance(task.get("id"), str)
        }
        task_ids.update(
            str(task.get("id", task.get("task_id")))
            for task in task_rows
            if isinstance(task, dict)
            and isinstance(task.get("id", task.get("task_id")), str)
        )
        result[wid] = {
            "workflow": _kent_optional(kent, ["workflow", "inspect", wid, "--json"], cwd=cwd),
            "graph": _kent_json(kent, ["workflow", "graph", "inspect", wid, "--json"], cwd=cwd),
            "validate": _kent_json(
                kent, ["workflow", "validate", wid, "--json"], cwd=cwd
            ),
            "tasks": tasks,
            "preview": _kent_json(
                kent,
                ["workflow", "delete", wid, "--json"],
                cwd=cwd,
            ),
        }
        result[wid]["links"] = result[wid]["workflow"].get("links", [])
        result[wid]["default"] = result[wid]["workflow"].get("default")
        result[wid]["current_nodes"] = result[wid]["workflow"].get("current_nodes", [])
        result[wid]["pending_approvals"] = result[wid]["workflow"].get("pending_approvals", [])
        for task in task_rows:
            task_id = task.get("id", task.get("task_id"))
            if isinstance(task_id, str):
                result[wid].setdefault("task_show", []).append(
                    _kent_json(kent, ["task", "show", task_id, "--project", project, "--json"], cwd=cwd)
                )
                result[wid].setdefault("sessions", []).extend(
                    _kent_pages(
                        kent,
                        ["task", "sessions", task_id, "--project", project],
                        cwd=cwd,
                    )
                )
        session_rows = _page_rows(
            result[wid].get("sessions", []), ("items", "sessions")
        )
        session_ids = {
            str(session.get("id", session.get("session_id")))
            for session in session_rows
            if isinstance(session.get("id", session.get("session_id")), str)
        }
        if reference:
            old = reference.get(wid, {})
            for session in old.get("sessions", []) if isinstance(old, dict) else []:
                if isinstance(session, dict):
                    session_id = session.get("session_id", session.get("id"))
                    if isinstance(session_id, str):
                        session_ids.add(session_id)
            old_sqlite = old.get("sqlite", {}) if isinstance(old, dict) else {}
            for session in old_sqlite.get("sessions", []) if isinstance(old_sqlite, dict) else []:
                if isinstance(session, dict) and isinstance(session.get("session_id"), str):
                    session_ids.add(session["session_id"])
        result[wid]["sqlite"] = _sqlite_snapshot(
            parsed["database"],
            parsed["schema"],
            sorted(session_ids),
            sorted(task_ids),
        )
        result[wid]["links"] = result[wid]["workflow"].get("links", [])
        result[wid]["default"] = result[wid]["workflow"].get("default")
        result[wid]["current_nodes"] = result[wid]["workflow"].get("current_nodes", [])
        result[wid]["pending_approvals"] = result[wid]["workflow"].get("pending_approvals", [])
        result[wid]["task_rows"] = task_rows
    return result


def _d9_check_git_resources(member: Mapping[str, Any]) -> None:
    for resource in member.get("worktrees", []):
        path = _absolute_path(resource["path"], "worktree.path")
        if not path.exists():
            raise EffectBlocked("declared managed worktree is absent")
        if _git(path, "status", "--porcelain"):
            raise EffectBlocked("declared managed worktree is dirty")
        if resource.get("branch") is not None and _git(
            path, "branch", "--show-current"
        ) != resource["branch"]:
            raise EffectBlocked("managed worktree branch drifted")
        if resource.get("head") is not None and _git(
            path, "rev-parse", "HEAD"
        ) != resource["head"]:
            raise EffectBlocked("managed worktree HEAD drifted")


def _d9_check_session_manifests(
    member: Mapping[str, Any],
    roots: Sequence[Path],
) -> None:
    for session in member.get("sessions", []):
        if session.get("retained") is not True:
            continue
        actual = _session_manifest(_session_path(session, roots))
        expected = session.get("manifest")
        if expected is not None and canonical_sha256(actual) != canonical_sha256(expected):
            raise EffectBlocked("retained Session manifest drifted")


def _d9_member_live_gate(
    member: Mapping[str, Any],
    live: Mapping[str, Any],
    *,
    require_preview: bool = True,
) -> None:
    wid = member["workflow_id"]
    workflow = live.get("workflow")
    if not isinstance(workflow, dict):
        raise EffectBlocked(f"D9 workflow {wid} readback is not an object")
    if workflow.get("present") is False:
        raise EffectBlocked(f"D9 workflow {wid} is absent before deletion")
    if workflow.get("id", workflow.get("workflow_id", wid)) != wid:
        raise EffectBlocked(f"D9 workflow {wid} identity drifted")
    plan_tasks = {
        str(task["id"]): task
        for task in member.get("tasks", [])
        if isinstance(task, dict)
    }
    live_tasks = live.get("task_rows", _page_rows(live.get("tasks"), ("items", "tasks")))
    live_by_id: dict[str, Mapping[str, Any]] = {}
    for task in live_tasks:
        task_id = task.get("id", task.get("task_id"))
        if isinstance(task_id, str):
            live_by_id[task_id] = task
    if set(live_by_id) != set(plan_tasks):
        raise EffectBlocked(f"D9 task inventory drifted for {wid}")
    for task_id, task in live_by_id.items():
        if task.get("terminal") is not True:
            raise EffectBlocked(f"D9 refuses live nonterminal task {task_id}")
        if task.get("current_node") not in (None, ""):
            raise EffectBlocked(f"D9 refuses active Current Node {task_id}")
        if task.get("approval_pending") is True:
            raise EffectBlocked(f"D9 refuses pending approval {task_id}")
        expected = plan_tasks[task_id]
        for key in ("status", "current_node", "terminal", "approval_pending"):
            if key in expected and task.get(key) != expected.get(key):
                raise EffectBlocked(f"D9 task {task_id} drifted: {key}")
    if "links" in member and canonical_sha256(live.get("links", [])) != canonical_sha256(member["links"]):
        raise EffectBlocked(f"D9 links drifted for {wid}")
    if "default" in member and canonical_sha256(live.get("default")) != canonical_sha256(member["default"]):
        raise EffectBlocked(f"D9 default drifted for {wid}")
    if live.get("current_nodes"):
        raise EffectBlocked(f"D9 has active Current Nodes for {wid}")
    if live.get("pending_approvals"):
        raise EffectBlocked(f"D9 has pending approvals for {wid}")
    if require_preview:
        preview = live.get("preview")
        if not isinstance(preview, dict):
            raise EffectBlocked(f"D9 delete preview is missing for {wid}")
        expected_digest = member["delete_preview"]["sha256"]
        actual_digest = preview.get("sha256")
        if actual_digest is None:
            actual_digest = canonical_sha256(preview)
        if actual_digest != expected_digest:
            raise EffectBlocked(f"D9 delete preview drifted for {wid}")


def _d9_inventory_digest(
    inventory: Mapping[str, Any],
    workflow_ids: Sequence[str],
) -> str:
    normalized = {
        key: inventory.get(key)
        for key in ("projects", "workflows", "worktrees", "database_schema")
    }
    normalized["members"] = {
        workflow_id: inventory.get(workflow_id)
        for workflow_id in workflow_ids
    }
    return canonical_sha256(normalized)


def _d9_current_digest(
    parsed: Mapping[str, Any],
    member: Mapping[str, Any],
    reference: Mapping[str, Any] | None,
) -> str:
    inventory = _d9_read_inventory(parsed, reference)
    _d9_member_live_gate(member, inventory[member["workflow_id"]])
    return _d9_inventory_digest(inventory, [item["workflow_id"] for item in parsed["members"]])


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def _sqlite_schema_check(
    connection: sqlite3.Connection,
    expected_schema: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if expected_schema != KENT_SCHEMA_IDENTITY:
        raise OperationError("unsupported Kent persistence schema")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {"sessions", "session_workflow_node_associations"}
    if not required.issubset(tables):
        raise OperationError("Kent persistence schema fingerprint is incomplete")
    session_info = list(connection.execute("PRAGMA table_info(sessions)"))
    association_info = list(
        connection.execute(
            "PRAGMA table_info(session_workflow_node_associations)"
        )
    )
    session_columns = tuple(row[1] for row in session_info)
    association_columns = tuple(row[1] for row in association_info)
    if not {"id", "task_id"}.issubset(session_columns):
        raise OperationError("sessions schema fingerprint is unsupported")
    if not {"session_id", "task_id"}.issubset(association_columns):
        raise OperationError("association schema fingerprint is unsupported")
    session_fk = list(connection.execute("PRAGMA foreign_key_list(sessions)"))
    association_fk = list(
        connection.execute(
            "PRAGMA foreign_key_list(session_workflow_node_associations)"
        )
    )
    if not any(row[3] == "task_id" and row[6].upper() == "SET NULL" for row in session_fk):
        raise OperationError("sessions.task_id cascade fingerprint is unsupported")
    if not any(row[3] == "task_id" and row[6].upper() == "CASCADE" for row in association_fk):
        raise OperationError("association task cascade fingerprint is unsupported")
    return session_columns, association_columns


def _sqlite_snapshot(
    database: Path,
    expected_schema: str,
    session_ids: Sequence[str],
    task_ids: Sequence[str],
) -> dict[str, Any]:
    """Read only fixed Session and association queries; plans supply no SQL."""
    if not database.is_file() or database.is_symlink():
        raise OperationError("Kent persistence database is absent or unsafe")
    uri = f"file:{database}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        session_columns, association_columns = _sqlite_schema_check(
            connection, expected_schema
        )
        sessions: list[dict[str, Any]] = []
        for session_id in session_ids:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchall()
            if len(rows) > 1:
                raise OperationError("Session identity matched multiple rows")
            sessions.append(
                {
                    "session_id": session_id,
                    "present": bool(rows),
                    "row": (
                        {
                            key: _sqlite_value(value)
                            for key, value in zip(session_columns, rows[0])
                        }
                        if rows
                        else None
                    ),
                }
            )
        associations: list[dict[str, Any]] = []
        for session_id in session_ids:
            for row in connection.execute(
                "SELECT * FROM session_workflow_node_associations WHERE session_id = ?",
                (session_id,),
            ).fetchall():
                associations.append(
                    {
                        key: _sqlite_value(value)
                        for key, value in zip(association_columns, row)
                    }
                )
        for task_id in task_ids:
            for row in connection.execute(
                "SELECT * FROM session_workflow_node_associations WHERE task_id = ?",
                (task_id,),
            ).fetchall():
                item = {
                    key: _sqlite_value(value)
                    for key, value in zip(association_columns, row)
                }
                if item not in associations:
                    associations.append(item)
        return {
            "sessions": sessions,
            "associations": associations,
        }
    except sqlite3.Error as error:
        raise OperationError(f"read-only Session query failed: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def _kent_delete_command(kent: Path, workflow_id: str, confirm: bool) -> list[str]:
    command = [str(kent), "workflow", "delete", workflow_id]
    if confirm:
        command.append("--confirm")
    command.append("--json")
    return command


def _resource_digest(value: Any) -> str:
    return canonical_sha256(value)


def retire_workflow_batch(
    plan: LoadedPlan,
    *,
    mode: str,
    kent: str | Path | None = None,
) -> dict[str, Any]:
    parsed = _validate_d9_plan(plan)
    if mode not in {"preview", "apply", "resume"}:
        raise PlanValidationError("retirement mode must be preview, apply, or resume")
    if kent is not None and str(kent) != str(parsed["kent"]):
        raise PlanValidationError("runtime --kent differs from the plan-bound executable")
    workflow_ids = [item["workflow_id"] for item in parsed["members"]]
    if mode == "preview":
        inventory = _d9_read_inventory(parsed)
        _d9_validate_batch_inventory(parsed, inventory, {}, inventory)
        return {
            "schema": "workflow-retirement-batch-report-v2",
            "plan_sha256": plan.sha256,
            "phase": "preview",
            "inventory_sha256": _d9_inventory_digest(inventory, workflow_ids),
            "effects_released": 0,
        }
    with OperationJournal(parsed["state_dir"], "workflow-retirement-batch", plan) as journal:
        if journal.state is None:
            inventory = _d9_read_inventory(parsed)
            _d9_validate_batch_inventory(parsed, inventory, {}, inventory)
            journal.persist(
                {
                    "phase": "prepared",
                    "inventory": inventory,
                    "inventory_sha256": _d9_inventory_digest(inventory, workflow_ids),
                    "members": [
                        {"workflow_id": item["workflow_id"], "status": "pending"}
                        for item in parsed["members"]
                    ],
                    "effects": {},
                }
            )
        state = journal.require_phase({"prepared", "in_progress", "complete"})
        if state["phase"] == "complete":
            return {"schema": "workflow-retirement-batch-report-v2", "phase": "complete", "resumed": True}
        statuses = {item["workflow_id"]: item["status"] for item in state["members"]}
        for member in parsed["members"]:
            if statuses[member["workflow_id"]] == "verified":
                continue
            inventory = _d9_read_inventory(parsed, state.get("inventory"))
            _d9_validate_batch_inventory(parsed, inventory, statuses, state["inventory"])
            journal.persist({**journal.state, "phase": "in_progress"})
            wid = member["workflow_id"]
            before = inventory.get(wid, {})
            preimage = canonical_sha256(before)
            preview = before.get("preview")
            if not isinstance(preview, dict):
                raise EffectBlocked(f"D9 delete preview is missing for {wid}")
            postimage = canonical_sha256(
                {"workflow": {"present": False, "workflow_id": wid}}
            )

            def current() -> str:
                latest = _d9_read_inventory(parsed, state.get("inventory"))
                latest_member = latest.get(wid, {})
                if latest_member.get("workflow", {}).get("present", True):
                    _d9_member_live_gate(member, latest_member)
                    return preimage
                _validate_d9_postimage(member, before, latest_member)
                return postimage

            effect_key = f"delete:{wid}"
            existing = (journal.state.get("effects") or {}).get(effect_key)
            if isinstance(existing, dict) and existing.get("status") in {
                "attempted", "unresolved", "failed", "ambiguous"
            }:
                settled = recover_effect(
                    journal,
                    effect_key=effect_key,
                    preimage_sha256=preimage,
                    postimage_sha256=postimage,
                    current_sha256=current,
                )
            else:
                settled = run_effect(
                    journal,
                    effect_key=effect_key,
                    command=_kent_delete_command(parsed["kent"], wid, True),
                    cwd=parsed["project_root"],
                    preimage_sha256=preimage,
                    postimage_sha256=postimage,
                    current_sha256=current,
                ).settlement
            if settled == "preimage":
                return {
                    "schema": "workflow-retirement-batch-report-v2",
                    "plan_sha256": plan.sha256,
                    "phase": "in_progress",
                    "settled": "preimage",
                    "effects_released": len(journal.state.get("effects", {})),
                }
            statuses[wid] = "verified"
            journal.persist(
                {
                    **journal.state,
                    "members": [
                        {"workflow_id": item["workflow_id"], "status": statuses[item["workflow_id"]]}
                        for item in parsed["members"]
                    ],
                }
            )
        final = _d9_read_inventory(parsed, state.get("inventory"))
        _d9_validate_batch_inventory(parsed, final, statuses, state["inventory"])
        journal.persist({**journal.state, "phase": "complete"})
        return {
            "schema": "workflow-retirement-batch-report-v2",
            "plan_sha256": plan.sha256,
            "phase": "complete",
            "members_verified": len(parsed["members"]),
        }


def _d9_validate_batch_inventory(
    parsed: Mapping[str, Any],
    inventory: Mapping[str, Any],
    statuses: Mapping[str, str],
    prepared: Mapping[str, Any],
) -> None:
    ids = [item["workflow_id"] for item in parsed["members"]]
    if inventory.get("database_schema") not in (None, parsed["schema"]):
        raise EffectBlocked("D9 database schema identity drifted")
    for member in parsed["members"]:
        wid = member["workflow_id"]
        live = inventory.get(wid)
        if not isinstance(live, dict):
            raise EffectBlocked(f"D9 member inventory is missing for {wid}")
        if statuses.get(wid) == "verified":
            if live.get("workflow", {}).get("present", True):
                raise EffectBlocked(f"D9 verified workflow {wid} reappeared")
            continue
        _d9_member_live_gate(member, live)
        old = prepared.get(wid) if isinstance(prepared, Mapping) else None
        if isinstance(old, Mapping):
            old_copy = dict(old)
            old_copy.pop("preview", None)
            current_copy = dict(live)
            current_copy.pop("preview", None)
            if canonical_sha256(current_copy) != canonical_sha256(old_copy):
                raise EffectBlocked(f"D9 pending member preimage drifted for {wid}")


def _validate_d9_postimage(
    member: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    if after.get("workflow", {}).get("present", False):
        raise EffectBlocked("D9 exact absence was not proved")
    before_sqlite = before.get("sqlite", {})
    after_sqlite = after.get("sqlite", {})
    before_sessions = before_sqlite.get("sessions", []) if isinstance(before_sqlite, dict) else []
    after_sessions = after_sqlite.get("sessions", []) if isinstance(after_sqlite, dict) else []
    before_by_id = {
        row.get("session_id"): row
        for row in before_sessions
        if isinstance(row, dict)
    }
    after_by_id = {
        row.get("session_id"): row
        for row in after_sessions
        if isinstance(row, dict)
    }
    for session_id, old in before_by_id.items():
        new = after_by_id.get(session_id)
        if new is None or old.get("present") != new.get("present"):
            raise EffectBlocked("retained Session identity changed")
        if old.get("present"):
            old_row = dict(old.get("row") or {})
            new_row = dict(new.get("row") or {})
            for key in set(old_row) | set(new_row):
                if key in {"task_id", "taskId"}:
                    if new_row.get(key) not in {None, ""}:
                        raise EffectBlocked("Session task association did not cascade to NULL")
                elif old_row.get(key) != new_row.get(key):
                    raise EffectBlocked("non-association Session field changed")
    deleted_task_ids = {
        task["id"] for task in member.get("tasks", []) if isinstance(task, dict)
    }
    before_associations = (
        before_sqlite.get("associations", []) if isinstance(before_sqlite, dict) else []
    )
    after_associations = (
        after_sqlite.get("associations", []) if isinstance(after_sqlite, dict) else []
    )
    expected_associations = [
        row
        for row in before_associations
        if row.get("task_id") not in deleted_task_ids
    ]
    if canonical_sha256(after_associations) != canonical_sha256(expected_associations):
        raise EffectBlocked("Session workflow-node association delta is not exact")
    for session in member.get("sessions", []):
        if session.get("retained") is True and (
            session.get("path") or session.get("relative")
        ):
            expected = session.get("manifest")
            actual = _session_manifest(
                _session_path(session, [Path(session.get("root") or Path(session["path"]).parent)])
            )
            if expected is not None and canonical_sha256(actual) != canonical_sha256(expected):
                raise EffectBlocked("retained Session filesystem manifest changed")


def _workflow_gate(item: Mapping[str, Any], label: str) -> None:
    for task in _bounded_list(item.get("tasks", []), f"{label}.tasks"):
        if not isinstance(task, dict) or task.get("terminal") is not True:
            raise PlanValidationError(f"{label} has a nonterminal task")
        if task.get("current_node") not in (None, ""):
            raise PlanValidationError(f"{label} has an active Current Node")
        if task.get("approval_pending") is True:
            raise PlanValidationError(f"{label} has a pending approval")
    if _bounded_list(item.get("current_nodes", []), f"{label}.current_nodes"):
        raise PlanValidationError(f"{label} has active Current Nodes")
    if _bounded_list(item.get("pending_approvals", []), f"{label}.pending_approvals"):
        raise PlanValidationError(f"{label} has pending approvals")


def _terminal_gate(item: Mapping[str, Any], label: str) -> None:
    expected_tasks = item.get("terminal_tasks")
    expected_anchors = item.get("terminal_anchors")
    if expected_tasks is not None:
        _bounded_list(expected_tasks, f"{label}.terminal_tasks")
    if expected_anchors is not None:
        _bounded_list(expected_anchors, f"{label}.terminal_anchors")
    graph = item.get("graph")
    if isinstance(graph, dict) and expected_anchors is not None:
        nodes = {
            node.get("id"): node.get("kind")
            for node in _bounded_list(graph.get("nodes", []), f"{label}.graph.nodes")
            if isinstance(node, dict)
        }
        for anchor in expected_anchors:
            if not isinstance(anchor, dict):
                raise PlanValidationError(f"{label}.terminal_anchors contains a non-object")
            anchor_id = _string(anchor.get("id"), "terminal anchor id")
            if nodes.get(anchor_id) != anchor.get("kind"):
                raise PlanValidationError("target graph does not preserve a terminal anchor")


def _compare_invariants(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    label: str,
) -> None:
    for key in ("links", "default", "project_id", "revision", "expected_version"):
        if key in expected and observed.get(key) != expected[key]:
            raise EffectBlocked(f"{label} invariant changed: {key}")


def _compare_terminal_inventory(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    label: str,
) -> None:
    for key in ("terminal_tasks", "terminal_anchors"):
        if key in expected:
            actual = observed.get(key)
            if canonical_sha256(actual) != canonical_sha256(expected[key]):
                raise EffectBlocked(f"{label} terminal inventory changed: {key}")


def _validate_canonical_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(
        plan.value,
        {"schema", "state_dir", "kent", "workflows", "d9"},
        "canonical plan",
    )
    state_dir = _absolute_path(_required(data, "state_dir", "canonical plan"), "state_dir")
    kent = _closed(_required(data, "kent", "canonical plan"), {"path", "sha256"}, "kent")
    path = _kent_path(_required(kent, "path", "kent"), "kent.path")
    digest = _sha256(_required(kent, "sha256", "kent"), "kent.sha256")
    _verify_executable(path, digest)
    dependency = _closed(_required(data, "d9", "canonical plan"), {"none", "path", "sha256", "operation", "phase", "members"}, "d9")
    if dependency.get("none") is True:
        if set(dependency) != {"none"}:
            raise PlanValidationError("canonical d9 none marker must be closed")
        d9 = None
    else:
        if dependency.get("none") is not False:
            raise PlanValidationError("canonical d9 dependency must be explicit")
        d9 = {
            "path": _absolute_path(_required(dependency, "path", "d9"), "d9.path"),
            "sha256": _sha256(_required(dependency, "sha256", "d9"), "d9.sha256"),
            "operation": _string(_required(dependency, "operation", "d9"), "d9.operation"),
            "phase": _string(_required(dependency, "phase", "d9"), "d9.phase"),
            "members": [_uuid(value, "d9.member") for value in _bounded_list(
                _required(dependency, "members", "d9"), "d9.members"
            )],
        }
        if d9["phase"] != "complete":
            raise PlanValidationError("canonical d9 dependency is not complete")
        _unique(d9["members"], "d9 members")
        if d9["operation"] != "workflow-retirement-batch":
            raise PlanValidationError("canonical d9 operation is unsupported")
    workflows = _bounded_list(_required(data, "workflows", "canonical plan"), "workflows")
    if not workflows:
        raise PlanValidationError("canonical plan has no workflows")
    parsed = []
    for index, raw in enumerate(workflows):
        _reject_raw_protocol_fields(raw, f"workflows[{index}]")
        item = _closed(
            raw,
            {
                "workflow_id", "project_id", "intent", "expected_version", "graph",
                "metadata", "terminal_tasks", "terminal_anchors", "links", "default",
            },
            f"workflows[{index}]",
        )
        item = dict(item)
        item["workflow_id"] = _uuid(_required(item, "workflow_id", "workflow_id"), "workflow_id")
        _uuid(_required(item, "project_id", "workflow.project_id"), "workflow.project_id")
        intent = _string(_required(item, "intent", "intent"), "intent")
        if intent not in {"graph-only", "metadata-only", "graph-and-metadata"}:
            raise PlanValidationError("canonical intent is not typed")
        version = _required(item, "expected_version", "workflow.expected_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise PlanValidationError("workflow.expected_version must be a non-negative integer")
        if intent in {"graph-only", "graph-and-metadata"}:
            graph = _closed(_required(item, "graph", "workflow"), {"version", "nodes", "edges"}, "workflow.graph")
            if graph.get("version") != version:
                raise PlanValidationError("target graph version must equal expected_version")
            _bounded_list(_required(graph, "nodes", "workflow.graph"), "workflow.graph.nodes")
            _bounded_list(_required(graph, "edges", "workflow.graph"), "workflow.graph.edges")
            item["graph"] = graph
        elif "graph" in item:
            raise PlanValidationError("metadata-only workflow may not carry a graph")
        if intent in {"metadata-only", "graph-and-metadata"}:
            metadata = _closed(_required(item, "metadata", "workflow"), {"name", "description", "execution_target"}, "workflow.metadata")
            _string(_required(metadata, "name", "workflow.metadata"), "metadata.name")
            _string(_required(metadata, "description", "workflow.metadata"), "metadata.description", nonempty=False)
            _string(_required(metadata, "execution_target", "workflow.metadata"), "metadata.execution_target")
            item["metadata"] = metadata
        elif "metadata" in item:
            raise PlanValidationError("graph-only workflow may not carry metadata")
        for key in ("terminal_tasks", "terminal_anchors", "links"):
            _bounded_list(_required(item, key, f"workflow.{key}"), f"workflow.{key}")
        if not isinstance(_required(item, "default", "workflow"), (str, type(None))):
            raise PlanValidationError("workflow.default must be a string or null")
        parsed.append(item)
    _unique([item["workflow_id"] for item in parsed], "canonical workflow identities")
    if d9 and not set(item["workflow_id"] for item in parsed).issubset(set(d9["members"])):
        raise PlanValidationError("canonical workflows are not covered by D9 dependency")
    return {
        "state_dir": state_dir,
        "kent": path,
        "kent_sha256": digest,
        "workflows": parsed,
        "d9": d9,
    }


def _canonical_read(parsed: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    wid = item["workflow_id"]
    project = item["project_id"]
    root = Path.cwd()
    workflow = _kent_json(parsed["kent"], ["workflow", "inspect", wid, "--json"], cwd=root)
    graph = _kent_json(parsed["kent"], ["workflow", "graph", "inspect", wid, "--json"], cwd=root)
    validation = _kent_json(parsed["kent"], ["workflow", "validate", wid, "--json"], cwd=root)
    tasks = _kent_pages(parsed["kent"], ["task", "list", "--project", project, "--workflow", wid], cwd=root)
    task_rows = _page_rows(tasks, ("items", "tasks"))
    details = []
    for task in task_rows:
        task_id = task.get("id", task.get("task_id"))
        if isinstance(task_id, str):
            details.append(_kent_json(parsed["kent"], ["task", "show", task_id, "--project", project, "--json"], cwd=root))
    observed = dict(workflow)
    observed.update(
        {
            "workflow_id": wid,
            "project_id": project,
            "graph": graph.get("graph", graph),
            "validation": validation,
            "tasks": task_rows,
            "task_details": details,
            "links": workflow.get("links", []),
            "default": workflow.get("default"),
            "current_nodes": workflow.get("current_nodes", []),
            "pending_approvals": workflow.get("pending_approvals", []),
            "terminal_tasks": [
                {"id": row.get("id", row.get("task_id")), "status": row.get("status")}
                for row in task_rows if row.get("terminal") is True
            ],
        }
    )
    return observed


def _canonical_effects(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    restore: bool = False,
    confirm: bool = True,
    graph_document: Mapping[str, Any] | None = None,
) -> list[list[str]]:
    wid = item["workflow_id"]
    metadata = item.get("metadata") or {}
    graph = graph_document if graph_document is not None else item.get("graph") or {}
    commands: list[list[str]] = []
    if item["intent"] in {"graph-only", "graph-and-metadata"}:
        commands.append([str(parsed["kent"]), "workflow", "graph", "apply", "-"])
        if confirm:
            commands[-1].append("--confirm")
        commands[-1].append("--json")
    if item["intent"] in {"metadata-only", "graph-and-metadata"}:
        commands.append(
            [
                str(parsed["kent"]),
                "workflow",
                "update",
                wid,
                "--name",
                _string(metadata.get("name", ""), "metadata.name"),
                "--description",
                _string(metadata.get("description", ""), "metadata.description", nonempty=False),
                "--execution-target",
                _string(metadata.get("execution_target", "none"), "metadata.execution_target"),
                "--json",
            ]
        )
    return commands


def reconcile_canonical_workflows(
    plan: LoadedPlan,
    *,
    mode: str,
    confirm: bool | str = False,
    kent: str | Path | None = None,
) -> dict[str, Any]:
    parsed = _validate_canonical_plan(plan)
    if kent is not None and str(kent) != str(parsed["kent"]):
        raise PlanValidationError("runtime --kent differs from the plan-bound executable")
    if mode not in {"prepare", "apply", "rollback"}:
        raise PlanValidationError("canonical mode must be prepare, apply, or rollback")
    if mode in {"apply", "rollback"} and confirm not in (True, plan.sha256):
        raise PlanValidationError("canonical mutation requires explicit confirmation")
    with OperationJournal(parsed["state_dir"], "canonical-workflow-reconcile", plan) as journal:
        _verify_executable(parsed["kent"], parsed["kent_sha256"])
        if mode == "prepare":
            if journal.state is not None:
                raise JournalError("canonical prepare refuses an existing journal")
            live = [_canonical_read(parsed, item) for item in parsed["workflows"]]
            for item, observed in zip(parsed["workflows"], live):
                _workflow_gate(observed, f"live workflow {item['workflow_id']}")
                _compare_invariants(item, observed, item["workflow_id"])
                _compare_terminal_inventory(item, observed, item["workflow_id"])
            journal.persist(
                {
                    "phase": "prepared",
                    "preimage": live,
                    "members": [
                        {"workflow_id": item["workflow_id"], "status": "prepared"}
                        for item in parsed["workflows"]
                    ],
                    "effects": {},
                }
            )
            return {"schema": "canonical-workflow-report-v2", "phase": "prepared", "effects_released": 0}
        state = journal.require_phase({"prepared", "in_progress", "complete"})
        if mode == "rollback":
            if state["phase"] == "prepared":
                journal.persist({**state, "phase": "rolled_back"})
                return {"schema": "canonical-workflow-report-v2", "phase": "rolled_back", "effects_released": 0}
            if state["phase"] == "complete":
                raise JournalError("canonical rollback after completion requires a new restore plan")
            for item in parsed["workflows"]:
                for number, command in enumerate(
                    _canonical_effects(parsed, item, restore=True)
                ):
                    run_effect(journal, effect_key=f"rollback:{item['workflow_id']}:{number}",
                               command=command, cwd=Path.cwd(),
                               stdin_bytes=canonical_bytes(
                                   (item.get("preimage") or {}).get("graph", {})
                               ) if "graph" in command else None)
            journal.persist({**journal.state, "phase": "rolled_back"})
            return {"schema": "canonical-workflow-report-v2", "phase": "rolled_back"}
        for item in parsed["workflows"]:
            live = _canonical_read(parsed, item)
            _workflow_gate(live, f"live workflow {item['workflow_id']}")
            _compare_invariants(item, live, item["workflow_id"])
            _compare_terminal_inventory(item, live, item["workflow_id"])
        journal.persist({**state, "phase": "in_progress"})
        for item in parsed["workflows"]:
            for number, command in enumerate(_canonical_effects(parsed, item)):
                run_effect(journal, effect_key=f"apply:{item['workflow_id']}:{number}",
                           command=command, cwd=Path.cwd(),
                           stdin_bytes=(
                               canonical_bytes(item.get("graph", {}))
                               if "graph" in command and "apply" in command
                               else None
                           ))
            observed = _canonical_read(parsed, item)
            _compare_invariants(item, observed, item["workflow_id"])
            _compare_terminal_inventory(item, observed, item["workflow_id"])
            if item.get("postimage") and canonical_sha256(observed) != _sha256(item["postimage"], "postimage"):
                raise EffectBlocked("canonical postimage mismatch")
        journal.persist({**journal.state, "phase": "complete"})
        return {"schema": "canonical-workflow-report-v2", "phase": "complete",
                "effects_released": len(journal.state.get("effects", {}))}


def _canonical_version(snapshot: Mapping[str, Any]) -> int:
    for key in ("version", "revision", "graph_version", "expected_version"):
        value = snapshot.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    graph = snapshot.get("graph")
    if isinstance(graph, dict) and isinstance(graph.get("version"), int):
        return graph["version"]
    raise EffectBlocked("canonical workflow version is absent")


def _canonical_target_graph(item: Mapping[str, Any], expected_version: int) -> dict[str, Any]:
    graph = dict(item.get("graph") or {})
    graph["expected_version"] = expected_version
    return graph


def _canonical_d9_check(dependency: Mapping[str, Any] | None) -> None:
    if dependency is None:
        return
    path = Path(dependency["path"])
    if path.is_symlink() or not path.is_file():
        raise EffectBlocked("canonical D9 journal is absent or unsafe")
    if sha256_bytes(path.read_bytes()) != dependency["sha256"]:
        raise EffectBlocked("canonical D9 journal digest drifted")
    try:
        value = json.loads(path.read_text(), object_pairs_hook=_duplicate_free)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EffectBlocked("canonical D9 journal is not readable") from error
    if not isinstance(value, dict) or value.get("operation") != dependency["operation"]:
        raise EffectBlocked("canonical D9 journal operation drifted")
    if value.get("phase") != "complete":
        raise EffectBlocked("canonical D9 journal is not complete")
    member_rows = {
        row.get("workflow_id"): row
        for row in value.get("members", [])
        if isinstance(row, dict)
    }
    for workflow_id in dependency["members"]:
        if member_rows.get(workflow_id, {}).get("status") != "verified":
            raise EffectBlocked("canonical D9 member is not verified")


def _canonical_validate_live(
    item: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    expected_version: int | None = None,
) -> None:
    if snapshot.get("present") is False:
        raise EffectBlocked("canonical workflow is absent")
    if expected_version is not None and _canonical_version(snapshot) != expected_version:
        raise EffectBlocked("canonical workflow version drifted")
    if snapshot.get("current_nodes"):
        raise EffectBlocked("canonical workflow has an active Current Node")
    if snapshot.get("pending_approvals"):
        raise EffectBlocked("canonical workflow has a pending approval")
    tasks = snapshot.get("tasks", [])
    if not isinstance(tasks, list):
        raise EffectBlocked("canonical task inventory is not a list")
    for task in tasks:
        if not isinstance(task, dict) or task.get("terminal") is not True:
            raise EffectBlocked("canonical workflow has a nonterminal task")
    if canonical_sha256(snapshot.get("links", [])) != canonical_sha256(item["links"]):
        raise EffectBlocked("canonical workflow links drifted")
    if canonical_sha256(snapshot.get("default")) != canonical_sha256(item["default"]):
        raise EffectBlocked("canonical workflow default drifted")
    expected_terminal = item["terminal_tasks"]
    actual_terminal = snapshot.get("terminal_tasks", [])
    if canonical_sha256(actual_terminal) != canonical_sha256(expected_terminal):
        raise EffectBlocked("canonical terminal task inventory drifted")


def _canonical_expected_post(
    before: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    graph: bool,
    metadata: bool,
) -> dict[str, Any]:
    expected = dict(before)
    if graph:
        expected["graph"] = item["graph"]
        expected["version"] = _canonical_version(before) + 1
    if metadata:
        expected["metadata"] = item["metadata"]
    return expected


def _canonical_state_digest(snapshot: Mapping[str, Any]) -> str:
    return canonical_sha256(snapshot)


def _validate_canonical_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(
        plan.value, {"schema", "state_dir", "kent", "d9", "workflows"}, "canonical plan"
    )
    state_dir = _absolute_path(_required(data, "state_dir", "canonical plan"), "state_dir")
    kent = _closed(_required(data, "kent", "canonical plan"), {"path", "sha256"}, "kent")
    kent_path = _kent_path(_required(kent, "path", "kent"), "kent.path")
    kent_sha = _sha256(_required(kent, "sha256", "kent"), "kent.sha256")
    _verify_executable(kent_path, kent_sha)
    dependency_raw = _closed(_required(data, "d9", "canonical plan"), {"none", "path", "sha256", "operation", "phase", "members"}, "d9")
    if dependency_raw.get("none") is True:
        if set(dependency_raw) != {"none"}:
            raise PlanValidationError("canonical d9 none marker is not closed")
        dependency = None
    else:
        if dependency_raw.get("none") is not False:
            raise PlanValidationError("canonical d9 dependency must declare none=false")
        dependency = {
            "path": _absolute_path(_required(dependency_raw, "path", "d9"), "d9.path"),
            "sha256": _sha256(_required(dependency_raw, "sha256", "d9"), "d9.sha256"),
            "operation": _string(_required(dependency_raw, "operation", "d9"), "d9.operation"),
            "phase": _string(_required(dependency_raw, "phase", "d9"), "d9.phase"),
            "members": [_uuid(value, "d9.member") for value in _bounded_list(
                _required(dependency_raw, "members", "d9"), "d9.members"
            )],
        }
        if dependency["operation"] != "workflow-retirement-batch" or dependency["phase"] != "complete":
            raise PlanValidationError("canonical d9 dependency is unsupported")
    workflows: list[dict[str, Any]] = []
    for index, raw in enumerate(_bounded_list(_required(data, "workflows", "canonical plan"), "workflows")):
        _reject_raw_protocol_fields(raw, f"workflows[{index}]")
        item = _closed(
            raw,
            {"workflow_id", "project_id", "intent", "expected_version", "graph",
             "metadata", "terminal_tasks", "terminal_anchors", "links", "default"},
            f"workflows[{index}]",
        )
        item = dict(item)
        item["workflow_id"] = _uuid(_required(item, "workflow_id", "workflow"), "workflow_id")
        item["project_id"] = _uuid(_required(item, "project_id", "workflow"), "project_id")
        intent = _string(_required(item, "intent", "workflow"), "intent")
        if intent not in {"graph-only", "metadata-only", "graph-and-metadata"}:
            raise PlanValidationError("canonical intent is unsupported")
        version = _required(item, "expected_version", "workflow")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise PlanValidationError("canonical expected_version must be a non-negative integer")
        for key in ("terminal_tasks", "terminal_anchors", "links"):
            _bounded_list(_required(item, key, "workflow"), f"workflow.{key}")
        if not isinstance(_required(item, "default", "workflow"), (str, type(None))):
            raise PlanValidationError("canonical default must be a string or null")
        if intent in {"graph-only", "graph-and-metadata"}:
            graph = _closed(_required(item, "graph", "workflow"), {"version", "nodes", "edges"}, "workflow.graph")
            if graph["version"] != version + 1:
                raise PlanValidationError("target graph must advance exactly one version")
            _bounded_list(_required(graph, "nodes", "workflow.graph"), "workflow.graph.nodes")
            _bounded_list(_required(graph, "edges", "workflow.graph"), "workflow.graph.edges")
            item["graph"] = graph
        elif "graph" in item:
            raise PlanValidationError("metadata-only workflow cannot carry graph")
        if intent in {"metadata-only", "graph-and-metadata"}:
            metadata = _closed(_required(item, "metadata", "workflow"), {"name", "description", "execution_target"}, "workflow.metadata")
            _string(_required(metadata, "name", "workflow.metadata"), "metadata.name")
            _string(_required(metadata, "description", "workflow.metadata"), "metadata.description", nonempty=False)
            _string(_required(metadata, "execution_target", "workflow.metadata"), "metadata.execution_target")
            item["metadata"] = metadata
        elif "metadata" in item:
            raise PlanValidationError("graph-only workflow cannot carry metadata")
        workflows.append(item)
    if not workflows:
        raise PlanValidationError("canonical plan has no workflows")
    _unique([item["workflow_id"] for item in workflows], "canonical workflow identities")
    if dependency and not set(item["workflow_id"] for item in workflows).issubset(set(dependency["members"])):
        raise PlanValidationError("canonical workflows are not covered by D9")
    return {"state_dir": state_dir, "kent": kent_path, "kent_sha256": kent_sha, "d9": dependency, "workflows": workflows}


def _canonical_read(parsed: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    kent = parsed["kent"]
    root = Path.cwd()
    wid = item["workflow_id"]
    project = item["project_id"]
    workflow = _kent_json(kent, ["workflow", "inspect", wid, "--json"], cwd=root)
    graph = _kent_json(kent, ["workflow", "graph", "inspect", wid, "--json"], cwd=root)
    validation = _kent_json(kent, ["workflow", "validate", wid, "--json"], cwd=root)
    task_pages = _kent_pages(kent, ["task", "list", "--project", project, "--workflow", wid], cwd=root)
    tasks = _page_rows(task_pages, ("items", "tasks"))
    details = []
    for task in tasks:
        task_id = task.get("id", task.get("task_id"))
        if isinstance(task_id, str):
            details.append(_kent_json(kent, ["task", "show", task_id, "--project", project, "--json"], cwd=root))
    result = dict(workflow)
    result.update({
        "workflow_id": wid,
        "project_id": project,
        "graph": graph.get("graph", graph),
        "validation": validation,
        "tasks": tasks,
        "task_details": details,
        "links": workflow.get("links", []),
        "default": workflow.get("default"),
        "current_nodes": workflow.get("current_nodes", []),
        "pending_approvals": workflow.get("pending_approvals", []),
        "terminal_tasks": [
            {"id": task.get("id", task.get("task_id")), "status": task.get("status")}
            for task in tasks if task.get("terminal") is True
        ],
    })
    result["version"] = _canonical_version(result)
    return result


def _canonical_effects(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    restore: bool = False,
    confirm: bool = True,
    graph_document: Mapping[str, Any] | None = None,
) -> list[list[str]]:
    del restore
    commands: list[list[str]] = []
    if item["intent"] in {"graph-only", "graph-and-metadata"}:
        command = [str(parsed["kent"]), "workflow", "graph", "apply", "-"]
        if confirm:
            command.append("--confirm")
        command.append("--json")
        commands.append(command)
    if item["intent"] in {"metadata-only", "graph-and-metadata"}:
        metadata = item["metadata"]
        commands.append([
            str(parsed["kent"]), "workflow", "update", item["workflow_id"],
            "--name", metadata["name"], "--description", metadata["description"],
            "--execution-target", metadata["execution_target"], "--json",
        ])
    return commands


def reconcile_canonical_workflows(
    plan: LoadedPlan,
    *,
    mode: str,
    confirm: bool | str = False,
    kent: str | Path | None = None,
) -> dict[str, Any]:
    parsed = _validate_canonical_plan(plan)
    if kent is not None and str(kent) != str(parsed["kent"]):
        raise PlanValidationError("runtime --kent differs from plan executable")
    if mode not in {"prepare", "apply", "rollback"}:
        raise PlanValidationError("canonical mode is unsupported")
    if mode in {"apply", "rollback"} and confirm not in (True, plan.sha256):
        raise PlanValidationError("canonical mutation requires confirmation")
    _canonical_d9_check(parsed["d9"])
    with OperationJournal(parsed["state_dir"], "canonical-workflow-reconcile", plan) as journal:
        if mode == "prepare":
            if journal.state is not None:
                raise JournalError("canonical prepare refuses an existing journal")
            live = [_canonical_read(parsed, item) for item in parsed["workflows"]]
            for item, observed in zip(parsed["workflows"], live):
                _canonical_validate_live(item, observed, expected_version=item["expected_version"])
            journal.persist({
                "phase": "prepared",
                "preimage": live,
                "members": [{"workflow_id": item["workflow_id"], "status": "prepared"} for item in parsed["workflows"]],
                "effects": {},
            })
            return {"schema": "canonical-workflow-report-v2", "phase": "prepared", "effects_released": 0}
        state = journal.require_phase({"prepared", "in_progress", "complete"})
        if mode == "rollback":
            if state["phase"] != "prepared":
                raise JournalError("canonical rollback is allowed only from prepared")
            journal.persist({**state, "phase": "rolled_back"})
            return {"schema": "canonical-workflow-report-v2", "phase": "rolled_back", "effects_released": 0}
        if state["phase"] == "complete":
            return {"schema": "canonical-workflow-report-v2", "phase": "complete", "resumed": True}
        journal.persist({**state, "phase": "in_progress"})
        for item in parsed["workflows"]:
            wid = item["workflow_id"]
            before = _canonical_read(parsed, item)
            _canonical_validate_live(item, before, expected_version=item["expected_version"])
            prepared = next(row for row in state["preimage"] if row["workflow_id"] == wid)
            if _canonical_state_digest(before) != _canonical_state_digest(prepared):
                raise EffectBlocked("canonical prepared preimage drifted")
            graph_document = dict(item.get("graph") or {})
            graph_document["expected_version"] = item["expected_version"]
            commands = _canonical_effects(parsed, item, graph_document=graph_document)
            for number, command in enumerate(commands):
                is_graph = "graph" in command
                is_metadata = "update" in command
                expected = _canonical_expected_post(
                    before, item, graph=is_graph, metadata=is_metadata
                )
                pre_hash = _canonical_state_digest(before)
                post_hash = _canonical_state_digest(expected)

                def current(
                    before=before, expected=expected, is_graph=is_graph, is_metadata=is_metadata
                ) -> str:
                    latest = _canonical_read(parsed, item)
                    _canonical_validate_live(item, latest)
                    if is_graph:
                        if latest.get("graph") != item["graph"] or _canonical_version(latest) != item["graph"]["version"]:
                            if _canonical_state_digest(latest) == _canonical_state_digest(before):
                                return pre_hash
                            raise EffectBlocked("canonical graph postimage is not exact")
                    if is_metadata and latest.get("metadata") != item["metadata"]:
                        if _canonical_state_digest(latest) == _canonical_state_digest(before):
                            return pre_hash
                        raise EffectBlocked("canonical metadata postimage is not exact")
                    if is_graph and latest.get("graph") != item["graph"]:
                        raise EffectBlocked("canonical graph postimage is not exact")
                    return post_hash

                key = f"apply:{wid}:{number}"
                existing = (journal.state.get("effects") or {}).get(key)
                if isinstance(existing, dict) and existing.get("status") in {"attempted", "unresolved", "failed", "ambiguous"}:
                    settled = recover_effect(
                        journal, effect_key=key, preimage_sha256=pre_hash,
                        postimage_sha256=post_hash, current_sha256=current,
                    )
                else:
                    settled = run_effect(
                        journal, effect_key=key, command=command, cwd=Path.cwd(),
                        preimage_sha256=pre_hash, postimage_sha256=post_hash,
                        stdin_bytes=canonical_bytes(graph_document) if is_graph else None,
                        current_sha256=current,
                    ).settlement
                if settled == "preimage":
                    return {"schema": "canonical-workflow-report-v2", "phase": "in_progress", "settled": "preimage", "effects_released": len(journal.state.get("effects", {}))}
                before = _canonical_read(parsed, item)
        journal.persist({**journal.state, "phase": "complete"})
        return {"schema": "canonical-workflow-report-v2", "phase": "complete", "effects_released": len(journal.state.get("effects", {}))}


def _validate_canonical_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(
        plan.value, {"schema", "state_dir", "kent", "d9", "workflows"}, "canonical plan"
    )
    state_dir = _absolute_path(_required(data, "state_dir", "canonical plan"), "state_dir")
    kent = _closed(_required(data, "kent", "canonical plan"), {"path", "sha256"}, "kent")
    kent_path = _kent_path(_required(kent, "path", "kent"), "kent.path")
    kent_sha = _sha256(_required(kent, "sha256", "kent"), "kent.sha256")
    _verify_executable(kent_path, kent_sha)
    raw_d9 = _closed(_required(data, "d9", "canonical plan"), {"none", "path", "sha256", "operation", "phase", "members"}, "d9")
    d9: dict[str, Any] | None
    if raw_d9.get("none") is True:
        if set(raw_d9) != {"none"}:
            raise PlanValidationError("canonical d9 none marker is not closed")
        d9 = None
    else:
        if raw_d9.get("none") is not False:
            raise PlanValidationError("canonical d9 dependency must explicitly declare none")
        d9 = {
            "path": _absolute_path(_required(raw_d9, "path", "d9"), "d9.path"),
            "sha256": _sha256(_required(raw_d9, "sha256", "d9"), "d9.sha256"),
            "operation": _string(_required(raw_d9, "operation", "d9"), "d9.operation"),
            "phase": _string(_required(raw_d9, "phase", "d9"), "d9.phase"),
            "members": [_uuid(value, "d9.member") for value in _bounded_list(
                _required(raw_d9, "members", "d9"), "d9.members"
            )],
        }
        if d9["operation"] != "workflow-retirement-batch" or d9["phase"] != "complete":
            raise PlanValidationError("canonical d9 dependency is unsupported")
    workflows: list[dict[str, Any]] = []
    for index, raw in enumerate(_bounded_list(_required(data, "workflows", "canonical plan"), "workflows")):
        _reject_raw_protocol_fields(raw, f"workflows[{index}]")
        item = dict(_closed(
            raw,
            {
                "workflow_id", "project_id", "intent", "expected_version", "graph",
                "metadata", "terminal_tasks", "terminal_anchors", "links", "default",
            },
            f"workflows[{index}]",
        ))
        item["workflow_id"] = _uuid(_required(item, "workflow_id", "workflow"), "workflow_id")
        item["project_id"] = _uuid(_required(item, "project_id", "workflow"), "project_id")
        intent = _string(_required(item, "intent", "workflow"), "intent")
        if intent not in {"graph-only", "metadata-only", "graph-and-metadata"}:
            raise PlanValidationError("canonical intent is unsupported")
        version = _required(item, "expected_version", "workflow")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise PlanValidationError("canonical expected_version must be a non-negative integer")
        for key in ("terminal_tasks", "terminal_anchors", "links"):
            _bounded_list(_required(item, key, "workflow"), f"workflow.{key}")
        if not isinstance(_required(item, "default", "workflow"), (str, type(None))):
            raise PlanValidationError("canonical default must be a string or null")
        if intent in {"graph-only", "graph-and-metadata"}:
            graph = dict(_closed(
                _required(item, "graph", "workflow"),
                {"version", "expected_version", "nodes", "edges"},
                "workflow.graph",
            ))
            if graph.get("expected_version", version) != version or graph["version"] != version + 1:
                raise PlanValidationError("target graph must advance exactly one version")
            _bounded_list(_required(graph, "nodes", "workflow.graph"), "workflow.graph.nodes")
            _bounded_list(_required(graph, "edges", "workflow.graph"), "workflow.graph.edges")
            item["graph"] = graph
        elif "graph" in item:
            raise PlanValidationError("metadata-only workflow cannot carry graph")
        if intent in {"metadata-only", "graph-and-metadata"}:
            metadata = _closed(
                _required(item, "metadata", "workflow"),
                {"name", "description", "execution_target"},
                "workflow.metadata",
            )
            _string(_required(metadata, "name", "workflow.metadata"), "metadata.name")
            _string(_required(metadata, "description", "workflow.metadata"), "metadata.description", nonempty=False)
            _string(_required(metadata, "execution_target", "workflow.metadata"), "metadata.execution_target")
            item["metadata"] = metadata
        elif "metadata" in item:
            raise PlanValidationError("graph-only workflow cannot carry metadata")
        workflows.append(item)
    if not workflows:
        raise PlanValidationError("canonical plan has no workflows")
    _unique([item["workflow_id"] for item in workflows], "canonical workflow identities")
    if d9 and not set(item["workflow_id"] for item in workflows).issubset(set(d9["members"])):
        raise PlanValidationError("canonical workflows are not covered by D9")
    return {"state_dir": state_dir, "kent": kent_path, "kent_sha256": kent_sha, "d9": d9, "workflows": workflows}


def _canonical_version(snapshot: Mapping[str, Any]) -> int:
    for key in ("version", "revision", "graph_version"):
        value = snapshot.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    graph = snapshot.get("graph")
    if isinstance(graph, dict) and isinstance(graph.get("version"), int):
        return graph["version"]
    raise EffectBlocked("canonical workflow version is absent")


def _canonical_read(parsed: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    kent = parsed["kent"]
    root = Path.cwd()
    wid = item["workflow_id"]
    project = item["project_id"]
    project_list = _kent_json(kent, ["project", "list", "--json"], cwd=root)
    workflow = _kent_json(kent, ["workflow", "inspect", wid, "--json"], cwd=root)
    graph_raw = _kent_json(kent, ["workflow", "graph", "inspect", wid, "--json"], cwd=root)
    validation = _kent_json(kent, ["workflow", "validate", wid, "--json"], cwd=root)
    task_pages = _kent_pages(kent, ["task", "list", "--project", project, "--workflow", wid], cwd=root)
    tasks = _page_rows(task_pages, ("items", "tasks"))
    details = []
    for task in tasks:
        task_id = task.get("id", task.get("task_id"))
        if isinstance(task_id, str):
            details.append(_kent_json(kent, ["task", "show", task_id, "--project", project, "--json"], cwd=root))
    graph = graph_raw.get("graph", graph_raw)
    if not isinstance(graph, dict):
        raise OperationError("canonical graph readback is not an object")
    result = dict(workflow)
    result.update(
        {
            "workflow_id": wid,
            "project_id": project,
            "project_list": project_list,
            "graph": graph,
            "validation": validation,
            "tasks": tasks,
            "task_details": details,
            "links": workflow.get("links", []),
            "default": workflow.get("default"),
            "current_nodes": workflow.get("current_nodes", []),
            "pending_approvals": workflow.get("pending_approvals", []),
            "terminal_tasks": [
                {"id": task.get("id", task.get("task_id")), "status": task.get("status")}
                for task in tasks if task.get("terminal") is True
            ],
            "version": workflow.get("version", workflow.get("revision", graph.get("version"))),
        }
    )
    return result


def _canonical_gate(item: Mapping[str, Any], snapshot: Mapping[str, Any], expected_version: int) -> None:
    if snapshot.get("present") is False:
        raise EffectBlocked("canonical workflow is absent")
    if _canonical_version(snapshot) != expected_version:
        raise EffectBlocked("canonical workflow version drifted")
    if snapshot.get("current_nodes") or snapshot.get("pending_approvals"):
        raise EffectBlocked("canonical workflow is not quiescent")
    project_list = snapshot.get("project_list", {})
    project_rows = (
        project_list.get("items", project_list.get("projects", []))
        if isinstance(project_list, dict)
        else []
    )
    if not isinstance(project_rows, list):
        project_rows = []
    if project_rows and not any(
        row.get("id", row.get("project_id")) == item["project_id"]
        for row in project_rows
    ):
        raise EffectBlocked("canonical project linkage is absent")
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list) or any(
        not isinstance(task, dict) or task.get("terminal") is not True for task in tasks
    ):
        raise EffectBlocked("canonical workflow has a nonterminal task")
    if canonical_sha256(snapshot.get("links", [])) != canonical_sha256(item["links"]):
        raise EffectBlocked("canonical links drifted")
    if canonical_sha256(snapshot.get("default")) != canonical_sha256(item["default"]):
        raise EffectBlocked("canonical default drifted")
    if canonical_sha256(snapshot.get("terminal_tasks", [])) != canonical_sha256(item["terminal_tasks"]):
        raise EffectBlocked("canonical terminal task inventory drifted")
    graph_nodes = {
        node.get("id"): node.get("kind")
        for node in snapshot.get("graph", {}).get("nodes", [])
        if isinstance(node, dict)
    }
    for anchor in item["terminal_anchors"]:
        if not isinstance(anchor, dict):
            raise EffectBlocked("canonical terminal anchor is not typed")
        if graph_nodes.get(anchor.get("id")) != anchor.get("kind"):
            raise EffectBlocked("canonical terminal anchor drifted")


def _canonical_effects(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    restore: bool = False,
    confirm: bool = True,
    graph_document: Mapping[str, Any] | None = None,
) -> list[list[str]]:
    del restore, graph_document
    commands: list[list[str]] = []
    if item["intent"] in {"graph-only", "graph-and-metadata"}:
        command = [str(parsed["kent"]), "workflow", "graph", "apply", "-"]
        if confirm:
            command.append("--confirm")
        command.append("--json")
        commands.append(command)
    if item["intent"] in {"metadata-only", "graph-and-metadata"}:
        metadata = item["metadata"]
        commands.append([
            str(parsed["kent"]), "workflow", "update", item["workflow_id"],
            "--name", metadata["name"], "--description", metadata["description"],
            "--execution-target", metadata["execution_target"], "--json",
        ])
    return commands


def _canonical_d9_check(dependency: Mapping[str, Any] | None) -> None:
    if dependency is None:
        return
    path = dependency["path"]
    if path.is_symlink() or not path.is_file() or sha256_bytes(path.read_bytes()) != dependency["sha256"]:
        raise EffectBlocked("canonical D9 journal is absent or drifted")
    try:
        value = json.loads(path.read_text(), object_pairs_hook=_duplicate_free)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EffectBlocked("canonical D9 journal is invalid") from error
    if value.get("operation") != dependency["operation"] or value.get("phase") != "complete":
        raise EffectBlocked("canonical D9 dependency is not complete")
    rows = {row.get("workflow_id"): row for row in value.get("members", []) if isinstance(row, dict)}
    if any(rows.get(member, {}).get("status") != "verified" for member in dependency["members"]):
        raise EffectBlocked("canonical D9 member is not verified")


def reconcile_canonical_workflows(
    plan: LoadedPlan,
    *,
    mode: str,
    confirm: bool | str = False,
    kent: str | Path | None = None,
) -> dict[str, Any]:
    parsed = _validate_canonical_plan(plan)
    if kent is not None and str(kent) != str(parsed["kent"]):
        raise PlanValidationError("runtime --kent differs from plan executable")
    if mode not in {"prepare", "apply", "rollback"}:
        raise PlanValidationError("canonical mode is unsupported")
    if mode in {"apply", "rollback"} and confirm not in (True, plan.sha256):
        raise PlanValidationError("canonical mutation requires confirmation")
    _canonical_d9_check(parsed["d9"])
    with OperationJournal(parsed["state_dir"], "canonical-workflow-reconcile", plan) as journal:
        if mode == "prepare":
            if journal.state is not None:
                raise JournalError("canonical prepare refuses an existing journal")
            live = [_canonical_read(parsed, item) for item in parsed["workflows"]]
            for item, snapshot in zip(parsed["workflows"], live):
                _canonical_gate(item, snapshot, item["expected_version"])
            journal.persist({
                "phase": "prepared",
                "preimage": live,
                "members": [{"workflow_id": item["workflow_id"], "status": "prepared"} for item in parsed["workflows"]],
                "effects": {},
            })
            return {"schema": "canonical-workflow-report-v2", "phase": "prepared", "effects_released": 0}
        state = journal.require_phase({"prepared", "in_progress", "complete"})
        if mode == "rollback":
            if state["phase"] != "prepared":
                raise JournalError("canonical rollback is allowed only from prepared")
            journal.persist({**state, "phase": "rolled_back"})
            return {"schema": "canonical-workflow-report-v2", "phase": "rolled_back", "effects_released": 0}
        if state["phase"] == "complete":
            return {"schema": "canonical-workflow-report-v2", "phase": "complete", "resumed": True}
        journal.persist({**state, "phase": "in_progress"})
        for item in parsed["workflows"]:
            wid = item["workflow_id"]
            before = _canonical_read(parsed, item)
            expected_version = item["expected_version"]
            _canonical_gate(item, before, expected_version)
            prepared = next(row for row in state["preimage"] if row["workflow_id"] == wid)
            if canonical_sha256(before) != canonical_sha256(prepared):
                raise EffectBlocked("canonical prepared preimage drifted")
            graph_doc = dict(item.get("graph") or {})
            graph_doc["expected_version"] = expected_version
            for number, command in enumerate(_canonical_effects(parsed, item, graph_document=graph_doc)):
                is_graph = "graph" in command
                pre_hash = canonical_sha256(before)
                target_graph = item.get("graph")
                target_version = target_graph["version"] if isinstance(target_graph, dict) else expected_version
                key = f"apply:{wid}:{number}"

                def current(
                    before=before, is_graph=is_graph, target_graph=target_graph,
                    target_version=target_version, item=item,
                ) -> str:
                    latest = _canonical_read(parsed, item)
                    _canonical_gate(item, latest, expected_version if is_graph else target_version)
                    if is_graph:
                        if latest.get("graph") != target_graph or _canonical_version(latest) != target_version:
                            if canonical_sha256(latest) == canonical_sha256(before):
                                return pre_hash
                            raise EffectBlocked("canonical graph readback is partial or ambiguous")
                    else:
                        if latest.get("name") != item["metadata"]["name"] and latest.get("metadata", {}).get("name") != item["metadata"]["name"]:
                            if canonical_sha256(latest) == canonical_sha256(before):
                                return pre_hash
                            raise EffectBlocked("canonical metadata readback is partial or ambiguous")
                    return target_hash

                target_hash = canonical_sha256(
                    {
                        **before,
                        "graph": target_graph if is_graph else before.get("graph"),
                        "version": target_version if is_graph else _canonical_version(before),
                        **({"name": item["metadata"]["name"], "description": item["metadata"]["description"], "execution_target": item["metadata"]["execution_target"]} if not is_graph else {}),
                    }
                )
                existing = (journal.state.get("effects") or {}).get(key)
                if isinstance(existing, dict) and existing.get("status") in {"attempted", "unresolved", "failed", "ambiguous"}:
                    settled = recover_effect(
                        journal, effect_key=key, preimage_sha256=pre_hash,
                        postimage_sha256=target_hash, current_sha256=current,
                    )
                else:
                    settled = run_effect(
                        journal, effect_key=key, command=command, cwd=Path.cwd(),
                        preimage_sha256=pre_hash, postimage_sha256=target_hash,
                        stdin_bytes=canonical_bytes(graph_doc) if is_graph else None,
                        current_sha256=current,
                    ).settlement
                if settled == "preimage":
                    return {"schema": "canonical-workflow-report-v2", "phase": "in_progress", "settled": "preimage", "effects_released": len(journal.state.get("effects", {}))}
                before = _canonical_read(parsed, item)
        journal.persist({**journal.state, "phase": "complete"})
        return {"schema": "canonical-workflow-report-v2", "phase": "complete", "effects_released": len(journal.state.get("effects", {}))}


def _validate_activation_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(
        plan.value,
        {
            "schema", "state_dir", "primary_root", "branch", "baseline_commit",
            "target_commit", "role", "git_config_allowlist", "tracking",
            "installed_links", "prompt_prestate", "backups", "source_prompt_sha256",
        },
        "activation plan",
    )
    if _string(_required(data, "branch", "activation plan"), "branch") != "main":
        raise PlanValidationError("primary activation is restricted to main")
    _sha1(_required(data, "baseline_commit", "activation plan"), "baseline_commit")
    _sha1(_required(data, "target_commit", "activation plan"), "target_commit")
    _absolute_path(_required(data, "state_dir", "activation plan"), "state_dir")
    _absolute_path(_required(data, "primary_root", "activation plan"), "primary_root")
    role = _closed(
        _required(data, "role", "activation plan"),
        {"prompt_path", "config_path", "kit_prompt_path", "expected_prompt_sha256"},
        "role",
    )
    for key in ("prompt_path", "config_path", "kit_prompt_path"):
        _absolute_path(_required(role, key, "role"), f"role.{key}")
    _sha256(_required(role, "expected_prompt_sha256", "role"), "role.expected_prompt_sha256")
    if not isinstance(_required(data, "git_config_allowlist", "activation plan"), dict):
        raise PlanValidationError("git_config_allowlist must be an object")
    if "backups" in data and not isinstance(data["backups"], dict):
        raise PlanValidationError("backups must be an object")
    if "installed_links" in data and not isinstance(data["installed_links"], list):
        raise PlanValidationError("installed_links must be a list")
    return data


def _activation_preflight(data: Mapping[str, Any]) -> dict[str, Any]:
    root = _absolute_path(data["primary_root"], "primary_root")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise OperationError("primary checkout root mismatch")
    if _git(root, "branch", "--show-current") != "main":
        raise OperationError("primary checkout is not on main")
    if _git(root, "status", "--porcelain"):
        raise OperationError("primary checkout is dirty")
    current = _git(root, "rev-parse", "HEAD")
    if current != data["baseline_commit"]:
        raise OperationError("primary baseline does not match the plan")
    _git(root, "merge-base", "--is-ancestor", data["baseline_commit"], data["target_commit"])
    main_ref = _git(root, "rev-parse", "refs/heads/main")
    if main_ref != data["baseline_commit"]:
        raise OperationError("local main ref does not match the baseline")
    if "tracking" in data:
        actual_tracking = _git(root, "config", "--get-regexp", r"^branch\.main\.")
        if actual_tracking != data["tracking"]:
            raise OperationError("local main tracking configuration drifted")
    if "git_config_allowlist" in data:
        allowed = data["git_config_allowlist"]
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in allowed.items()):
            raise PlanValidationError("git_config_allowlist must contain strings")
        actual_config = {}
        for key in allowed:
            actual_config[key] = _git(root, "config", "--get", key, check=False)
        if actual_config != allowed:
            raise OperationError("Git configuration is outside the approved allowlist")
    role = data["role"]
    prompt = Path(role["prompt_path"])
    if data.get("prompt_prestate") is not None:
        actual = {
            "exists": prompt.exists(),
            "symlink": prompt.is_symlink(),
            "target": str(prompt.resolve()) if prompt.is_symlink() else None,
        }
        if actual != data["prompt_prestate"]:
            raise OperationError("installed prompt prestate drifted")
    return {
        "root": str(root),
        "current_commit": current,
        "baseline_commit": data["baseline_commit"],
        "target_commit": data["target_commit"],
        "config_sha256": sha256_bytes(
            _git(root, "config", "--list", "--null").encode()
        ),
    }


def activate_primary_checkout(
    plan: LoadedPlan,
    *,
    mode: str,
    confirm: bool | str = False,
) -> dict[str, Any]:
    data = _validate_activation_plan(plan)
    if mode not in {"preview", "apply", "rollback"}:
        raise PlanValidationError("activation mode must be preview, apply, or rollback")
    if mode == "preview":
        return {"schema": "kit-primary-activation-report-v2", "phase": "preview",
                "plan_sha256": plan.sha256, "preflight": _activation_preflight(data),
                "effects_released": 0}
    if confirm not in (True, plan.sha256):
        raise PlanValidationError("activation mutation requires explicit confirmation")
    with OperationJournal(Path(data["state_dir"]), "kit-primary-activation", plan) as journal:
        if journal.state is None:
            journal.persist({"phase": "prepared", "preflight": _activation_preflight(data), "effects": {}})
        state = journal.require_phase({
            "prepared", "activation_committed", "primary_promoted", "role_adopted", "verified"
        })
        if mode == "rollback":
            if state["phase"] != "prepared":
                raise JournalError("activation rollback is allowed only from prepared")
            journal.persist({**state, "phase": "rolled_back"})
            return {"schema": "kit-primary-activation-report-v2", "phase": "rolled_back", "effects_released": 0}
        if state["phase"] == "prepared":
            journal.persist({**state, "phase": "activation_committed"})
            command = [
                "/usr/bin/git",
                "-C",
                str(data["primary_root"]),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-c",
                "maintenance.auto=false",
                "merge",
                "--ff-only",
                data["target_commit"],
            ]
            run_effect(journal, effect_key="primary-merge", command=command,
                       cwd=Path(data["primary_root"]),
                       preimage_sha256=canonical_sha256(
                           {"head": data["baseline_commit"]}
                       ),
                       postimage_sha256=canonical_sha256(
                           {"head": data["target_commit"]}
                       ),
                       extra_env={
                           "GIT_TERMINAL_PROMPT": "0",
                           "GIT_PAGER": "cat",
                           "GIT_CONFIG_NOSYSTEM": "1",
                           "GIT_CONFIG_GLOBAL": os.devnull,
                       })
            journal.persist({**journal.state, "phase": "primary_promoted"})
            state = journal.state
        elif state["phase"] == "activation_committed":
            def current_head() -> str:
                return canonical_sha256({"head": _git(Path(data["primary_root"]), "rev-parse", "HEAD")})

            effect = (state.get("effects") or {}).get("primary-merge")
            if not isinstance(effect, dict):
                raise JournalError("activation effect is missing")
            try:
                settled = recover_effect(
                    journal,
                    effect_key="primary-merge",
                    preimage_sha256=canonical_sha256(
                        {"head": data["baseline_commit"]}
                    ),
                    postimage_sha256=canonical_sha256(
                        {"head": data["target_commit"]}
                    ),
                    current_sha256=current_head,
                )
            except EffectBlocked:
                raise
            if settled == "preimage":
                return {
                    "schema": "kit-primary-activation-report-v2",
                    "phase": "activation_committed",
                    "settled": "preimage",
                    "effects_released": 0,
                }
            journal.persist({**journal.state, "phase": "primary_promoted"})
            state = journal.state
        if state["phase"] == "primary_promoted":
            role = data["role"]
            prompt = Path(role["prompt_path"])
            kit_prompt = Path(role["kit_prompt_path"])
            expected = role["expected_prompt_sha256"]
            if prompt.exists() and not prompt.is_symlink():
                if sha256_bytes(prompt.read_bytes()) != expected:
                    raise OperationError("existing release-decision prompt is not byte-identical")
                backup = prompt.with_name(prompt.name + ".release-decision.backup")
                if backup.exists():
                    raise OperationError("backup already exists")
                prompt.rename(backup)
            if prompt.is_symlink() and prompt.resolve() != kit_prompt.resolve():
                raise OperationError("release-decision symlink points elsewhere")
            if not prompt.exists():
                prompt.parent.mkdir(parents=True, exist_ok=True)
                prompt.symlink_to(kit_prompt)
            journal.persist({**journal.state, "phase": "role_adopted"})
            state = journal.state
        if state["phase"] in {"role_adopted", "verified"}:
            if _git(Path(data["primary_root"]), "rev-parse", "HEAD") != data["target_commit"]:
                raise OperationError("primary target readback mismatch")
            journal.persist({**journal.state, "phase": "verified"})
        return {"schema": "kit-primary-activation-report-v2", "phase": "verified",
                "effects_released": len(journal.state.get("effects", {}))}


def _activation_lstat(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.exists():
        if path.is_file():
            return {"kind": "file", "sha256": sha256_bytes(path.read_bytes())}
        if path.is_dir():
            return {"kind": "directory"}
    return {"kind": "absent"}


def _validate_activation_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(
        plan.value,
        {
            "schema", "state_dir", "primary_root", "branch", "baseline_commit",
            "target_commit", "role", "git_config_allowlist", "tracking",
            "installed_links", "prompt_prestate", "backups", "source_prompt_sha256",
        },
        "activation plan",
    )
    if _string(_required(data, "branch", "activation plan"), "branch") != "main":
        raise PlanValidationError("primary activation is restricted to main")
    _sha1(_required(data, "baseline_commit", "activation plan"), "baseline_commit")
    _sha1(_required(data, "target_commit", "activation plan"), "target_commit")
    state_dir = _absolute_path(_required(data, "state_dir", "activation plan"), "state_dir")
    primary_root = _absolute_path(_required(data, "primary_root", "activation plan"), "primary_root")
    role = _closed(
        _required(data, "role", "activation plan"),
        {"prompt_path", "config_path", "kit_prompt_path", "expected_prompt_sha256"},
        "role",
    )
    for key in ("prompt_path", "config_path", "kit_prompt_path"):
        _absolute_path(_required(role, key, "role"), f"role.{key}")
    _sha256(_required(role, "expected_prompt_sha256", "role"), "role.expected_prompt_sha256")
    _sha256(_required(data, "source_prompt_sha256", "activation plan"), "source_prompt_sha256")
    allowlist = _required(data, "git_config_allowlist", "activation plan")
    if not isinstance(allowlist, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in allowlist.items()
    ):
        raise PlanValidationError("git_config_allowlist must be a string map")
    tracking = _required(data, "tracking", "activation plan")
    if not isinstance(tracking, (str, type(None))):
        raise PlanValidationError("tracking must be a string or null")
    links = _bounded_list(_required(data, "installed_links", "activation plan"), "installed_links")
    for index, link in enumerate(links):
        link = _closed(link, {"path", "target"}, f"installed_links[{index}]")
        _absolute_path(_required(link, "path", f"installed_links[{index}]"), "installed link.path")
        _absolute_path(_required(link, "target", f"installed_links[{index}]"), "installed link.target")
    prestate = _closed(
        _required(data, "prompt_prestate", "activation plan"),
        {"kind", "target", "sha256"},
        "prompt_prestate",
    )
    if prestate["kind"] not in {"absent", "file", "symlink"}:
        raise PlanValidationError("prompt_prestate.kind is unsupported")
    if prestate.get("target") is not None:
        _string(prestate["target"], "prompt_prestate.target")
    if prestate.get("sha256") is not None:
        _sha256(prestate["sha256"], "prompt_prestate.sha256")
    backups = _closed(_required(data, "backups", "activation plan"), {"path", "sha256", "kind"}, "backups")
    if backups.get("path") is not None:
        _absolute_path(backups["path"], "backups.path")
    if backups.get("sha256") is not None:
        _sha256(backups["sha256"], "backups.sha256")
    return {
        **data,
        "state_dir": state_dir,
        "primary_root": primary_root,
        "role": {**role},
        "git_config_allowlist": dict(allowlist),
        "installed_links": links,
        "prompt_prestate": prestate,
        "backups": backups,
    }


def _activation_preflight(data: Mapping[str, Any]) -> dict[str, Any]:
    root = data["primary_root"]
    if not root.is_dir() or root.is_symlink():
        raise OperationError("primary checkout root is unsafe")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise OperationError("primary checkout root mismatch")
    if _git(root, "branch", "--show-current") != "main":
        raise OperationError("primary checkout is not on main")
    if _git(root, "status", "--porcelain"):
        raise OperationError("primary checkout is dirty")
    baseline = data["baseline_commit"]
    target = data["target_commit"]
    if _git(root, "rev-parse", "HEAD") != baseline:
        raise OperationError("primary baseline does not match the plan")
    if _git(root, "rev-parse", "refs/heads/main") != baseline:
        raise OperationError("local main ref does not match the baseline")
    _git(root, "cat-file", "-e", f"{target}^{{commit}}")
    _git(root, "merge-base", "--is-ancestor", baseline, target)
    tracking = data["tracking"]
    actual_tracking = _git(root, "config", "--local", "--get-regexp", r"^branch\.main\.", check=False)
    if tracking is not None and actual_tracking != tracking:
        raise OperationError("main tracking configuration drifted")
    dangerous = {}
    for key in (
        "core.hookspath", "credential.helper", "core.askpass", "pager.diff",
        "pager.show", "pager.log", "maintenance.auto", "core.attributesfile",
        "core.whitespace",
    ):
        dangerous[key] = _git(root, "config", "--local", "--get", key, check=False)
    for key, value in dangerous.items():
        if value and data["git_config_allowlist"].get(key) != value:
            raise OperationError(f"unapproved Git configuration: {key}")
    role = data["role"]
    source = Path(role["kit_prompt_path"])
    if source.is_symlink() or not source.is_file():
        raise OperationError("Kit source prompt is absent or unsafe")
    if sha256_bytes(source.read_bytes()) != data["source_prompt_sha256"]:
        raise OperationError("Kit source prompt digest drifted")
    config = Path(role["config_path"])
    if config.is_symlink() or not config.is_file():
        raise OperationError("role configuration is absent or unsafe")
    try:
        import tomllib
        parsed_config = tomllib.loads(config.read_text())
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise OperationError("role configuration is invalid") from error
    config_text = config.read_text()
    if any(key in config_text for key in ("model =", "tools =", "workflow =", "agent =")):
        raise OperationError("role configuration contains forbidden execution authority")
    if not isinstance(parsed_config, dict):
        raise OperationError("role configuration is not an object")
    registrations = parsed_config.get("roles", parsed_config.get("subagents", {}))
    if not isinstance(registrations, dict) or not any(
        key in registrations for key in ("release-decision", "release_decision")
    ):
        raise OperationError("release-decision role is not registered")
    for link in data["installed_links"]:
        path = Path(link["path"])
        target_path = Path(link["target"])
        if not path.is_symlink() or path.resolve() != target_path.resolve() or not target_path.exists():
            raise OperationError("installed Kit link is missing, foreign, or dangling")
    prompt = Path(role["prompt_path"])
    actual_prestate = _activation_lstat(prompt)
    if actual_prestate["kind"] == "file" and actual_prestate.get("sha256") != role["expected_prompt_sha256"]:
        raise OperationError("installed prompt bytes drifted")
    if actual_prestate["kind"] == "symlink":
        if prompt.resolve() != source.resolve() or not source.exists():
            raise OperationError("installed prompt symlink is foreign or dangling")
    if actual_prestate["kind"] not in {"absent", "file", "symlink"}:
        raise OperationError("installed prompt kind is unsupported")
    if actual_prestate != data["prompt_prestate"] and actual_prestate["kind"] != "file":
        raise OperationError("installed prompt prestate drifted")
    backup_path = Path(data["backups"]["path"]) if data["backups"].get("path") else prompt.with_name(prompt.name + ".release-decision.backup")
    backup_state = _activation_lstat(backup_path)
    expected_backup_kind = data["backups"].get("kind", "absent")
    if backup_state["kind"] != expected_backup_kind:
        raise OperationError("activation backup state drifted")
    if data["backups"].get("sha256") and backup_state.get("sha256") != data["backups"]["sha256"]:
        raise OperationError("activation backup digest drifted")
    return {
        "root": str(root),
        "current_commit": _git(root, "rev-parse", "HEAD"),
        "target_commit": target,
        "source_prompt_sha256": data["source_prompt_sha256"],
        "prompt": actual_prestate,
        "backup": backup_state,
        "config_sha256": sha256_bytes(config.read_bytes()),
    }


def activate_primary_checkout(
    plan: LoadedPlan,
    *,
    mode: str,
    confirm: bool | str = False,
) -> dict[str, Any]:
    data = _validate_activation_plan(plan)
    if mode not in {"preview", "apply", "rollback"}:
        raise PlanValidationError("activation mode is unsupported")
    if mode == "preview":
        return {
            "schema": "kit-primary-activation-report-v2",
            "phase": "preview",
            "plan_sha256": plan.sha256,
            "preflight": _activation_preflight(data),
            "effects_released": 0,
        }
    if confirm not in (True, plan.sha256):
        raise PlanValidationError("activation mutation requires confirmation")
    journal_obj = OperationJournal(data["state_dir"], "kit-primary-activation", plan)
    with journal_obj as journal:
        if mode == "rollback":
            if journal.state is None:
                raise JournalError("activation rollback requires a prepared journal")
            state = journal.require_phase({"prepared"})
            journal.persist({**state, "phase": "rolled_back"})
            return {"schema": "kit-primary-activation-report-v2", "phase": "rolled_back", "effects_released": 0}
        if journal.state is None:
            journal.persist({"phase": "prepared", "preflight": _activation_preflight(data), "effects": {}})
        state = journal.require_phase({"prepared", "activation_committed", "primary_promoted", "role_adopted", "verified"})
        if state["phase"] == "prepared":
            _activation_preflight(data)
            journal.persist({**state, "phase": "activation_committed"})
            root = data["primary_root"]
            pre_hash = canonical_sha256({"head": data["baseline_commit"]})
            post_hash = canonical_sha256({"head": data["target_commit"]})

            def current_head() -> str:
                head = _git(root, "rev-parse", "HEAD")
                if head == data["baseline_commit"]:
                    return pre_hash
                if head == data["target_commit"]:
                    return post_hash
                raise EffectBlocked("primary HEAD is ambiguous")

            command = [
                "/usr/bin/git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                "-c", "credential.helper=", "-c", "maintenance.auto=false",
                "merge", "--ff-only", data["target_commit"],
            ]
            result = run_effect(
                journal, effect_key="primary-merge", command=command, cwd=root,
                preimage_sha256=pre_hash, postimage_sha256=post_hash,
                extra_env={
                    "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                },
                current_sha256=current_head,
            )
            if result.settlement == "preimage":
                return {"schema": "kit-primary-activation-report-v2", "phase": "activation_committed", "settled": "preimage", "effects_released": 0}
            state = journal.state
            journal.persist({**state, "phase": "primary_promoted"})
        elif state["phase"] == "activation_committed":
            _activation_preflight(data)
            effect = (state.get("effects") or {}).get("primary-merge")
            if not isinstance(effect, dict):
                raise JournalError("activation merge effect is missing")
            pre_hash = canonical_sha256({"head": data["baseline_commit"]})
            post_hash = canonical_sha256({"head": data["target_commit"]})

            def current_head() -> str:
                head = _git(data["primary_root"], "rev-parse", "HEAD")
                if head == data["baseline_commit"]:
                    return pre_hash
                if head == data["target_commit"]:
                    return post_hash
                raise EffectBlocked("primary HEAD is ambiguous")

            if effect.get("status") == "settled_preimage":
                return {"schema": "kit-primary-activation-report-v2", "phase": "activation_committed", "settled": "preimage", "effects_released": 0}
            settled = recover_effect(
                journal, effect_key="primary-merge", preimage_sha256=pre_hash,
                postimage_sha256=post_hash, current_sha256=current_head,
            )
            if settled == "preimage":
                return {"schema": "kit-primary-activation-report-v2", "phase": "activation_committed", "settled": "preimage", "effects_released": 0}
            journal.persist({**journal.state, "phase": "primary_promoted"})
        if journal.state["phase"] == "primary_promoted":
            _activation_preflight(data)
            role = data["role"]
            prompt = Path(role["prompt_path"])
            source = Path(role["kit_prompt_path"])
            backup = Path(data["backups"]["path"]) if data["backups"].get("path") else prompt.with_name(prompt.name + ".release-decision.backup")
            if prompt.is_symlink():
                if prompt.resolve() != source.resolve():
                    raise OperationError("installed prompt symlink is foreign")
            elif prompt.exists():
                if not prompt.is_file() or sha256_bytes(prompt.read_bytes()) != role["expected_prompt_sha256"]:
                    raise OperationError("installed prompt is not the approved regular file")
                if not backup.exists():
                    prompt.rename(backup)
                elif not backup.is_file() or sha256_bytes(backup.read_bytes()) != role["expected_prompt_sha256"]:
                    raise OperationError("activation backup would be clobbered")
                if prompt.exists() and not prompt.is_symlink():
                    prompt.unlink()
                if not prompt.exists():
                    prompt.symlink_to(source)
            else:
                prompt.parent.mkdir(parents=True, exist_ok=True)
                prompt.symlink_to(source)
            if not prompt.is_symlink() or prompt.resolve() != source.resolve():
                raise OperationError("installed prompt adoption readback failed")
            journal.persist({**journal.state, "phase": "role_adopted"})
        _activation_preflight(data)
        if _git(data["primary_root"], "rev-parse", "HEAD") != data["target_commit"]:
            raise OperationError("primary target readback mismatch")
        final_prompt = _activation_lstat(Path(data["role"]["prompt_path"]))
        if final_prompt["kind"] != "symlink" or Path(data["role"]["prompt_path"]).resolve() != Path(data["role"]["kit_prompt_path"]).resolve():
            raise OperationError("final release-decision prompt readback mismatch")
        journal.persist({**journal.state, "phase": "verified"})
        return {
            "schema": "kit-primary-activation-report-v2",
            "phase": "verified",
            "effects_released": len(journal.state.get("effects", {})),
        }


def main_guardian(argv: Sequence[str]) -> int:
    del argv
    return 2
