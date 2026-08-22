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


_GUARDIAN = r"""
import base64, json, os, selectors, signal, subprocess, sys
lock_fd = int(sys.argv[1])
report_fd = int(sys.argv[2])
command = json.loads(base64.b64decode(sys.argv[3]).decode())
cwd = sys.argv[4]
env = json.loads(base64.b64decode(sys.argv[5]).decode())
limit = int(sys.argv[6])
stdin_bytes = base64.b64decode(sys.argv[7])
child = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, stdin=subprocess.PIPE, close_fds=True,
                         pass_fds=(lock_fd,))
if stdin_bytes:
    child.stdin.write(stdin_bytes)
child.stdin.close()
os.write(report_fd, (json.dumps({"pid": child.pid}) + "\n").encode())
os.close(report_fd)
streams = {child.stdout: bytearray(), child.stderr: bytearray()}
buffers = {child.stdout: streams[child.stdout], child.stderr: streams[child.stderr]}
selector = selectors.DefaultSelector()
for stream in streams:
    selector.register(stream, selectors.EVENT_READ)
def stop(signum, _frame):
    try:
        child.terminate()
        child.wait(timeout=5)
    except Exception:
        try:
            child.kill()
            child.wait(timeout=2)
        except Exception:
            pass
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while streams:
    for key, _ in selector.select(0.1):
        chunk = key.fileobj.read1(65536)
        if not chunk:
            selector.unregister(key.fileobj)
            streams.pop(key.fileobj, None)
        elif len(buffers[key.fileobj]) < limit:
            buffers[key.fileobj].extend(chunk[:limit-len(buffers[key.fileobj])])
    if child.poll() is not None and not streams:
        break
child.wait()
sys.stdout.buffer.write(bytes(buffers.get(child.stdout, b""))[-limit:])
sys.stderr.buffer.write(bytes(buffers.get(child.stderr, b""))[-limit:])
sys.stdout.flush()
sys.stderr.flush()
raise SystemExit(child.returncode)
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
) -> EffectResult:
    if timeout <= 0 or timeout > 300:
        raise PlanValidationError("effect timeout must be between 0 and 300 seconds")
    command = _argv(list(command), "effect command")
    if not Path(command[0]).is_absolute():
        raise PlanValidationError("effect executable must be an absolute path")
    cwd = _absolute_path(str(cwd), "effect cwd")
    digest = canonical_sha256(command)
    state = journal.state or {}
    effects = dict(state.get("effects") or {})
    previous = effects.get(effect_key)
    if isinstance(previous, dict):
        status = previous.get("status")
        if status == "settled_preimage":
            if previous.get("settled_invocation") == journal.invocation_id:
                raise JournalError("same-cycle effect replay is forbidden")
        elif status not in {None}:
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
    env = _safe_env(extra_env)
    encoded_command = base64.b64encode(canonical_bytes(command)).decode()
    encoded_env = base64.b64encode(canonical_bytes(env)).decode()
    if stdin_bytes is None:
        stdin_bytes = b""
    if len(stdin_bytes) > MAX_OUTPUT:
        raise PlanValidationError("effect stdin is too large")
    encoded_stdin = base64.b64encode(stdin_bytes).decode()
    guardian = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _GUARDIAN,
            str(lock_fd),
            str(write_fd),
            encoded_command,
            str(cwd),
            encoded_env,
            str(MAX_OUTPUT),
            encoded_stdin,
        ],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(lock_fd, write_fd),
        start_new_session=True,
        close_fds=True,
    )
    os.close(write_fd)
    child_pid: int | None = None
    try:
        acknowledgement = selectors.DefaultSelector()
        acknowledgement.register(read_fd, selectors.EVENT_READ)
        if not acknowledgement.select(timeout):
            raise EffectBlocked("effect guardian acknowledgement timed out")
        data = os.read(read_fd, 4096)
        if not data:
            raise EffectBlocked("effect guardian acknowledgement was lost")
        child_pid = int(json.loads(data.splitlines()[0].decode())["pid"])
        if data:
            child_pid = int(json.loads(data.splitlines()[0].decode())["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        guardian.terminate()
        try:
            guardian.wait(timeout=5)
        except subprocess.TimeoutExpired:
            guardian.kill()
            guardian.wait(timeout=2)
        raise EffectBlocked("effect guardian acknowledgement was invalid") from error
    finally:
        os.close(read_fd)
    effects[effect_key]["child"] = {"guardian_pid": guardian.pid, "child_pid": child_pid}
    journal.persist({**journal.state, "effects": effects})
    try:
        stdout, stderr = _bounded_communicate(guardian, timeout)
    except subprocess.TimeoutExpired as error:
        guardian.terminate()
        try:
            guardian.wait(timeout=5)
        except subprocess.TimeoutExpired:
            guardian.kill()
            guardian.wait(timeout=2)
        effects[effect_key]["status"] = "ambiguous"
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
    effects[effect_key]["status"] = "verified" if result.returncode == 0 else "failed"
    journal.persist({**journal.state, "effects": effects})
    if result.returncode:
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
    if not isinstance(entry, dict) or entry.get("status") != "attempted":
        raise JournalError(f"effect {effect_key!r} is not awaiting settlement")
    current = current_sha256()
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
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.tmp"
    if temporary.exists():
        raise JournalError("deterministic report temporary file already exists")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)
    if path.read_bytes() != data:
        raise JournalError("report readback mismatch")


def verify_release_portfolio(
    plan: LoadedPlan,
    *,
    report_path: Path | None = None,
    write_report: bool = False,
) -> dict[str, Any]:
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
        records.append(
            {
                "repository": repository,
                "commit": commit,
                "profile_sha256": _blob_digest(root, commit, ".kent/workflow-profile.toml"),
                "source_digests": _selected_source_digests(root, commit),
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
    target = report_path
    if target is None and "report_path" in data:
        target = _absolute_path(data["report_path"], "report_path")
    if write_report:
        if target is None:
            raise PlanValidationError("write_report requires report_path")
        _atomic_write(target, canonical_bytes(report) + b"\n")
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


def _selected_source_digests(root: Path, commit: str) -> dict[str, str]:
    profile_path = ".kent/workflow-profile.toml"
    profile_bytes = _blob_bytes(root, commit, profile_path)
    try:
        profile = ProjectProfile.from_toml(
            root,
            profile_bytes.decode("utf-8"),
            source=f"{commit}:{profile_path}",
            check_files=False,
        )
        spec_path = profile.release.spec_path
        spec_bytes = _blob_bytes(root, commit, spec_path)
        spec = ReleaseSpec.from_toml(
            spec_bytes.decode("utf-8"), profile=profile
        )
        manifest_path = spec.source_manifest.path
        manifest_bytes = _blob_bytes(root, commit, manifest_path)
    except (AttributeError, UnicodeDecodeError, ValueError, KeyError) as error:
        raise OperationError("selected release source is not schema-4 complete") from error
    return {
        "profile_sha256": sha256_bytes(profile_bytes),
        "release_spec_sha256": sha256_bytes(spec_bytes),
        "source_manifest_sha256": sha256_bytes(manifest_bytes),
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
            record["sha256"] = sha256_bytes(entry.read_bytes())
        result.append(record)
    if len(result) > MAX_LIST:
        raise OperationError("Session manifest is too large")
    return result


def _typed_member(member: Mapping[str, Any], index: int) -> dict[str, Any]:
    _reject_raw_protocol_fields(member, f"members[{index}]")
    allowed = {
        "workflow_id",
        "revision",
        "project_id",
        "project",
        "links",
        "default",
        "tasks",
        "sessions",
        "worktrees",
        "retained",
        "absent",
        "preimage",
        "postimage",
        "resource_inventory",
        "expected",
    }
    item = _closed(member, allowed, f"members[{index}]")
    workflow_id = _uuid(_required(item, "workflow_id", f"members[{index}]"), "workflow_id")
    revision = _sha1(_required(item, "revision", f"members[{index}]"), "revision")
    tasks = _bounded_list(item.get("tasks", []), f"members[{index}].tasks")
    for task in tasks:
        if not isinstance(task, dict):
            raise PlanValidationError("task inventory must contain objects")
        if task.get("terminal") is not True:
            raise PlanValidationError("D9 refuses a nonterminal task")
        if task.get("current_node") not in (None, ""):
            raise PlanValidationError("D9 refuses an active Current Node")
        if task.get("approval_pending") is True:
            raise PlanValidationError("D9 refuses a pending approval")
    _unique(
        [_string(task.get("id"), "task.id") for task in tasks if isinstance(task, dict)],
        "task identities",
    )
    for key in ("links", "sessions", "worktrees", "retained", "absent"):
        _bounded_list(item.get(key, []), f"members[{index}].{key}")
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
            {"id", "status", "path", "manifest", "retained", "task_id"},
            f"members[{index}].session",
        )
        _string(_required(session, "id", "session.id"), "session.id")
        _string(_required(session, "status", "session.status"), "session.status")
        if session.get("path") is not None:
            _absolute_path(session["path"], "session.path")
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
        {
            "schema",
            "project_id",
            "state_dir",
            "kent",
            "kent_path",
            "kent_sha256",
            "database",
            "database_path",
            "schema_identity",
            "project_root",
            "session_roots",
            "members",
            "retained_resources",
        },
        "retirement plan",
    )
    project_id = _uuid(_required(data, "project_id", "retirement plan"), "project_id")
    state_dir = _absolute_path(_required(data, "state_dir", "retirement plan"), "state_dir")
    kent_value = data.get("kent") or {}
    if isinstance(kent_value, dict):
        kent = _closed(kent_value, {"path", "sha256"}, "kent")
        kent_path = _kent_path(_required(kent, "path", "kent"), "kent.path")
        kent_sha = _sha256(_required(kent, "sha256", "kent"), "kent.sha256")
    else:
        kent_path = _kent_path(data.get("kent_path"), "kent_path")
        kent_sha = _sha256(_required(data, "kent_sha256", "retirement plan"), "kent_sha256")
    _verify_executable(kent_path, kent_sha)
    database = data.get("database") or {}
    if isinstance(database, dict) and database:
        database = _closed(database, {"path", "schema", "project_root", "session_roots"}, "database")
        database_path = _absolute_path(_required(database, "path", "database"), "database.path")
        schema_identity = _string(_required(database, "schema", "database"), "database.schema")
        project_root = _absolute_path(_required(database, "project_root", "database"), "database.project_root")
        session_roots = [_absolute_path(item, "session_root") for item in _bounded_list(
            _required(database, "session_roots", "database"), "database.session_roots"
        )]
    else:
        database_path = _absolute_path(data.get("database_path"), "database_path")
        schema_identity = _string(data.get("schema_identity"), "schema_identity")
        project_root = _absolute_path(data.get("project_root"), "project_root")
        session_roots = [_absolute_path(item, "session_root") for item in _bounded_list(
            data.get("session_roots", []), "session_roots"
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


def _d9_read_inventory(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Run the complete source-owned read protocol, never a plan command."""
    kent = parsed["kent"]
    _verify_executable(kent, parsed["kent_sha256"])
    project = parsed["project_id"]
    cwd = parsed["project_root"]
    result: dict[str, Any] = {
        "projects": _kent_json(kent, ["project", "list", "--json"], cwd=cwd),
        "workflows": _kent_pages(
            kent, ["workflow", "list", "--project", project], cwd=cwd
        ),
        "worktrees": _kent_json(kent, ["worktree", "list", "--json"], cwd=cwd),
        "database_schema": parsed["schema"],
    }
    for member in parsed["members"]:
        _d9_check_git_resources(member)
        _d9_check_session_manifests(member)
        wid = member["workflow_id"]
        tasks = _kent_pages(
            kent,
            ["task", "list", "--project", project, "--workflow", wid],
            cwd=cwd,
        )
        task_rows = [
            row
            for page in tasks
            for row in page.get("items", page.get("tasks", []))
            if isinstance(row, dict)
        ]
        result[wid] = {
            "workflow": _kent_json(kent, ["workflow", "inspect", wid, "--json"], cwd=cwd),
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
        for task in task_rows:
            task_id = task.get("id", task.get("task_id"))
            if isinstance(task_id, str):
                result[wid].setdefault("task_show", []).append(
                    _kent_json(kent, ["task", "show", "--project", project, task_id, "--json"], cwd=cwd)
                )
                result[wid].setdefault("sessions", []).extend(
                    _kent_pages(
                        kent,
                        ["task", "sessions", "--project", project, task_id],
                        cwd=cwd,
                    )
                )
                session_ids = [
                    session.get("id", session.get("session_id"))
                    for page in result[wid].get("sessions", [])
                    for session in page.get("items", page.get("sessions", []))
                    if isinstance(session, dict)
                ]
                for session_id in session_ids:
                    if isinstance(session_id, str):
                        result[wid].setdefault("sqlite_sessions", []).append(
                            _sqlite_session_by_id(
                                parsed["database"], parsed["schema"], session_id
                            )
                        )
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


def _d9_check_session_manifests(member: Mapping[str, Any]) -> None:
    for session in member.get("sessions", []):
        if session.get("retained") is not True or not session.get("path"):
            continue
        actual = _session_manifest(Path(session["path"]))
        expected = session.get("manifest")
        if expected is not None and canonical_sha256(actual) != canonical_sha256(expected):
            raise EffectBlocked("retained Session manifest drifted")


def _sqlite_session_by_id(
    database: Path,
    expected_schema: str,
    session_id: str,
) -> dict[str, Any]:
    """Read only the schema-owned Session row; plans cannot provide SQL."""
    if not database.is_file() or database.is_symlink():
        raise OperationError("Kent persistence database is absent or unsafe")
    uri = f"file:{database}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if expected_schema and expected_schema not in {"kent-2.6.1", "sqlite"}:
            raise OperationError("unsupported Kent persistence schema")
        if "sessions" not in tables:
            return {"session_id": session_id, "present": False}
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(sessions)")
        }
        if "id" not in columns:
            raise OperationError("Session table has no supported identity column")
        row = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchall()
        if len(row) != 1:
            return {"session_id": session_id, "present": False, "matches": len(row)}
        names = [
            item[1]
            for item in connection.execute("PRAGMA table_info(sessions)")
        ]
        return {
            "session_id": session_id,
            "present": True,
            "row": dict(zip(names, row[0])),
        }
    except sqlite3.Error as error:
        raise OperationError(f"read-only Session query failed: {error}") from error
    finally:
        try:
            connection.close()
        except (NameError, AttributeError):
            pass


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
    if mode == "preview":
        inventory = _d9_read_inventory(parsed)
        return {
            "schema": "workflow-retirement-batch-report-v2",
            "plan_sha256": plan.sha256,
            "phase": "preview",
            "inventory_sha256": canonical_sha256(inventory),
            "effects_released": 0,
        }
    with OperationJournal(parsed["state_dir"], "workflow-retirement-batch", plan) as journal:
        if journal.state is None:
            inventory = _d9_read_inventory(parsed)
            journal.persist(
                {
                    "phase": "prepared",
                    "inventory": inventory,
                    "inventory_sha256": canonical_sha256(inventory),
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
            inventory = _d9_read_inventory(parsed)
            if canonical_sha256(inventory) != state["inventory_sha256"]:
                raise EffectBlocked("D9 preimage changed before member effect")
            journal.persist({**journal.state, "phase": "in_progress"})
            wid = member["workflow_id"]
            run_effect(
                journal,
                effect_key=f"delete:{wid}",
                command=_kent_delete_command(parsed["kent"], wid, True),
                cwd=parsed["project_root"],
            )
            post = _d9_read_inventory(parsed)
            member_record = post.get(wid, {})
            if member_record.get("workflow", {}).get("present", True):
                raise EffectBlocked(f"retired workflow {wid} is still present")
            before = state.get("inventory", {}).get(wid, {})
            _validate_d9_postimage(member, before, member_record)
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
        journal.persist({**journal.state, "phase": "complete"})
        return {
            "schema": "workflow-retirement-batch-report-v2",
            "plan_sha256": plan.sha256,
            "phase": "complete",
            "members_verified": len(parsed["members"]),
        }


def _validate_d9_postimage(
    member: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    if after.get("workflow", {}).get("present", False):
        raise EffectBlocked("D9 exact absence was not proved")
    before_sessions = before.get("sqlite_sessions", [])
    after_sessions = after.get("sqlite_sessions", [])
    if not before_sessions:
        return
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
    for session in member.get("sessions", []):
        if session.get("retained") is True and session.get("path"):
            expected = session.get("manifest")
            actual = _session_manifest(Path(session["path"]))
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
        {"schema", "state_dir", "kent", "kent_path", "kent_sha256", "workflows", "d9_journal"},
        "canonical plan",
    )
    state_dir = _absolute_path(_required(data, "state_dir", "canonical plan"), "state_dir")
    kent = data.get("kent") or {}
    if isinstance(kent, dict):
        kent = _closed(kent, {"path", "sha256"}, "kent")
        path = _kent_path(_required(kent, "path", "kent"), "kent.path")
        digest = _sha256(_required(kent, "sha256", "kent"), "kent.sha256")
    else:
        path = _kent_path(data.get("kent_path"), "kent_path")
        digest = _sha256(data.get("kent_sha256"), "kent_sha256")
    _verify_executable(path, digest)
    workflows = _bounded_list(_required(data, "workflows", "canonical plan"), "workflows")
    if not workflows:
        raise PlanValidationError("canonical plan has no workflows")
    parsed = []
    for index, raw in enumerate(workflows):
        if not isinstance(raw, dict):
            raise PlanValidationError("workflow entry must be an object")
        _reject_raw_protocol_fields(raw, f"workflows[{index}]")
        allowed = {
            "workflow_id",
            "project_id",
            "intent",
            "expected_version",
            "revision",
            "preimage",
            "postimage",
            "target",
            "graph",
            "metadata",
            "tasks",
            "current_nodes",
            "pending_approvals",
            "terminal_tasks",
            "terminal_anchors",
            "links",
            "default",
            "rollback",
            "allow_create",
        }
        item = _closed(raw, allowed, f"workflows[{index}]")
        item["workflow_id"] = _uuid(_required(item, "workflow_id", "workflow_id"), "workflow_id")
        intent = _string(_required(item, "intent", "intent"), "intent")
        if intent not in {"graph-only", "metadata-only", "graph-and-metadata"}:
            raise PlanValidationError("canonical intent is not typed")
        _workflow_gate(item, f"workflows[{index}]")
        _terminal_gate(item, f"workflows[{index}]")
        if "expected_version" not in item and "revision" not in item:
            raise PlanValidationError("canonical plan must bind an expected revision")
        if item.get("allow_create", False) is not False:
            raise PlanValidationError("canonical reconciliation may not create workflows")
        if "graph" in item and not isinstance(item["graph"], dict):
            raise PlanValidationError("target graph must be a document")
        if "metadata" in item and not isinstance(item["metadata"], dict):
            raise PlanValidationError("target metadata must be an object")
        parsed.append(item)
    _unique([item["workflow_id"] for item in parsed], "canonical workflow identities")
    return {"state_dir": state_dir, "kent": path, "kent_sha256": digest, "workflows": parsed,
            "d9_journal": data.get("d9_journal")}


def _canonical_read(parsed: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    wid = item["workflow_id"]
    project = item.get("project_id")
    args = ["workflow", "inspect", wid, "--json"]
    if project:
        args[2:2] = ["--project", str(project)]
    root = item.get("project_root", Path.cwd())
    return _kent_json(parsed["kent"], args, cwd=Path(root))


def _canonical_effects(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    restore: bool = False,
    confirm: bool = True,
) -> list[list[str]]:
    wid = item["workflow_id"]
    metadata = item.get("metadata") or {}
    graph = item.get("graph") or {}
    if restore:
        metadata = (item.get("preimage") or {}).get("metadata", metadata)
        graph = (item.get("preimage") or {}).get("graph", graph)
    version = item.get("expected_version")
    commands: list[list[str]] = []
    if item["intent"] in {"graph-only", "graph-and-metadata"}:
        commands.append([str(parsed["kent"]), "workflow", "graph", "apply", "-"])
        if confirm:
            commands[-1].append("--confirm")
        if version is not None:
            commands[-1].extend(["--expected-version", str(version)])
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
    del version
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


def main_guardian(argv: Sequence[str]) -> int:
    del argv
    return 2
