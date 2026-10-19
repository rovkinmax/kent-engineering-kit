"""Closed, recoverable operational boundaries for the Kit release tools.

The module intentionally keeps plans declarative.  Kent and Git commands are
constructed here from typed plan values; a plan can never smuggle in a shell,
an executable, or an arbitrary probe.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import struct
import subprocess
import sys
import tempfile
import time
import sqlite3
import tomllib
import uuid
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from .release import canonical_bytes as release_canonical_bytes
from .release import ReleaseSpec
from .profile import ProjectProfile
from .revision import RevisionPreflightError, preflight_project_revision
from .kent import execution_target_from_policy
from .model import SpecError, validate_execution_target
SHA1_RE = '^[0-9a-f]{40}$'
SHA256_RE = '^[0-9a-f]{64}$'
UUID_RE = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'
MAX_LIST = 256
MAX_COMMAND = 64
MAX_OUTPUT = 256 * 1024
CANONICAL_GRAPH_MAX_OUTPUT = 8 * 1024 * 1024
EXEC_ENTRY_LIMIT = 128 * 1024
EXEC_AGGREGATE_LIMIT = 512 * 1024
EXEC_FD_DECIMAL_RESERVE = 20
EXEC_WRITE_CHUNK = 65536
MANIFEST_READ_CHUNK = 64 * 1024
SESSION_MANIFEST_FILE_LIMIT = 32 * 1024 * 1024
SESSION_MANIFEST_TOTAL_LIMIT = 64 * 1024 * 1024
SETUP_KILL_REAP_GRACE = 2.0
JOURNAL_SCHEMA = 'kit-operation-journal-v1'
PHASES = {
    'prepared', 'in_progress', 'complete', 'verified', 'activation_committed', 'primary_promoted', 'role_adopted',
    'rolled_back',
}
JOURNAL_FIELDS = {
    'schema', 'operation', 'plan_sha256', 'phase', 'effects', 'members', 'preimage', 'inventory',
    'inventory_sha256', 'preflight',
}
RELEASE_LIVE_PORTFOLIO_SCHEMA = 'release-live-portfolio-plan-v1'
RELEASE_LIVE_PORTFOLIO_OPERATION = 'release-live-portfolio'
RELEASE_LIVE_PORTFOLIO_REPORT = 'release-live-portfolio-report-v1'
RELEASE_LIVE_PORTFOLIO_MODES = ('preview', 'prepare', 'retire', 'apply', 'rollback')
RELEASE_LIVE_PORTFOLIO_MUTATION_MODES = frozenset({'prepare', 'retire', 'apply', 'rollback'})
PORTFOLIO_PHASES = {
    'prepared', 'retirement_in_progress', 'd9_complete', 'canonical_in_progress', 'complete', 'rolled_back',
}
PORTFOLIO_MEMBER_STATUSES = {'pending', 'graph_verified', 'metadata_verified', 'verified', 'rolled_back'}
PORTFOLIO_JOURNAL_FIELDS = {
    'schema', 'operation', 'plan_sha256', 'phase', 'retirement_preimage', 'canonical_preimage',
    'retirement_targets', 'canonical_targets', 'members', 'effects',
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
        raise PlanValidationError(f'value is not canonical JSON: {error}') from error

def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def _duplicate_free(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanValidationError(f'duplicate JSON key: {key}')
        result[key] = value
    return result

def _closed(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanValidationError(f'{label} must be an object')
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanValidationError(f'{label} has unknown fields: {unknown}')
    return value

def _required(value: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in value:
        raise PlanValidationError(f'{label} is missing {key!r}')
    return value[key]

def _string(value: Any, label: str, *, nonempty: bool=True) -> str:
    if not isinstance(value, str) or (nonempty and (not value)):
        raise PlanValidationError(f'{label} must be a non-empty string')
    if '\x00' in value:
        raise PlanValidationError(f'{label} contains NUL')
    return value

def _digest(value: Any, label: str, pattern: str) -> str:
    value = _string(value, label)
    import re
    if not re.fullmatch(pattern, value):
        raise PlanValidationError(f'{label} has an invalid digest')
    return value

def _sha1(value: Any, label: str) -> str:
    return _digest(value, label, SHA1_RE)

def _sha256(value: Any, label: str) -> str:
    return _digest(value, label, SHA256_RE)

def _uuid(value: Any, label: str) -> str:
    return _digest(value, label, UUID_RE).lower()

def _native_project_id(value: Any, label: str) -> str:
    value = _string(value, label)
    if not value.startswith('project-'):
        raise PlanValidationError(f'{label} must be a native project ID')
    suffix = value[len('project-'):]
    if _uuid(suffix, label) != suffix:
        raise PlanValidationError(f'{label} must use a canonical lowercase UUID')
    return value

def _repository(value: Any, label: str) -> str:
    value = _string(value, label)
    import re
    if not re.fullmatch('[^/\\s]+/[^/\\s]+', value):
        raise PlanValidationError(f'{label} must be owner/name')
    return value

def _absolute_path(value: Any, label: str) -> Path:
    raw = _string(value, label)
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts:
        raise PlanValidationError(f"{label} must be absolute without '..'")
    return path

def _bounded_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlanValidationError(f'{label} must be a list')
    if len(value) > MAX_LIST:
        raise PlanValidationError(f'{label} exceeds {MAX_LIST} entries')
    return value

def _unique(values: Iterable[str], label: str) -> None:
    values = list(values)
    if len(values) != len(set(values)):
        raise PlanValidationError(f'{label} contains duplicate identities')

def _argv(value: Any, label: str) -> list[str]:
    values = _bounded_list(value, label)
    if not values or len(values) > MAX_COMMAND:
        raise PlanValidationError(f'{label} must contain 1..{MAX_COMMAND} items')
    return [_string(item, f'{label}[{index}]') for index, item in enumerate(values)]

@dataclass(frozen=True)
class LoadedPlan:
    schema: str
    value: dict[str, Any]
    raw: bytes
    sha256: str

def load_plan(path: Path, *, schema: str | Sequence[str], expected_sha256: str, mutation: bool=False,
        confirm: str | None=None) -> \
        LoadedPlan:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise PlanValidationError('plan path must not be a symlink')
    raw = path.read_bytes()
    expected = _sha256(expected_sha256, 'expected_plan_sha256')
    actual = sha256_bytes(raw)
    if actual != expected:
        raise PlanValidationError('plan digest mismatch')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_duplicate_free, parse_constant=lambda token: (_ for _
                in ()).throw(PlanValidationError(f'invalid JSON constant {token}')))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlanValidationError(f'plan is not valid JSON: {error}') from error
    schemas = (schema,) if isinstance(schema, str) else tuple(schema)
    if not isinstance(value, dict) or value.get('schema') not in schemas:
        if isinstance(schema, str):
            raise PlanValidationError(f'expected schema {schema!r}')
        raise PlanValidationError(f'expected schema in {schemas!r}')
    if canonical_bytes(value) != raw:
        raise PlanValidationError('plan bytes are not canonical JSON')
    if mutation and confirm != expected:
        raise PlanValidationError('mutation requires --confirm equal to plan digest')
    return LoadedPlan(schema, value, raw, actual)

def _safe_state_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise JournalError("state directory must be absolute without '..'")
    if path.exists() and path.is_symlink():
        raise JournalError('state directory must not be a symlink')
    path.mkdir(parents=True, exist_ok=True, mode=448)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 18:
        raise JournalError('state directory must be a private user-owned directory')
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
        self.operation = _string(operation, 'operation')
        import re
        if not re.fullmatch('[a-z0-9-]{1,64}', self.operation):
            raise JournalError('operation name is not a safe journal stem')
        self.plan = plan
        self.allowed_phases = PORTFOLIO_PHASES if operation == RELEASE_LIVE_PORTFOLIO_OPERATION else PHASES
        self.allowed_fields = (
            PORTFOLIO_JOURNAL_FIELDS if operation == RELEASE_LIVE_PORTFOLIO_OPERATION else JOURNAL_FIELDS
        )
        self.lock_path = self.state_dir / '.operations.lock'
        self.path = self.state_dir / f'{self.operation}.journal.json'
        self.temp_path = self.state_dir / f'{self.operation}.journal.tmp'
        self._lock_fd: int | None = None
        self.state: dict[str, Any] | None = None
        self.invocation_id = hashlib.sha256(f'{os.getpid()}:{time.monotonic_ns()}'.encode()).hexdigest()

    def __enter__(self) -> 'OperationJournal':
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        try:
            self._lock_fd = os.open(self.lock_path, flags, 384)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            raise JournalError('operation lock is held by another invocation') from error
        if self.path.exists():
            self.state = self._read()
            if self.state.get('plan_sha256') != self.plan.sha256:
                self.__exit__(None, None, None)
                raise JournalError('journal belongs to a different plan')
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def _read(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise JournalError('journal path must not be a symlink')
        raw = self.path.read_bytes()
        try:
            state = json.loads(raw.decode('utf-8'), object_pairs_hook=_duplicate_free)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JournalError(f'journal is not valid JSON: {error}') from error
        if not isinstance(state, dict) or state.get('schema') != JOURNAL_SCHEMA:
            raise JournalError('journal schema is invalid')
        if state.get('operation') != self.operation:
            raise JournalError('journal operation is invalid')
        if state.get('plan_sha256') != self.plan.sha256:
            raise JournalError('journal plan digest is invalid')
        if state.get('phase') not in self.allowed_phases:
            raise JournalError('journal phase is invalid')
        if set(state) - self.allowed_fields:
            raise JournalError('journal contains unknown fields')
        if canonical_bytes(state) + b'\n' != raw:
            raise JournalError('journal readback is not canonical')
        return state

    def persist(self, state: Mapping[str, Any]) -> None:
        if self._lock_fd is None:
            raise JournalError('journal lock is not held')
        data = dict(state)
        data.update({'schema': JOURNAL_SCHEMA, 'operation': self.operation, 'plan_sha256': self.plan.sha256})
        if set(data) - self.allowed_fields:
            raise JournalError('journal contains unknown fields')
        if data.get('phase') not in self.allowed_phases:
            raise JournalError('journal phase is not closed')
        encoded = canonical_bytes(data) + b'\n'
        if self.temp_path.exists():
            raise JournalError('deterministic journal temporary file already exists')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.temp_path, flags, 384)
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
        if self.state is None or self.state.get('phase') not in allowed:
            raise JournalError('journal phase is not allowed')
        return self.state

def _safe_env(extra: Mapping[str, str] | None=None) -> dict[str, str]:
    allowed = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SYSTEMROOT'}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env['PATH'] = os.defpath
    for key in list(env):
        if key.startswith(('GIT_', 'KENT_', 'SSH_')):
            env.pop(key, None)
    if extra:
        for key, value in extra.items():
            if not isinstance(key, str) or not key.isidentifier():
                raise PlanValidationError(f'invalid environment key: {key!r}')
            env[key] = _string(value, f'environment.{key}')
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
import base64, hashlib, json, os, selectors, signal, subprocess, sys
lock_fd = int(sys.argv[1])
report_fd = int(sys.argv[2])
control_fd = int(sys.argv[3])
command_encoded = sys.argv[4]
command = json.loads(base64.b64decode(command_encoded).decode())
cwd = sys.argv[5]
env_encoded = sys.argv[6]
env = json.loads(base64.b64decode(env_encoded).decode())
limit = int(sys.argv[7])
input_limit = int(sys.argv[12])
expected_length = int(sys.argv[8])
expected_sha256 = sys.argv[9]
fd_decimal_limit = int(sys.argv[10])
if input_limit not in (262144, 1048576) or expected_length < 0 or expected_length > input_limit:
    raise SystemExit(126)
if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
    raise SystemExit(126)
stdin_bytes = sys.stdin.buffer.read(input_limit + 1)
if len(stdin_bytes) != expected_length or hashlib.sha256(stdin_bytes).hexdigest() != expected_sha256:
    raise SystemExit(126)
stdin_view = memoryview(stdin_bytes)
child = None

def stop(signum, _frame):
    if child is not None:
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
gate_read, gate_write = os.pipe()
gate_read_arg = str(gate_read)
if len(gate_read_arg) > fd_decimal_limit:
    os.close(gate_read)
    os.close(gate_write)
    raise SystemExit(126)
try:
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            sys.argv[11],
            gate_read_arg,
            command_encoded,
            cwd,
            env_encoded,
        ],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        pass_fds=(lock_fd, gate_read),
        start_new_session=True,
    )
except Exception:
    os.close(gate_read)
    os.close(gate_write)
    raise
os.close(gate_read)
report = (json.dumps({"pid": child.pid}, separators=(",", ":")) + "\n").encode()
try:
    if os.write(report_fd, report) != len(report):
        stop(signal.SIGTERM, None)
    os.close(report_fd)
except Exception:
    stop(signal.SIGTERM, None)
if os.read(control_fd, 1) != b"1":
    stop(signal.SIGTERM, None)
try:
    os.close(control_fd)
except OSError:
    pass
if os.write(gate_write, b"1") != 1:
    stop(signal.SIGTERM, None)
try:
    os.close(gate_write)
except OSError:
    pass
selector = selectors.DefaultSelector()
buffers = {"stdout": bytearray(), "stderr": bytearray()}
for name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
    os.set_blocking(stream.fileno(), False)
    selector.register(stream, selectors.EVENT_READ, name)
os.set_blocking(child.stdin.fileno(), False)
if stdin_view:
    selector.register(child.stdin, selectors.EVENT_WRITE, "stdin")
else:
    child.stdin.close()
while selector.get_map():
    for key, _ in selector.select(0.1):
        if key.data == "stdin":
            try:
                written = os.write(key.fileobj.fileno(), stdin_view[:65536])
            except (BrokenPipeError, OSError):
                written = 0
                stdin_view = memoryview(b"")
            else:
                stdin_view = stdin_view[written:]
            if not stdin_view:
                selector.unregister(key.fileobj)
                key.fileobj.close()
            continue
        try:
            chunk = os.read(key.fileobj.fileno(), 65536)
        except BlockingIOError:
            continue
        if not chunk:
            selector.unregister(key.fileobj)
            key.fileobj.close()
        elif len(buffers[key.data]) < limit:
            buffers[key.data].extend(chunk[: limit - len(buffers[key.data])])
    if child.poll() is not None:
        for key in list(selector.get_map().values()):
            if key.data == "stdin":
                selector.unregister(key.fileobj)
                key.fileobj.close()
child.wait()
sys.stdout.buffer.write(bytes(buffers["stdout"]))
sys.stderr.buffer.write(bytes(buffers["stderr"]))
sys.stdout.flush()
sys.stderr.flush()
raise SystemExit(child.returncode)
"""

def _encoded_json(value: Any) -> str:
    return base64.b64encode(canonical_bytes(value)).decode('ascii')

def _guardian_argv(lock_fd: int | str, report_fd: int | str, control_fd: int | str, command_encoded: str, cwd: Path,
        env_encoded: str, stdin_length: int, stdin_sha256: str, stdin_limit: int=MAX_OUTPUT) -> list[str]:
    return [sys.executable, '-c', _GUARDIAN, str(lock_fd), str(report_fd), str(control_fd), command_encoded, str(cwd),
            env_encoded, str(MAX_OUTPUT), str(stdin_length), stdin_sha256, str(EXEC_FD_DECIMAL_RESERVE), _CHILD_GATE,
            str(stdin_limit)]

def _child_gate_argv(release_fd: int | str, command_encoded: str, cwd: Path, env_encoded: str) -> list[str]:
    return [sys.executable, '-c', _CHILD_GATE, str(release_fd), command_encoded, str(cwd), env_encoded]

def _exec_vector_size(argv: Sequence[str], env: Mapping[str, str]) -> tuple[list[int], int]:
    entries = [*argv, *(f'{key}={value}' for key, value in sorted(env.items()))]
    sizes = [len(os.fsencode(entry)) + 1 for entry in entries]
    pointer_bytes = (len(argv) + len(env) + 2) * struct.calcsize('P')
    return (sizes, sum(sizes) + pointer_bytes)

def _validate_exec_vector(argv: Sequence[str], env: Mapping[str, str], label: str) -> None:
    sizes, aggregate = _exec_vector_size(argv, env)
    labels = [*(f'argv[{index}]' for index in range(len(argv))),
            *(f'environment.{key}' for key in sorted(env))]
    for entry_label, size in zip(labels, sizes, strict=True):
        if size > EXEC_ENTRY_LIMIT:
            raise PlanValidationError(f'{label} {entry_label} exceeds the process-exec entry limit')
    if aggregate > EXEC_AGGREGATE_LIMIT:
        raise PlanValidationError(f'{label} exceeds the aggregate process-exec limit')

def _validate_effect_exec_vectors(command: Sequence[str], cwd: Path, env: Mapping[str, str], stdin_length: int,
        stdin_sha256: str, lock_fd: int, stdin_limit: int=MAX_OUTPUT) -> tuple[str, str]:
    lock_arg = str(lock_fd)
    if len(lock_arg) > EXEC_FD_DECIMAL_RESERVE:
        raise PlanValidationError('operation lock descriptor exceeds the process-exec reserve')
    _validate_exec_vector(command, env, 'effect child')
    command_encoded = _encoded_json(command)
    env_encoded = _encoded_json(env)
    reserved_fd = '9' * EXEC_FD_DECIMAL_RESERVE
    guardian_argv = _guardian_argv(lock_arg, reserved_fd, reserved_fd, command_encoded, cwd, env_encoded,
            stdin_length, stdin_sha256, stdin_limit)
    child_gate_argv = _child_gate_argv(reserved_fd, command_encoded, cwd, env_encoded)
    _validate_exec_vector(guardian_argv, env, 'effect guardian')
    _validate_exec_vector(child_gate_argv, env, 'effect child gate')
    return (command_encoded, env_encoded)

def _descriptor_arg(fd: int) -> str:
    value = str(fd)
    if len(value) > EXEC_FD_DECIMAL_RESERVE:
        raise EffectBlocked('allocated descriptor exceeds the process-exec reserve')
    return value

def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass

def _close_stream(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass

def _transfer_guardian_stdin(process: subprocess.Popen[bytes], payload: bytes, deadline: float, timeout: float) -> None:
    stream = process.stdin
    if stream is None:
        raise EffectBlocked('effect guardian stdin is unavailable')
    if not payload:
        stream.close()
        return
    fd = stream.fileno()
    os.set_blocking(fd, False)
    selector = selectors.DefaultSelector()
    view = memoryview(payload)
    try:
        selector.register(fd, selectors.EVENT_WRITE)
        while view:
            if process.poll() is not None:
                raise EffectBlocked('effect guardian exited during stdin transfer')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EffectBlocked(f'effect guardian stdin transfer exceeded {timeout} seconds')
            events = selector.select(min(0.1, remaining))
            if not events:
                continue
            try:
                written = os.write(fd, view[:EXEC_WRITE_CHUNK])
            except (BrokenPipeError, OSError) as error:
                raise EffectBlocked('effect guardian closed stdin during transfer') from error
            if type(written) is not int or written <= 0 or written > len(view):
                raise EffectBlocked('effect guardian stdin transfer made no valid progress')
            view = view[written:]
    finally:
        selector.close()
    stream.close()

def _read_guardian_ack(process: subprocess.Popen[bytes], report_fd: int, deadline: float, timeout: float) -> int:
    selector = selectors.DefaultSelector()
    data = bytearray()
    os.set_blocking(report_fd, False)
    try:
        selector.register(report_fd, selectors.EVENT_READ)
        while b'\n' not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EffectBlocked(f'effect guardian acknowledgement exceeded {timeout} seconds')
            events = selector.select(min(0.1, remaining))
            if not events:
                if process.poll() is not None:
                    raise EffectBlocked('effect guardian acknowledgement was lost')
                continue
            try:
                chunk = os.read(report_fd, 4096 - len(data))
            except BlockingIOError:
                continue
            if not chunk:
                raise EffectBlocked('effect guardian acknowledgement was lost')
            data.extend(chunk)
            if len(data) >= 4096 and b'\n' not in data:
                raise EffectBlocked('effect guardian acknowledgement was too large')
    finally:
        selector.close()
    line, separator, trailing = bytes(data).partition(b'\n')
    if separator != b'\n' or trailing:
        raise EffectBlocked('effect guardian acknowledgement was invalid')
    try:
        acknowledgement = json.loads(line.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EffectBlocked('effect guardian acknowledgement was invalid') from error
    if not isinstance(acknowledgement, dict) or set(acknowledgement) != {'pid'}:
        raise EffectBlocked('effect guardian acknowledgement was invalid')
    child_pid = acknowledgement['pid']
    if type(child_pid) is not int or child_pid <= 0:
        raise EffectBlocked('effect guardian acknowledgement was invalid')
    if not _pid_alive(child_pid):
        raise EffectBlocked('effect child is not alive after acknowledgement')
    return child_pid

def _bounded_communicate(process: subprocess.Popen[bytes], timeout: float, limit: int=MAX_OUTPUT) -> tuple[bytes,
        bytes]:
    selector = selectors.DefaultSelector()
    streams: dict[Any, bytearray] = {}
    buffers: dict[Any, bytearray] = {}
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            streams[stream] = bytearray()
            buffers[stream] = streams[stream]
            selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
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
                    available = limit - len(streams[key.fileobj])
                    streams[key.fileobj].extend(chunk[:available])
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
        return (bytes(buffers.get(process.stdout, b'')), bytes(buffers.get(process.stderr, b'')))
    finally:
        selector.close()
        if process.poll() is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

@dataclass(frozen=True)
class EffectResult:
    command_digest: str
    returncode: int
    stdout_sha256: str
    stderr_sha256: str
    guardian_pid: int | None
    child_pid: int | None
    settlement: str | None = None

def _effect_inputs(command: Sequence[str], cwd: Path, extra_env: Mapping[str, str] | None, stdin_bytes: bytes | None,
        preimage_sha256: str | None, postimage_sha256: str | None, *, stdin_limit: int=MAX_OUTPUT) -> tuple[list[str], Path, dict[str, str], bytes,
        dict[str, Any]]:
    argv = _argv(list(command), 'effect command')
    executable = Path(argv[0])
    if not executable.is_absolute() or executable.is_symlink() or (not executable.is_file()) or (not
            os.access(executable, os.X_OK)):
        raise PlanValidationError('effect executable must be a regular executable file')
    root = _absolute_path(str(cwd), 'effect cwd')
    if not root.is_dir() or root.is_symlink():
        raise PlanValidationError('effect cwd must be a regular directory')
    stdin = stdin_bytes if stdin_bytes is not None else b''
    if type(stdin_limit) is not int or stdin_limit not in (MAX_OUTPUT, 1024 * 1024):
        raise PlanValidationError('effect stdin bound is invalid')
    if len(stdin) > stdin_limit:
        raise PlanValidationError('effect stdin is too large')
    for name, value in (('preimage_sha256', preimage_sha256), ('postimage_sha256', postimage_sha256)):
        if value is not None:
            _sha256(value, name)
    env = _safe_env(extra_env)
    identity = {'command_digest': canonical_sha256(argv), 'cwd': str(root), 'environment_sha256': canonical_sha256(env),
            'stdin_sha256': sha256_bytes(stdin), 'preimage_sha256': preimage_sha256, 'postimage_sha256':
            postimage_sha256}
    return (argv, root, env, stdin, identity)

def _verify_effect_identity(entry: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for key, value in expected.items():
        if entry.get(key) != value:
            raise JournalError(f'effect identity drifted: {key}')

def _wait_owned_until(process: subprocess.Popen[bytes], deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return process.poll() is not None
    try:
        process.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired):
        return process.poll() is not None
    return True

def _terminate_owned_during_setup(process: subprocess.Popen[bytes], deadline: float) -> None:
    kill_deadline = deadline + SETUP_KILL_REAP_GRACE
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if not _wait_owned_until(process, deadline):
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                pass
            _wait_owned_until(process, kill_deadline)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            _close_stream(stream)

def _terminate_owned(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    except OSError:
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

def run_effect(journal: OperationJournal, *, effect_key: str, command: Sequence[str], cwd: Path, timeout: float=30.0,
        preimage_sha256: str | None=None, postimage_sha256: str | None=None, extra_env: Mapping[str, str] | None=None,
        stdin_bytes: bytes | None=None, current_sha256: Callable[[], str] | None=None,
        pre_release_check: Callable[[], Any] | None=None, stdin_limit: int=MAX_OUTPUT) -> EffectResult:
    if timeout <= 0 or timeout > 300:
        raise PlanValidationError('effect timeout must be between 0 and 300 seconds')
    if not callable(current_sha256):
        raise PlanValidationError('effect requires an exact current-state readback')
    command, cwd, env, stdin_bytes, identity = _effect_inputs(command, cwd, extra_env, stdin_bytes, preimage_sha256,
            postimage_sha256, stdin_limit=stdin_limit)
    lock_fd = journal._lock_fd
    if lock_fd is None:
        raise JournalError('effect requires the held operation lock')
    command_encoded, env_encoded = _validate_effect_exec_vectors(command, cwd, env, len(stdin_bytes),
            identity['stdin_sha256'], lock_fd, stdin_limit)
    state = journal.state or {}
    effects = dict(state.get('effects') or {})
    previous = effects.get(effect_key)
    if isinstance(previous, dict):
        status = previous.get('status')
        if status != 'settled_preimage':
            raise JournalError(f'effect {effect_key!r} is already {status!r}')
        _verify_effect_identity(previous, identity)
        if previous.get('settled_invocation') == journal.invocation_id:
            raise JournalError('same-cycle effect replay is forbidden')
    effects[effect_key] = {**identity, 'status': 'attempted', 'attempt': int((previous or {}).get('attempt', 0)) + 1,
            'child': None}
    journal.persist({**state, 'phase': state.get('phase', 'in_progress'), 'effects': effects})
    attempt = effects[effect_key]['attempt']
    read_fd: int | None = None
    write_fd: int | None = None
    control_read: int | None = None
    control_write: int | None = None
    guardian: subprocess.Popen[bytes] | None = None
    child_pid: int | None = None
    released = False
    try:
        read_fd, write_fd = os.pipe()
        control_read, control_write = os.pipe()
        deadline = time.monotonic() + timeout
        guardian_argv = _guardian_argv(_descriptor_arg(lock_fd), _descriptor_arg(write_fd),
                _descriptor_arg(control_read), command_encoded, cwd, env_encoded, len(stdin_bytes),
                identity['stdin_sha256'], stdin_limit)
        guardian = subprocess.Popen(guardian_argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(lock_fd, write_fd, control_read),
                start_new_session=True, close_fds=True)
        os.close(write_fd)
        write_fd = None
        os.close(control_read)
        control_read = None
        _transfer_guardian_stdin(guardian, stdin_bytes, deadline, timeout)
        child_pid = _read_guardian_ack(guardian, read_fd, deadline, timeout)
        os.close(read_fd)
        read_fd = None
        expected_entry = {**identity, 'status': 'attempted', 'attempt': attempt,
                'child': {'guardian_pid': guardian.pid, 'child_pid': child_pid}}
        effects[effect_key] = expected_entry
        journal.persist({**journal.state, 'effects': effects})
        persisted = ((journal.state or {}).get('effects') or {}).get(effect_key)
        if persisted != expected_entry:
            raise JournalError('effect child ownership readback mismatch')
        if pre_release_check is not None:
            pre_release_check()
        written = os.write(control_write, b'1')
        if type(written) is not int or written != 1:
            raise EffectBlocked('effect child release write was not confirmed')
        released = True
        _close_fd(control_write)
        control_write = None
    except Exception as error:
        if released:
            raise
        _close_fd(control_write)
        control_write = None
        _close_fd(control_read)
        control_read = None
        _close_fd(read_fd)
        read_fd = None
        _close_fd(write_fd)
        write_fd = None
        if guardian is not None:
            _terminate_owned_during_setup(guardian, deadline)
        repair_state = journal.state or state
        repair_effects = dict(repair_state.get('effects') or effects)
        repair_effects[effect_key] = {**identity, 'status': 'unresolved', 'attempt': attempt, 'child': None}
        try:
            journal.persist({**repair_state, 'effects': repair_effects})
        except Exception as repair_error:
            raise EffectBlocked('effect setup failed and journal repair could not be persisted') from repair_error
        if isinstance(error, EffectBlocked):
            raise
        raise EffectBlocked('effect setup failed before child release') from error
    if guardian is None or child_pid is None:
        raise EffectBlocked('effect guardian setup completed without durable ownership')
    try:
        stdout, stderr = _bounded_communicate(guardian, timeout)
    except subprocess.TimeoutExpired as error:
        _terminate_owned(guardian)
        effects[effect_key]['status'] = 'unresolved'
        effects[effect_key]['result'] = {'timed_out': True}
        journal.persist({**journal.state, 'effects': effects})
        try:
            settlement = _settle_effect(journal, effect_key, current_sha256(), preimage_sha256, postimage_sha256)
        except EffectBlocked:
            raise EffectBlocked(f'effect {effect_key!r} timed out ambiguously') from error
        return EffectResult(identity['command_digest'], guardian.returncode or 124, sha256_bytes(b''),
                sha256_bytes(b''), guardian.pid, child_pid, settlement)
    result = EffectResult(identity['command_digest'], guardian.returncode or 0, sha256_bytes(stdout),
            sha256_bytes(stderr), guardian.pid, child_pid)
    effects[effect_key]['result'] = {'returncode': result.returncode, 'stdout_sha256': result.stdout_sha256,
            'stderr_sha256': result.stderr_sha256}
    effects[effect_key]['status'] = 'unresolved'
    journal.persist({**journal.state, 'effects': effects})
    settlement = _settle_effect(journal, effect_key, current_sha256(), preimage_sha256, postimage_sha256)
    return EffectResult(result.command_digest, result.returncode, result.stdout_sha256, result.stderr_sha256,
            result.guardian_pid, result.child_pid, settlement)

def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True

def _settle_effect(journal: OperationJournal, effect_key: str, current: str, preimage_sha256: str | None,
        postimage_sha256: str | None) -> str:
    current = _sha256(current, 'current_sha256')
    state = journal.state or {}
    entry = (state.get('effects') or {}).get(effect_key)
    if not isinstance(entry, dict):
        raise JournalError(f'effect {effect_key!r} is not journaled')
    if entry.get('preimage_sha256') != preimage_sha256:
        raise JournalError('effect preimage identity drifted')
    if entry.get('postimage_sha256') != postimage_sha256:
        raise JournalError('effect postimage identity drifted')
    if postimage_sha256 and current == postimage_sha256:
        entry['status'] = 'verified'
        journal.persist({**state, 'effects': state['effects']})
        return 'postimage'
    if preimage_sha256 and current == preimage_sha256:
        entry['status'] = 'settled_preimage'
        entry['settled_invocation'] = journal.invocation_id
        journal.persist({**state, 'effects': state['effects']})
        return 'preimage'
    entry['status'] = 'ambiguous'
    journal.persist({**state, 'effects': state['effects']})
    raise EffectBlocked('effect completion is ambiguous')

def recover_effect(journal: OperationJournal, *, effect_key: str, command: Sequence[str], cwd: Path, preimage_sha256:
        str | None, postimage_sha256: str | None, current_sha256: Callable[[], str], extra_env: Mapping[str, str] |
        None=None, stdin_bytes: bytes | None=None, stdin_limit: int=MAX_OUTPUT) -> str:
    if not callable(current_sha256):
        raise PlanValidationError('effect recovery requires an exact readback')
    _, _, _, _, identity = _effect_inputs(command, cwd, extra_env, stdin_bytes, preimage_sha256, postimage_sha256,
            stdin_limit=stdin_limit)
    recovery_phases = {'prepared', 'in_progress', 'activation_committed'}
    if journal.operation == RELEASE_LIVE_PORTFOLIO_OPERATION:
        recovery_phases.update({'retirement_in_progress', 'd9_complete', 'canonical_in_progress'})
    state = journal.require_phase(recovery_phases)
    entry = (state.get('effects') or {}).get(effect_key)
    if not isinstance(entry, dict):
        raise JournalError(f'effect {effect_key!r} is not awaiting settlement')
    _verify_effect_identity(entry, identity)
    if entry.get('status') == 'settled_preimage':
        if entry.get('settled_invocation') == journal.invocation_id:
            raise JournalError('same-cycle settled effect replay is forbidden')
        raise JournalError('settled preimage requires a new effect attempt')
    if entry.get('status') not in {'attempted', 'unresolved', 'ambiguous'}:
        raise JournalError('effect is not awaiting settlement')
    return _settle_effect(journal, effect_key, current_sha256(), preimage_sha256, postimage_sha256)

def _settle_or_run(journal: OperationJournal, *, effect_key: str, command: Sequence[str], cwd: Path, preimage_sha256:
        str, postimage_sha256: str, current_sha256: Callable[[], str], extra_env: Mapping[str, str] | None=None,
        stdin_bytes: bytes | None=None, timeout: float=30.0,
        pre_release_check: Callable[[], Any] | None=None, stdin_limit: int=MAX_OUTPUT) -> str:
    entry = (journal.state.get('effects') or {}).get(effect_key)
    if isinstance(entry, dict) and entry.get('status') == 'verified':
        if current_sha256() != postimage_sha256:
            raise EffectBlocked(f'verified effect {effect_key!r} postimage drifted')
        return 'postimage'
    if isinstance(entry, dict) and entry.get('status') in {'attempted', 'unresolved', 'ambiguous'}:
        return recover_effect(journal, effect_key=effect_key, command=command, cwd=cwd, preimage_sha256=preimage_sha256,
                postimage_sha256=postimage_sha256, current_sha256=current_sha256, extra_env=extra_env,
                stdin_bytes=stdin_bytes, stdin_limit=stdin_limit)
    return run_effect(journal, effect_key=effect_key, command=command, cwd=cwd, timeout=timeout,
            preimage_sha256=preimage_sha256, postimage_sha256=postimage_sha256, current_sha256=current_sha256,
            extra_env=extra_env, stdin_bytes=stdin_bytes, pre_release_check=pre_release_check,
            stdin_limit=stdin_limit).settlement or ''

def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 30.0,
    extra_env: Mapping[str, str] | None = None,
    output_limit: int = MAX_OUTPUT,
    stdin: int | None = None,
) -> tuple[int, bytes, bytes]:
    if (
        not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or output_limit < 1
        or output_limit > CANONICAL_GRAPH_MAX_OUTPUT
    ):
        raise OperationError('internal command output bound is invalid')
    if stdin not in (None, subprocess.DEVNULL):
        raise OperationError('internal command stdin mode is unsupported')
    argv = _argv(list(command), 'internal command')
    if not Path(argv[0]).is_absolute():
        raise OperationError('internal command executable must be absolute')
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=_safe_env(extra_env),
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    try:
        out, err = _bounded_communicate(process, timeout, limit=output_limit)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait(timeout=2)
        raise OperationError('internal command timed out') from error
    return (process.returncode or 0, out, err)

def _json_command(
    command: Sequence[str],
    *,
    cwd: Path,
    output_limit: int = MAX_OUTPUT,
) -> dict[str, Any]:
    code, out, err = _run(command, cwd=cwd, output_limit=output_limit)
    if code:
        raise OperationError(err.decode(errors='replace') or 'command failed')
    try:
        value = json.loads(out.decode('utf-8'), object_pairs_hook=_duplicate_free)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OperationError('internal command did not return JSON') from error
    if not isinstance(value, dict):
        raise OperationError('internal command returned a non-object')
    return value

def _kent_path(value: Any, label: str='kent') -> Path:
    path = _absolute_path(value, label)
    if path.is_symlink() or not path.is_file() or (not os.access(path, os.X_OK)):
        raise PlanValidationError(f'{label} is not an executable file')
    return path

def _verify_executable(path: Path, digest: str) -> None:
    if sha256_bytes(path.read_bytes()) != _sha256(digest, 'kent_sha256'):
        raise PlanValidationError('Kent executable bytes do not match the plan')

def _kent_json(
    kent: Path,
    args: Sequence[str],
    *,
    cwd: Path,
    output_limit: int = MAX_OUTPUT,
) -> dict[str, Any]:
    return _json_command([str(kent), *args], cwd=cwd, output_limit=output_limit)

def _kent_optional(kent: Path, args: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    code, out, err = _run([str(kent), *args], cwd=cwd)
    if code == 0:
        try:
            value = json.loads(out.decode('utf-8'), object_pairs_hook=_duplicate_free)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OperationError('Kent command did not return JSON') from error
        if not isinstance(value, dict):
            raise OperationError('Kent command returned a non-object')
        return value
    text = (err + out).decode('utf-8', errors='replace').lower()
    if 'not found' in text or 'no such' in text or 'unknown workflow' in text:
        return {'present': False}
    raise OperationError(err.decode(errors='replace') or 'Kent command failed')

def _git(root: Path, *args: str, check: bool=True) -> str:
    command = ['/usr/bin/git', '-C', str(root), *args]
    code, out, err = _run(command, cwd=root)
    if check and code:
        raise OperationError(err.decode(errors='replace') or out.decode(errors='replace'))
    return out.decode().strip()

def _repository_identity(root: Path) -> str:
    remote = _git(root, 'config', '--get', 'remote.origin.url', check=False)
    if not remote:
        raise OperationError(f'{root} has no origin identity')
    import re
    match = re.search('(?:github\\.com[:/])([^/:\\s]+/[^/\\s]+?)(?:\\.git)?$', remote)
    if not match:
        raise OperationError(f'{root} has an unrecognized origin identity')
    return match.group(1)

def _atomic_write(path: Path, data: bytes) -> None:
    path = _absolute_path(str(path), 'report_path')
    if path.is_symlink():
        raise JournalError('report path must not be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=448)
    lock_path = path.with_name(f'.{path.name}.lock')
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, flags, 384)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(lock_fd)
        raise JournalError('report lock is held by another writer') from error
    temporary = path.parent / f'.{path.name}.tmp'
    try:
        if temporary.exists():
            raise JournalError('deterministic report temporary file already exists')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
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
            raise JournalError('report readback mismatch')
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

def verify_release_portfolio(plan: LoadedPlan, *, report_path: Path | None=None, write_report: bool=False) -> dict[str,
        Any]:
    if report_path is not None and (not write_report):
        raise PlanValidationError('report_path is allowed only with write_report')
    data = _closed(plan.value, {'schema', 'kit', 'projects', 'report_path'}, 'portfolio plan')
    kit = _closed(_required(data, 'kit', 'portfolio plan'), {'root', 'repository', 'commit'}, 'kit')
    kit_root = _absolute_path(_required(kit, 'root', 'kit'), 'kit.root')
    kit_repo = _repository(_required(kit, 'repository', 'kit'), 'kit.repository')
    kit_commit = _sha1(_required(kit, 'commit', 'kit'), 'kit.commit')
    projects = _bounded_list(_required(data, 'projects', 'portfolio plan'), 'projects')
    if len(projects) != 4:
        raise PlanValidationError('portfolio plan must bind exactly four projects')
    records = []
    identities = []
    _git(kit_root, 'cat-file', '-e', f'{kit_commit}^{{commit}}')
    if _repository_identity(kit_root) != kit_repo:
        raise OperationError('Kit repository identity does not match the plan')
    for index, raw in enumerate(projects):
        item = _closed(raw, {'root', 'repository', 'commit'}, f'projects[{index}]')
        root = _absolute_path(_required(item, 'root', f'projects[{index}]'), 'project.root')
        repository = _repository(_required(item, 'repository', f'projects[{index}]'), 'project.repository')
        commit = _sha1(_required(item, 'commit', f'projects[{index}]'), 'project.commit')
        identities.append(repository)
        if _repository_identity(root) != repository:
            raise OperationError('project repository identity does not match the plan')
        try:
            result = preflight_project_revision(root, commit)
        except RevisionPreflightError as error:
            raise OperationError(str(error)) from error
        if result.commit_oid != commit:
            raise OperationError('selected project commit changed during preflight')
        if result.release_preview is None or result.runtime_source_inputs is None:
            raise OperationError('schema-4 release/runtime inputs are incomplete')
        source_digests = _selected_source_digests(root, commit)
        records.append({'repository': repository, 'commit': commit, 'profile_sha256': source_digests['profile_sha256'],
                'release_spec_sha256': source_digests['release_spec_sha256'], 'source_manifest_sha256':
                source_digests['source_manifest_sha256'], 'snapshot_sha256': source_digests['snapshot_sha256'],
                'builder_sha256': source_digests['builder_sha256'], 'release_preview_sha256':
                canonical_sha256(result.release_preview) if result.release_preview is not None else None,
                'runtime_source_inputs_sha256': result.runtime_source_inputs.selected_runtime_source_inputs_sha256 if
                result.runtime_source_inputs is not None else None})
    _unique(identities, 'projects')
    report = {'schema': 'release-portfolio-report-v2', 'plan_sha256': plan.sha256, 'kit': {'repository': kit_repo,
            'commit': kit_commit}, 'projects': sorted(records, key=lambda item: item['repository']), 'ready': True}
    if write_report:
        if 'report_path' not in data:
            raise PlanValidationError('write_report requires a plan-bound report_path')
        target = _absolute_path(data['report_path'], 'report_path')
        if report_path is not None and Path(report_path) != target:
            raise PlanValidationError('report path differs from the plan binding')
        _atomic_write(target, canonical_bytes(report) + b'\n')
    elif report_path is not None:
        raise PlanValidationError('report_path is allowed only with write_report')
    return report

def _blob_digest(root: Path, commit: str, path: str) -> str:
    out = _blob_bytes(root, commit, path)
    return sha256_bytes(out)

def _blob_bytes(root: Path, commit: str, path: str) -> bytes:
    code, out, err = _run(['/usr/bin/git', '-C', str(root), 'show', f'{commit}:{path}'], cwd=root)
    if code:
        raise OperationError(err.decode(errors='replace') or f'missing {path}')
    return out

def _selected_source_digests(root: Path, commit: str) -> dict[str, Any]:
    profile_path = '.kent/workflow-profile.toml'
    profile_bytes = _blob_bytes(root, commit, profile_path)
    try:
        profile = ProjectProfile.from_toml(root, profile_bytes.decode('utf-8'), source=f'{commit}:{profile_path}',
                check_files=False)
        if profile.schema_version != 4:
            raise OperationError('portfolio requires schema-4 projects')
        spec_path = profile.release.spec_path
        spec_bytes = _blob_bytes(root, commit, spec_path)
        spec = ReleaseSpec.from_toml(spec_bytes.decode('utf-8'), profile=profile)
        manifest_path = spec.source_manifest.path
        manifest_bytes = _blob_bytes(root, commit, manifest_path)
        snapshot_bytes = _blob_bytes(root, commit, profile.release.snapshot_path)
        builder_bytes = _blob_bytes(root, commit, profile.release.builder_path) if profile.release.builder_path else \
                None
    except (AttributeError, UnicodeDecodeError, ValueError, KeyError) as error:
        raise OperationError('selected release source is not schema-4 complete') from error
    return {'profile_sha256': sha256_bytes(profile_bytes), 'release_spec_sha256': sha256_bytes(spec_bytes),
            'source_manifest_sha256': sha256_bytes(manifest_bytes), 'snapshot_sha256': sha256_bytes(snapshot_bytes),
            'builder_sha256': sha256_bytes(builder_bytes) if builder_bytes else None}

def _reject_raw_protocol_fields(value: Mapping[str, Any], label: str) -> None:
    forbidden = {'command', 'commands', 'argv', 'probe', 'absence_probe', 'sql', 'script', 'shell', 'executable'}
    found = sorted(forbidden & set(value))
    if found:
        raise PlanValidationError(f'{label} contains forbidden protocol fields: {found}')

def _manifest_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _manifest_require_primitives() -> None:
    # Check capabilities by name so instrumentation of these functions does not
    # change the platform's advertised support.
    if (
        not {'open', 'stat', 'readlink'} <= {
            function.__name__ for function in getattr(os, 'supports_dir_fd', ())
        }
        or 'scandir' not in {function.__name__ for function in getattr(os, 'supports_fd', ())}
        or 'stat' not in {function.__name__ for function in getattr(os, 'supports_follow_symlinks', ())}
        or any(not getattr(os, name, 0) for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK'))
    ):
        raise OperationError('descriptor-relative manifest primitives are unavailable')


def _manifest_open(
    parent: int | None,
    name: str,
    expected: os.stat_result,
    *,
    directory: bool,
    label: str,
) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        valid_type = stat.S_ISDIR(opened.st_mode) if directory else stat.S_ISREG(opened.st_mode)
        if not valid_type or _manifest_fingerprint(opened) != _manifest_fingerprint(expected):
            raise OperationError(f'{label} manifest entry changed before opening')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _manifest_root(
    path: Path,
    label: str,
    expected: Mapping[str, tuple[int, ...]] | None = None,
) -> Iterator[tuple[int, dict[str, tuple[int, ...]]]]:
    """Acquire every root component relative to a verified owned descriptor.

    Existing ancestor aliases (including platform temporary-directory aliases)
    are resolved with readlinkat and retained in the anchor preimage. The root
    itself and all manifest descendants must be real directories/files.
    """
    _manifest_require_primitives()
    if '..' in path.parts:
        raise OperationError(f'invalid {label} manifest root path')
    pending = list(path.absolute().parts[1:])
    physical = Path('/')
    anchors: dict[str, tuple[int, ...]] = {}
    descriptor: int | None = None

    def remember(location: Path, info: os.stat_result) -> None:
        # Unrelated siblings can change ancestor timestamps. Bind directory
        # identity/mode here; the manifest root has a full snapshot below.
        identity = (
            _manifest_fingerprint(info) if stat.S_ISLNK(info.st_mode)
            else (info.st_dev, info.st_ino, info.st_mode)
        )
        key = location.as_posix()
        if (
            (key in anchors and anchors[key] != identity)
            or (expected is not None and expected.get(key) != identity)
        ):
            raise OperationError(f'{label} manifest root path changed')
        anchors[key] = identity

    try:
        root_info = os.stat('/', follow_symlinks=False)
        remember(Path('/'), root_info)
        descriptor = _manifest_open(None, '/', root_info, directory=True, label=label)
        traversed = 0
        while pending:
            traversed += 1
            if traversed > MAX_LIST or len(pending) > MAX_LIST:
                raise OperationError(f'{label} manifest root path is too large')
            name, *pending = pending
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if name == '..':
                replacement = _manifest_open(descriptor, name, info, directory=True, label=label)
                os.close(descriptor)
                descriptor = replacement
                physical = physical.parent
                remember(physical, info)
                continue
            location = physical / name
            remember(location, info)
            if stat.S_ISLNK(info.st_mode):
                if not pending:
                    raise OperationError(f'{label} manifest refuses a symlink root')
                target = os.readlink(name, dir_fd=descriptor)
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _manifest_fingerprint(after) != _manifest_fingerprint(info):
                    raise OperationError(f'{label} manifest ancestor alias changed')
                # Resolve target components in order: a preceding symlink can
                # change which pinned directory a later '..' must ascend from.
                target_path = Path(target)
                target_parts = target_path.parts
                if not target_path.is_absolute():
                    pending = list(target_parts) + pending
                    continue
                pending = list(target_parts[1:]) + pending
                physical = Path('/')
                remember(physical, root_info)
                replacement = _manifest_open(None, '/', root_info, directory=True, label=label)
            else:
                replacement = _manifest_open(descriptor, name, info, directory=True, label=label)
                physical = location
            os.close(descriptor)
            descriptor = replacement
        if expected is not None and anchors != expected:
            raise OperationError(f'{label} manifest root path changed')
        yield descriptor, anchors
    except OSError as error:
        raise OperationError(f'{label} manifest directory or entry cannot be accessed safely') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _manifest_directory(
    root: int,
    relative: Path,
    snapshot: Mapping[str, tuple[str, tuple[int, ...]]],
    label: str,
) -> Iterator[int]:
    # Rewalk from the pinned root instead of retaining one FD per entry/depth.
    # At most root, current directory, and its next child are open here.
    descriptor = os.dup(root)
    try:
        current = Path()
        if snapshot['.'] != ('directory', _manifest_fingerprint(os.fstat(descriptor))):
            raise OperationError(f'{label} manifest root changed')
        for name in relative.parts:
            current /= name
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if snapshot.get(current.as_posix()) != ('directory', _manifest_fingerprint(info)):
                raise OperationError(f'{label} manifest directory changed')
            replacement = _manifest_open(descriptor, name, info, directory=True, label=label)
            os.close(descriptor)
            descriptor = replacement
        yield descriptor
    finally:
        os.close(descriptor)


def _manifest_entries(
    root: int,
    label: str,
) -> tuple[list[tuple[Path, str, os.stat_result]], dict[str, tuple[str, tuple[int, ...]]]]:
    entries: list[tuple[Path, str, os.stat_result]] = []
    snapshot = {'.': ('directory', _manifest_fingerprint(os.fstat(root)))}
    pending = [Path()]
    while pending:
        relative = pending.pop()
        with _manifest_directory(root, relative, snapshot, label) as directory:
            remaining = MAX_LIST - len(entries)
            children: list[os.DirEntry[str]] = []
            with os.scandir(directory) as stream:
                for entry in stream:
                    children.append(entry)
                    if len(children) > remaining:
                        raise OperationError(f'{label} manifest is too large')
            children.sort(key=lambda entry: entry.name)
            child_directories: list[Path] = []
            for entry in children:
                name = entry.name
                if not name or name in {'.', '..'} or Path(name).name != name:
                    raise OperationError(f'invalid {label} manifest path')
                child_relative = relative / name
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise OperationError(f'{label} manifest refuses symlinks')
                if stat.S_ISDIR(info.st_mode):
                    kind = 'directory'
                    child_directories.append(child_relative)
                elif stat.S_ISREG(info.st_mode):
                    kind = 'file'
                else:
                    raise OperationError(f'{label} manifest refuses non-regular files')
                entries.append((child_relative, kind, info))
                snapshot[child_relative.as_posix()] = (kind, _manifest_fingerprint(info))
            if snapshot[relative.as_posix()] != ('directory', _manifest_fingerprint(os.fstat(directory))):
                raise OperationError(f'{label} manifest directory changed during enumeration')
            pending.extend(reversed(child_directories))
    return entries, snapshot


def _manifest_records(root: int, *, file_limit: int, total_limit: int, label: str) -> list[dict[str, Any]]:
    entries, snapshot = _manifest_entries(root, label)
    total_bytes = 0
    records: list[dict[str, Any]] = []
    file_entries: list[tuple[Path, os.stat_result]] = []
    for relative, kind, info in entries:
        if snapshot.get(relative.as_posix()) != (
            kind,
            _manifest_fingerprint(info),
        ):
            raise OperationError(f'{label} manifest entry changed during enumeration')
        if kind == 'directory':
            records.append({
                'path': relative.as_posix(),
                'type': 'directory',
                'mode': stat.S_IMODE(info.st_mode),
                'bytes': 0,
            })
            continue
        if info.st_size > file_limit:
            raise OperationError(f'{label} manifest file exceeds the bound')
        total_bytes += info.st_size
        if total_bytes > total_limit:
            raise OperationError(f'{label} manifest exceeds the byte bound')
        file_entries.append((relative, info))

    for relative, expected in file_entries:
        with _manifest_directory(root, relative.parent, snapshot, label) as parent:
            descriptor = _manifest_open(parent, relative.name, expected, directory=False, label=label)
            try:
                digest = hashlib.sha256()
                bytes_read = 0
                while True:
                    chunk = os.read(descriptor, MANIFEST_READ_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    bytes_read += len(chunk)
                    if bytes_read > expected.st_size:
                        raise OperationError(f'{label} manifest file grew while hashing')
                after = os.fstat(descriptor)
                path_info = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                if (
                    bytes_read != expected.st_size
                    or _manifest_fingerprint(after) != _manifest_fingerprint(expected)
                    or _manifest_fingerprint(path_info) != _manifest_fingerprint(after)
                ):
                    raise OperationError(f'{label} manifest file changed while hashing')
            finally:
                os.close(descriptor)
        records.append({
            'path': relative.as_posix(),
            'type': 'file',
            'mode': stat.S_IMODE(expected.st_mode),
            'bytes': expected.st_size,
            'sha256': digest.hexdigest(),
        })

    _, snapshot_after = _manifest_entries(root, label)
    if snapshot_after != snapshot:
        raise OperationError(f'{label} manifest state changed')
    return sorted(records, key=lambda record: record['path'])


def _directory_manifest(path: Path, *, file_limit: int, total_limit: int, label: str) -> list[dict[str, Any]]:
    with _manifest_root(path, label) as (root, anchors):
        before = _manifest_fingerprint(os.fstat(root))
        records = _manifest_records(root, file_limit=file_limit, total_limit=total_limit, label=label)
        with _manifest_root(path, label, anchors) as (verified, _):
            if _manifest_fingerprint(os.fstat(verified)) != before:
                raise OperationError(f'{label} manifest root changed')
        return records


def _session_manifest(path: Path) -> list[dict[str, Any]]:
    return _directory_manifest(
        path,
        file_limit=SESSION_MANIFEST_FILE_LIMIT,
        total_limit=SESSION_MANIFEST_TOTAL_LIMIT,
        label='Session',
    )


def _retained_resource_manifest(path: Path) -> list[dict[str, Any]]:
    return _directory_manifest(
        path,
        file_limit=MAX_OUTPUT,
        total_limit=MAX_OUTPUT,
        label='retained resource',
    )


def _session_path(session: Mapping[str, Any], roots: Sequence[Path]) -> Path:
    if session.get('relative') is not None:
        root = _absolute_path(session.get('root'), 'session.root')
        relative = Path(_string(session['relative'], 'session.relative'))
        if relative.is_absolute() or '..' in relative.parts:
            raise OperationError('Session path traversal is forbidden')
        path = root / relative
    else:
        path = _absolute_path(session.get('path'), 'session.path')
    path = path.absolute()
    matching_root = next((root.absolute() for root in roots if path == root.absolute() or root.absolute() in
            path.parents), None)
    if matching_root is None:
        raise EffectBlocked('Session path is outside the declared roots')
    relative_parts = path.relative_to(matching_root).parts
    current = matching_root
    if current.is_symlink():
        raise EffectBlocked('Session root is a symlink')
    for part in relative_parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            raise EffectBlocked('Session path is absent') from None
        if stat.S_ISLNK(info.st_mode):
            raise EffectBlocked('Session path contains a symlink')
    if not current.is_dir():
        raise EffectBlocked('Session path is not a directory')
    return current
KENT_SCHEMA_IDENTITY = 'kent-2.6.1'

def _typed_member(member: Mapping[str, Any], index: int, project_id: str) -> dict[str, Any]:
    label = f'members[{index}]'
    _reject_raw_protocol_fields(member, label)
    item = _closed(member, {'workflow_id', 'revision', 'links', 'default', 'tasks', 'sessions', 'worktrees', 'retained',
            'absent', 'delete_preview'}, label)
    workflow_id = _uuid(_required(item, 'workflow_id', label), f'{label}.workflow_id')
    revision = _required(item, 'revision', label)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise PlanValidationError(f'{label}.revision must be a non-negative integer')
    tasks: list[dict[str, Any]] = []
    for task_index, raw in enumerate(_bounded_list(_required(item, 'tasks', label), f'{label}.tasks')):
        task_label = f'{label}.tasks[{task_index}]'
        task = _closed(raw, {'id', 'status', 'terminal', 'current_node', 'approval_pending'}, task_label)
        parsed_task = {'id': _string(_required(task, 'id', task_label), f'{task_label}.id'), 'status':
                _string(_required(task, 'status', task_label), f'{task_label}.status'), 'terminal': _required(task,
                'terminal', task_label), 'current_node': _required(task, 'current_node', task_label),
                'approval_pending': _required(task, 'approval_pending', task_label)}
        if parsed_task['terminal'] is not True:
            raise PlanValidationError('D9 refuses a nonterminal task')
        if parsed_task['current_node'] is not None:
            raise PlanValidationError('D9 refuses an active Current Node')
        if parsed_task['approval_pending'] is not False:
            raise PlanValidationError('D9 refuses a pending approval')
        tasks.append(parsed_task)
    tasks.sort(key=lambda task: task['id'])
    task_ids = [task['id'] for task in tasks]
    _unique(task_ids, f'{label}.task identities')
    links: list[dict[str, Any]] = []
    for link_index, raw in enumerate(_bounded_list(_required(item, 'links', label), f'{label}.links')):
        link_label = f'{label}.links[{link_index}]'
        link = _closed(raw, {'project_id', 'workflow_id', 'is_default'}, link_label)
        parsed_link = {'project_id': _uuid(_required(link, 'project_id', link_label), f'{link_label}.project_id'),
                'workflow_id': _uuid(_required(link, 'workflow_id', link_label), f'{link_label}.workflow_id'),
                'is_default': _required(link, 'is_default', link_label)}
        if parsed_link['project_id'] != project_id:
            raise PlanValidationError('D9 link project identity drifted')
        if parsed_link['workflow_id'] != workflow_id:
            raise PlanValidationError('D9 link Workflow identity drifted')
        if not isinstance(parsed_link['is_default'], bool):
            raise PlanValidationError('D9 link is_default must be boolean')
        links.append(parsed_link)
    links.sort(key=lambda link: (link['project_id'], link['workflow_id']))
    _unique([f"{link['project_id']}:{link['workflow_id']}" for link in links], f'{label}.link identities')
    default_raw = _required(item, 'default', label)
    default = _uuid(default_raw, f'{label}.default') if default_raw is not None else None
    expected_default = workflow_id if any((link['is_default'] for link in links)) else None
    if default != expected_default:
        raise PlanValidationError('D9 default does not match the project link')
    sessions: list[dict[str, Any]] = []
    live_statuses = {'active', 'running', 'streaming', 'starting'}
    for session_index, raw in enumerate(_bounded_list(_required(item, 'sessions', label), f'{label}.sessions')):
        session_label = f'{label}.sessions[{session_index}]'
        session = _closed(raw, {'id', 'status', 'task_id', 'retained', 'live_owner', 'root', 'relative', 'manifest'},
                session_label)
        relative = Path(_string(_required(session, 'relative', session_label), f'{session_label}.relative'))
        if relative.is_absolute() or '..' in relative.parts:
            raise PlanValidationError('Session relative path is unsafe')
        parsed_session = {'id': _string(_required(session, 'id', session_label), f'{session_label}.id'), 'status':
                _string(_required(session, 'status', session_label), f'{session_label}.status'), 'task_id':
                _string(_required(session, 'task_id', session_label), f'{session_label}.task_id'), 'retained':
                _required(session, 'retained', session_label), 'live_owner': _required(session, 'live_owner',
                session_label), 'root': str(_absolute_path(_required(session, 'root',
                session_label), f'{session_label}.root')), 'relative': relative.as_posix(), 'manifest':
                _bounded_list(_required(session, 'manifest', session_label), f'{session_label}.manifest')}
        if parsed_session['task_id'] not in task_ids:
            raise PlanValidationError('Session belongs to an undeclared Task')
        if parsed_session['retained'] is not True:
            raise PlanValidationError('D9 Session disposition must be retained')
        if parsed_session['live_owner'] is not None:
            raise PlanValidationError('D9 refuses a live Session owner')
        if parsed_session['status'].lower() in live_statuses:
            raise PlanValidationError('D9 refuses a live Session status')
        canonical_bytes(parsed_session['manifest'])
        sessions.append(parsed_session)
    sessions.sort(key=lambda session: session['id'])
    _unique([session['id'] for session in sessions], f'{label}.Session identities')
    worktrees: list[dict[str, Any]] = []
    for worktree_index, raw in enumerate(_bounded_list(_required(item, 'worktrees', label), f'{label}.worktrees')):
        worktree_label = f'{label}.worktrees[{worktree_index}]'
        worktree = _closed(raw, {'path', 'branch', 'head', 'dirty', 'owner_session', 'registered', 'retained'},
                worktree_label)
        parsed_worktree = {'path': str(_absolute_path(_required(worktree, 'path',
                worktree_label), f'{worktree_label}.path')), 'branch': _string(_required(worktree, 'branch',
                worktree_label), f'{worktree_label}.branch'), 'head': _sha1(_required(worktree, 'head',
                worktree_label), f'{worktree_label}.head'), 'dirty': _required(worktree, 'dirty', worktree_label),
                'owner_session': _required(worktree, 'owner_session', worktree_label), 'registered': _required(worktree,
                'registered', worktree_label), 'retained': _required(worktree, 'retained', worktree_label)}
        if parsed_worktree['dirty'] is not False:
            raise PlanValidationError('D9 refuses a dirty worktree')
        if parsed_worktree['owner_session'] is not None:
            raise PlanValidationError('D9 refuses a live-owned worktree')
        if parsed_worktree['registered'] is not True:
            raise PlanValidationError('D9 requires Kent worktree registration')
        if parsed_worktree['retained'] is not True:
            raise PlanValidationError('D9 worktree disposition must be retained')
        worktrees.append(parsed_worktree)
    worktrees.sort(key=lambda worktree: worktree['path'])
    _unique([worktree['path'] for worktree in worktrees], f'{label}.worktree paths')
    resources: dict[str, list[dict[str, Any]]] = {'retained': [], 'absent': []}
    for disposition in resources:
        raw_resources = _bounded_list(_required(item, disposition, label), f'{label}.{disposition}')
        for resource_index, raw in enumerate(raw_resources):
            resource_label = f'{label}.{disposition}[{resource_index}]'
            resource = _closed(raw, {'kind', 'id', 'path', 'sha256'}, resource_label)
            kind = _string(_required(resource, 'kind', resource_label), f'{resource_label}.kind')
            if kind not in {'file', 'directory'}:
                raise PlanValidationError('D9 resource kind is unsupported')
            digest = _required(resource, 'sha256', resource_label)
            if disposition == 'retained':
                digest = _sha256(digest, f'{resource_label}.sha256')
            elif digest is not None:
                raise PlanValidationError('absent resources must have sha256=null')
            resources[disposition].append({'kind': kind, 'id': _string(_required(resource, 'id',
                    resource_label), f'{resource_label}.id'), 'path': str(_absolute_path(_required(resource, 'path',
                    resource_label), f'{resource_label}.path')), 'sha256': digest})
        resources[disposition].sort(key=lambda resource: resource['id'])
        _unique([resource['id'] for resource in resources[disposition]], f'{label}.{disposition} identities')
    retained_ids = {resource['id'] for resource in resources['retained']}
    absent_ids = {resource['id'] for resource in resources['absent']}
    retained_paths = {resource['path'] for resource in resources['retained']}
    absent_paths = {resource['path'] for resource in resources['absent']}
    if retained_ids & absent_ids or retained_paths & absent_paths:
        raise PlanValidationError('D9 retained and absent resources contradict')
    preview = _closed(_required(item, 'delete_preview', label), {'workflow_id', 'sha256'}, f'{label}.delete_preview')
    preview_id = _uuid(_required(preview,
            'workflow_id', f'{label}.delete_preview'), f'{label}.delete_preview.workflow_id')
    if preview_id != workflow_id:
        raise PlanValidationError('delete preview Workflow identity drifted')
    return {'workflow_id': workflow_id, 'revision': revision, 'links': links, 'default': default, 'tasks': tasks,
            'sessions': sessions, 'worktrees': worktrees, 'retained': resources['retained'], 'absent':
            resources['absent'], 'delete_preview': {'workflow_id': preview_id, 'sha256': _sha256(_required(preview,
            'sha256', f'{label}.delete_preview'), f'{label}.delete_preview.sha256')}}

def _validate_d9_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(plan.value, {'schema', 'project_id', 'state_dir', 'kent', 'database', 'members'}, 'retirement plan')
    project_id = _uuid(_required(data, 'project_id', 'retirement plan'), 'project_id')
    state_dir = _absolute_path(_required(data, 'state_dir', 'retirement plan'), 'state_dir')
    kent = _closed(_required(data, 'kent', 'retirement plan'), {'path', 'sha256'}, 'kent')
    kent_path = _kent_path(_required(kent, 'path', 'kent'), 'kent.path')
    kent_sha = _sha256(_required(kent, 'sha256', 'kent'), 'kent.sha256')
    _verify_executable(kent_path, kent_sha)
    database = _closed(_required(data, 'database', 'retirement plan'), {'path', 'schema', 'project_root',
            'session_roots'}, 'database')
    database_path = _absolute_path(_required(database, 'path', 'database'), 'database.path')
    schema = _string(_required(database, 'schema', 'database'), 'database.schema')
    if schema != KENT_SCHEMA_IDENTITY:
        raise PlanValidationError('unsupported Kent persistence schema')
    project_root = _absolute_path(_required(database, 'project_root', 'database'), 'database.project_root')
    session_roots = sorted({_absolute_path(root, 'database.session_root') for root in _bounded_list(_required(database,
            'session_roots', 'database'), 'database.session_roots')}, key=str)
    members = [_typed_member(raw, index, project_id) for index, raw in enumerate(_bounded_list(_required(data,
            'members', 'retirement plan'), 'members'))]
    if not members:
        raise PlanValidationError('retirement plan has no members')
    members.sort(key=lambda member: member['workflow_id'])
    _unique([member['workflow_id'] for member in members], 'Workflow identities')
    roots = {str(root) for root in session_roots}
    if any((session['root'] not in roots for member in members for session in member['sessions'])):
        raise PlanValidationError('Session root is not declared by the database plan')
    return {'project_id': project_id, 'state_dir': state_dir, 'kent': kent_path, 'kent_sha256': kent_sha, 'database':
            database_path, 'schema': schema, 'project_root': project_root, 'session_roots': session_roots, 'members':
            members}

def _kent_pages(kent: Path, args: Sequence[str], *, cwd: Path) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    offset = 0
    total = 0
    while True:
        page = _kent_json(kent, [*args, '--offset', str(offset), '--limit', '100', '--json'], cwd=cwd)
        pages.append(page)
        rows = next((page[key] for key in ('items', 'workflows', 'tasks', 'sessions') if key in page), None)
        if not isinstance(rows, list) or any((not isinstance(row, dict) for row in rows)):
            raise OperationError('Kent pagination returned invalid rows')
        total += len(rows)
        if total > MAX_LIST:
            raise OperationError('Kent pagination exceeded the bounded inventory')
        next_offset = page.get('next_offset')
        if next_offset is None:
            return pages
        if not isinstance(next_offset, int) or isinstance(next_offset, bool) or (not rows) or (next_offset != offset +
                len(rows)):
            raise OperationError('Kent pagination is incomplete or contradictory')
        offset = next_offset

def _page_rows(pages: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict):
            raise OperationError('Kent pagination returned a non-object page')
        values = next((page[key] for key in keys if key in page), None)
        if not isinstance(values, list):
            raise OperationError('Kent pagination returned non-list rows')
        if any((not isinstance(row, dict) for row in values)):
            raise OperationError('Kent pagination returned non-object rows')
        rows.extend(values)
    return rows

def _status_name(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ('kind', 'status', 'name'):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
    raise EffectBlocked('Task status is absent or malformed')

def _row_id(value: Mapping[str, Any], *keys: str) -> str:
    for source in (value, value.get('summary'), value.get('workflow')):
        if isinstance(source, Mapping):
            for key in keys:
                if isinstance(source.get(key), str) and source[key]:
                    return source[key]
    raise EffectBlocked('Kent identity is absent')

def _task_state(value: Mapping[str, Any], fallback_id: str | None=None) -> dict[str, Any]:
    summary = value.get('summary') if isinstance(value.get('summary'), dict) else {}
    task_id = fallback_id or _row_id(value, 'id', 'task_id')
    status_value = value.get('status', summary.get('status'))
    done = value.get('terminal', summary.get('done'))
    native = status_value.get('native_state') if isinstance(status_value, dict) else None
    terminal = done is True or native == 'terminal'
    status = _status_name(status_value if status_value is not None else 'done' if terminal else None)
    current = value.get('current_node', summary.get('current_node'))
    current_nodes = value.get('current_nodes', summary.get('current_nodes', []))
    if current is None and (not terminal) and isinstance(current_nodes, list) and current_nodes:
        node = current_nodes[0]
        current = node.get('node_id', node.get('id')) if isinstance(node, dict) else node
    pending = value.get('approval_pending', summary.get('approval_pending', False))
    pending_rows = value.get('pending_approvals', summary.get('pending_approvals', []))
    if pending_rows:
        pending = True
    if not isinstance(terminal, bool) or not isinstance(pending, bool):
        raise EffectBlocked('Task terminal/approval state is malformed')
    return {'id': task_id, 'status': status, 'terminal': terminal, 'current_node': None if terminal else current,
            'approval_pending': pending}

def _project_rows(kent: Path, cwd: Path) -> list[dict[str, str]]:
    code, out, err = _run([str(kent), 'project', 'list'], cwd=cwd)
    if code:
        raise OperationError(err.decode(errors='replace') or 'Kent project list failed')
    rows: list[dict[str, str]] = []
    for line in out.decode('utf-8').splitlines():
        parts = line.split('\t')
        if len(parts) != 3 or not all(parts):
            raise OperationError('Kent project list output is malformed')
        rows.append({'id': parts[0], 'name': parts[1], 'path': parts[2]})
    return sorted(rows, key=lambda row: row['id'])

def _workflow_link_state(rows: Sequence[Mapping[str, Any]], project_id: str, workflow_id: str) -> tuple[list[dict[str,
        Any]], str | None, int | None]:
    matches = [row for row in rows if row.get('id', row.get('workflow_id')) == workflow_id]
    if len(matches) > 1:
        raise EffectBlocked('Workflow list contains a duplicate identity')
    if not matches:
        return ([], None, None)
    row = matches[0]
    link = row.get('project_link')
    if not isinstance(link, dict) or not isinstance(link.get('default'), bool):
        raise EffectBlocked('Workflow project link readback is malformed')
    version = row.get('version')
    if not isinstance(version, int) or isinstance(version, bool):
        raise EffectBlocked('Workflow list revision is malformed')
    links = [{'project_id': project_id, 'workflow_id': workflow_id, 'is_default': link['default']}]
    return (links, workflow_id if link['default'] else None, version)

def _live_execution_target(policy: Any, label: str) -> str:
    if not isinstance(policy, dict):
        raise EffectBlocked(f'{label} execution-target policy is malformed')
    mode = policy.get('mode')
    if not isinstance(mode, str):
        raise EffectBlocked(f'{label} execution-target policy is malformed')
    if mode in {'ask_on_first_execution', 'none', 'head', 'default_branch'}:
        if set(policy) != {'mode'}:
            raise EffectBlocked(f'{label} execution-target policy is malformed')
    elif mode == 'custom_ref':
        if set(policy) != {'mode', 'custom_ref'}:
            raise EffectBlocked(f'{label} execution-target policy is malformed')
        if not isinstance(policy.get('custom_ref'), str) or not policy['custom_ref'] or any(
            character.isspace() for character in policy['custom_ref']
        ):
            raise EffectBlocked(f'{label} execution-target policy is malformed')
    else:
        raise EffectBlocked(f'{label} execution-target policy is unsupported')
    try:
        target = execution_target_from_policy(policy)
        validate_execution_target(target)
    except (SpecError, TypeError, ValueError) as error:
        raise EffectBlocked(f'{label} execution-target policy is malformed') from error
    return target


def _summary_source(value: Mapping[str, Any], workflow_id: str) -> dict[str, Any]:
    expected_fields = {'id', 'version', 'name', 'description', 'execution_target_policy'}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise EffectBlocked('Workflow summary is malformed')
    source = value
    if source['id'] != workflow_id:
        raise EffectBlocked('Workflow inspect identity is malformed')
    if not isinstance(source['version'], int) or isinstance(source['version'], bool) or source['version'] < 0:
        raise EffectBlocked('Workflow inspect revision is malformed')
    try:
        _string(source['name'], 'Workflow name')
        _string(source['description'], 'Workflow description', nonempty=False)
    except PlanValidationError as error:
        raise EffectBlocked('Workflow summary metadata is malformed') from error
    _live_execution_target(source['execution_target_policy'], 'Workflow')
    return source


def _workflow_summary(value: Mapping[str, Any], workflow_id: str) -> dict[str, Any]:
    source = _summary_source(value, workflow_id)
    return {'present': True, 'workflow_id': workflow_id, 'revision': source['version']}


DELETE_PREVIEW_DIAGNOSTIC = 'Workflow deletion was not confirmed. Rerun with --confirm to delete it.\n'
DELETE_IMPACT_FIELDS = {
    'workflow_id', 'version', 'project_count', 'link_count', 'task_count',
    'current_node_count', 'pending_approval_count', 'blocked_task_count',
    'default_replacement_project_count',
}


def _kent_delete_preview(
    kent: Path,
    workflow_id: str,
    *,
    cwd: Path,
) -> dict[str, Any]:
    code, out, err = _run(
        [str(kent), 'workflow', 'delete', workflow_id, '--json'],
        cwd=cwd,
    )
    try:
        value = json.loads(out.decode('utf-8'), object_pairs_hook=_duplicate_free)
    except (PlanValidationError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EffectBlocked('Workflow delete preview is malformed') from error
    if not isinstance(value, dict):
        raise EffectBlocked('Workflow delete preview is malformed')
    if set(value) not in ({'deleted', 'impact'}, {'deleted', 'impact', 'blockers'}):
        raise EffectBlocked('Workflow delete preview is malformed')
    if value['deleted'] is not False:
        raise EffectBlocked('Workflow delete preview deleted flag is invalid')
    impact = value['impact']
    if not isinstance(impact, dict) or set(impact) != DELETE_IMPACT_FIELDS:
        raise EffectBlocked('Workflow delete preview impact is malformed')
    if impact['workflow_id'] != workflow_id:
        raise EffectBlocked('Workflow delete preview identity drifted')
    for key, count in impact.items():
        if key == 'workflow_id':
            continue
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise EffectBlocked('Workflow delete preview impact is malformed')
    if value.get('blockers', []) != []:
        raise EffectBlocked('Workflow delete preview has blockers')
    if code == 1:
        if err != DELETE_PREVIEW_DIAGNOSTIC.encode('utf-8'):
            raise EffectBlocked('Workflow delete preview diagnostic drifted')
    elif code == 0:
        if err:
            raise EffectBlocked('Workflow delete preview unexpectedly wrote stderr')
    else:
        raise EffectBlocked('Workflow delete preview exit code is invalid')
    return value

def _worktree_state(raw: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any] | None:
    topology = raw.get('topology')
    if isinstance(topology, dict) and topology.get('variant') == 'registered':
        registered = topology.get('registered')
        git = registered.get('git') if isinstance(registered, dict) else None
        kent = registered.get('kent') if isinstance(registered, dict) else None
        if not isinstance(git, dict) or not isinstance(kent, dict):
            raise EffectBlocked('Kent worktree registration is malformed')
        path = git.get('canonical_root')
        branch = git.get('branch_name')
        head = git.get('head_object')
        owner = kent.get('origin_session_id') or None
        registered_flag = True
    else:
        path = raw.get('path')
        branch = raw.get('branch')
        head = raw.get('head')
        owner = raw.get('owner_session')
        registered_flag = raw.get('registered') is True
    if path != expected['path']:
        return None
    root = Path(path)
    if not root.is_dir() or root.is_symlink():
        raise EffectBlocked('declared managed worktree is absent or unsafe')
    return {'path': path, 'branch': branch, 'head': head, 'dirty': bool(_git(root, 'status', '--porcelain=v1',
            '--untracked-files=all')), 'owner_session': owner, 'registered': registered_flag, 'retained': True}

def _resource_state(resource: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(resource['path'])
    if path.is_symlink():
        raise EffectBlocked('D9 resource path is a symlink')
    if not path.exists():
        return {**resource, 'present': False, 'sha256': None}
    if resource['kind'] == 'file':
        if not path.is_file():
            raise EffectBlocked('D9 retained file changed kind')
        digest = sha256_bytes(path.read_bytes())
    elif resource['kind'] == 'directory':
        digest = canonical_sha256(_retained_resource_manifest(path))
    else:
        raise EffectBlocked('D9 resource kind is unsupported')
    return {**resource, 'present': True, 'sha256': digest}

def _session_cli_state(row: Mapping[str, Any], task_id: str, live_ids: set[str]) -> dict[str, Any]:
    session_id = _row_id(row, 'id', 'session_id')
    owner = row.get('live_owner', row.get('owner_session_id'))
    if session_id in live_ids and owner is None:
        owner = session_id
    return {'id': session_id, 'status': _status_name(row.get('status')), 'task_id': row.get('task_id', task_id),
            'retained': row.get('retained', True), 'live_owner': owner}

def _d9_read_inventory(parsed: Mapping[str, Any], reference: Mapping[str, Any] | None=None) -> dict[str, Any]:
    del reference
    kent = parsed['kent']
    _verify_executable(kent, parsed['kent_sha256'])
    project_id = parsed['project_id']
    cwd = parsed['project_root']
    projects = _project_rows(kent, cwd)
    project_matches = [row for row in projects if row['id'] == project_id]
    if len(project_matches) != 1:
        raise EffectBlocked('D9 project identity is absent or duplicated')
    if Path(project_matches[0]['path']).resolve() != cwd.resolve():
        raise EffectBlocked('D9 project root drifted')
    workflow_pages = _kent_pages(kent, ['workflow', 'list', '--project', project_id], cwd=cwd)
    workflow_rows = _page_rows(workflow_pages, ('items', 'workflows'))
    worktree_value = _kent_json(kent, ['worktree', 'list', '--json'], cwd=cwd)
    raw_worktrees = worktree_value.get('worktrees', worktree_value.get('items', []))
    if not isinstance(raw_worktrees, list):
        raise EffectBlocked('Kent worktree list is malformed')
    inventory: dict[str, Any] = {'project': project_matches[0], 'database_schema': parsed['schema'], 'members': {}}
    for member in parsed['members']:
        wid = member['workflow_id']
        links, default, listed_version = _workflow_link_state(workflow_rows, project_id, wid)
        inspect = _kent_optional(kent, ['workflow', 'inspect', wid, '--summary', '--json'], cwd=cwd)
        present = bool(links)
        if present != (inspect.get('present') is not False):
            raise EffectBlocked('Workflow list and inspect disagree on presence')
        task_pages = _kent_pages(kent, ['task', 'list', '--project', project_id, '--workflow', wid], cwd=cwd)
        task_rows = _page_rows(task_pages, ('items', 'tasks'))
        list_tasks = sorted((_task_state(row) for row in task_rows), key=lambda row: row['id'])
        details: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        graph_sha = validation_sha = preview_sha = None
        if present:
            workflow = _workflow_summary(inspect, wid)
            if workflow['revision'] != listed_version:
                raise EffectBlocked('Workflow list and inspect revisions disagree')
            graph = _kent_json(kent, ['workflow', 'graph', 'inspect', wid, '--json'], cwd=cwd)
            validation = _kent_json(kent, ['workflow', 'validate', wid, '--json'], cwd=cwd)
            graph_sha = canonical_sha256(graph)
            validation_sha = canonical_sha256(validation)
            if validation.get('valid') is not True:
                raise EffectBlocked('Workflow validation failed')
            preview = _kent_delete_preview(kent, wid, cwd=cwd)
            preview_sha = canonical_sha256(preview)
            for task in list_tasks:
                detail = _kent_json(kent, ['task', 'show', task['id'], '--project', project_id, '--json'], cwd=cwd)
                details.append(_task_state(detail, task['id']))
                live_rows = detail.get('live_sessions', [])
                if not isinstance(live_rows, list):
                    raise EffectBlocked('Task live Session inventory is malformed')
                live_ids = {row if isinstance(row, str) else row.get('session_id', row.get('id')) for row in live_rows
                        if isinstance(row, (str, dict))}
                pages = _kent_pages(kent, ['task', 'sessions', task['id'], '--project', project_id], cwd=cwd)
                task_sessions = [_session_cli_state(row, task['id'], live_ids) for row in _page_rows(pages, ('items',
                        'sessions'))]
                count = detail.get('retained_session_count')
                if count is not None and count != len(task_sessions):
                    raise EffectBlocked('Task retained Session pagination is incomplete')
                sessions.extend(task_sessions)
            impact = preview['impact']
            expected_impact = {
                'workflow_id': wid,
                'version': listed_version,
                'project_count': 1,
                'link_count': len(links),
                'task_count': len(list_tasks),
                'current_node_count': sum(
                    task['current_node'] is not None for task in details
                ),
                'pending_approval_count': sum(
                    task['approval_pending'] for task in details
                ),
                'blocked_task_count': sum(
                    task['current_node'] is not None or task['approval_pending']
                    for task in details
                ),
                'default_replacement_project_count': 0,
            }
            if impact != expected_impact:
                raise EffectBlocked('Workflow delete preview impact drifted')
        elif task_rows:
            raise EffectBlocked('absent Workflow still has Tasks')
        details.sort(key=lambda row: row['id'])
        sessions.sort(key=lambda row: row['id'])
        if len(sessions) != len({session['id'] for session in sessions}):
            raise EffectBlocked('retained Session inventory contains duplicates')
        worktrees: list[dict[str, Any]] = []
        for expected in member['worktrees']:
            matches = [state for raw in raw_worktrees if isinstance(raw, dict) for state in [_worktree_state(raw,
                    expected)] if state is not None]
            if len(matches) != 1:
                raise EffectBlocked('managed worktree registration drifted')
            worktrees.append(matches[0])
        session_ids = [session['id'] for session in member['sessions']]
        task_ids = [task['id'] for task in member['tasks']]
        inventory['members'][wid] = {'workflow': workflow if present else {'present': False, 'workflow_id': wid,
                'revision': None}, 'links': links, 'default': default, 'graph_sha256': graph_sha, 'validation_sha256':
                validation_sha, 'tasks': list_tasks, 'task_details': details, 'sessions': sessions, 'session_manifests':
                [{'id': session['id'], 'manifest': _session_manifest(_session_path(session, [Path(session['root'])]))}
                for session in member['sessions']], 'sqlite': _sqlite_snapshot(parsed['database'], parsed['schema'],
                session_ids, task_ids), 'worktrees': sorted(worktrees, key=lambda row: row['path']), 'retained':
                [_resource_state(row) for row in member['retained']], 'absent': [_resource_state(row) for row in
                member['absent']], 'preview_sha256': preview_sha}
    return inventory

def _d9_member_live_gate(member: Mapping[str, Any], live: Mapping[str, Any], *, require_preview: bool=True) -> None:
    wid = member['workflow_id']
    workflow = live.get('workflow')
    if not isinstance(workflow, dict) or workflow.get('present') is False:
        raise EffectBlocked(f'D9 Workflow {wid} is absent before deletion')
    if workflow != {'present': True, 'workflow_id': wid, 'revision': member['revision']}:
        raise EffectBlocked(f'D9 Workflow revision drifted for {wid}')
    for key in ('links', 'default'):
        if canonical_sha256(live.get(key)) != canonical_sha256(member[key]):
            raise EffectBlocked(f'D9 {key} drifted for {wid}')
    for key in ('tasks', 'task_details'):
        if canonical_sha256(live.get(key)) != canonical_sha256(member['tasks']):
            raise EffectBlocked(f'D9 Task list/detail drifted for {wid}')
    expected_sessions = [{key: session[key] for key in ('id', 'status', 'task_id', 'retained', 'live_owner')} for
            session in member['sessions']]
    if canonical_sha256(live.get('sessions')) != canonical_sha256(expected_sessions):
        raise EffectBlocked(f'D9 Session inventory drifted for {wid}')
    if any((session['live_owner'] is not None for session in live.get('sessions', []))):
        raise EffectBlocked(f'D9 has a live Session owner for {wid}')
    expected_manifests = [{'id': session['id'], 'manifest': session['manifest']} for session in member['sessions']]
    if canonical_sha256(live.get('session_manifests')) != canonical_sha256(expected_manifests):
        raise EffectBlocked('retained Session manifest drifted')
    sqlite_rows = live.get('sqlite', {}).get('sessions', [])
    sqlite_by_id = {row.get('session_id'): row for row in sqlite_rows}
    for session in member['sessions']:
        row = sqlite_by_id.get(session['id'])
        if not isinstance(row, dict) or row.get('present') is not True:
            raise EffectBlocked('retained Session is absent from SQLite')
        if (row.get('row') or {}).get('task_id') != session['task_id']:
            raise EffectBlocked('retained Session Task association drifted')
    if canonical_sha256(live.get('worktrees')) != canonical_sha256(member['worktrees']):
        raise EffectBlocked('managed worktree state drifted')
    expected_retained = [{**row, 'present': True} for row in member['retained']]
    expected_absent = [{**row, 'present': False} for row in member['absent']]
    if canonical_sha256(live.get('retained')) != canonical_sha256(expected_retained):
        raise EffectBlocked('retained resource drifted')
    if canonical_sha256(live.get('absent')) != canonical_sha256(expected_absent):
        raise EffectBlocked('absent resource appeared')
    if require_preview and live.get('preview_sha256') != member['delete_preview']['sha256']:
        raise EffectBlocked(f'D9 delete preview drifted for {wid}')

def _d9_inventory_digest(inventory: Mapping[str, Any]) -> str:
    return canonical_sha256(inventory)

def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {'bytes_hex': value.hex()}
    return value
SQLITE_SCHEMA_FINGERPRINTS = {'sessions': 'f0e7fa1866f934af36dfc8cafb5284aaef521728c73745a68bc455fec20bd196',
        'session_workflow_node_associations': 'a63ebbc41c4c41cb9dcf0c700ba25d0fc2cc7a5e566d992738f4eea4a273b3b8'}

def _sqlite_schema_check(connection: sqlite3.Connection, expected_schema: str) -> tuple[tuple[str, ...], tuple[str,
        ...], dict[str, str]]:
    if expected_schema != KENT_SCHEMA_IDENTITY:
        raise OperationError('unsupported Kent persistence schema')
    columns: dict[str, tuple[str, ...]] = {}
    fingerprints: dict[str, str] = {}
    for table, expected in SQLITE_SCHEMA_FINGERPRINTS.items():
        info = [list(row) for row in connection.execute(f'PRAGMA table_info({table})')]
        foreign_keys = sorted([list(row) for row in connection.execute(f'PRAGMA foreign_key_list({table})')])
        if not info:
            raise OperationError('Kent persistence schema fingerprint is incomplete')
        fingerprint = canonical_sha256({'columns': info, 'foreign_keys': foreign_keys})
        if fingerprint != expected:
            raise OperationError(f'{table} schema fingerprint is unsupported')
        columns[table] = tuple((str(row[1]) for row in info))
        fingerprints[table] = fingerprint
    return (columns['sessions'], columns['session_workflow_node_associations'], fingerprints)

def _sqlite_snapshot(database: Path, expected_schema: str, session_ids: Sequence[str], task_ids: Sequence[str]) -> \
        dict[str, Any]:
    if not database.is_file() or database.is_symlink():
        raise OperationError('Kent persistence database is absent or unsafe')
    _unique(session_ids, 'SQLite Session identities')
    _unique(task_ids, 'SQLite Task identities')
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)
        connection.execute('PRAGMA query_only=ON')
        session_columns, association_columns, fingerprints = _sqlite_schema_check(connection, expected_schema)
        sessions: list[dict[str, Any]] = []
        for session_id in sorted(session_ids):
            rows = connection.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchall()
            if len(rows) > 1:
                raise OperationError('Session identity matched multiple rows')
            sessions.append({'session_id': session_id, 'present': bool(rows), 'row': {key: _sqlite_value(value) for key,
                    value in zip(session_columns, rows[0])} if rows else None})
        associations: list[dict[str, Any]] = []
        for column, values in (('session_id', session_ids), ('task_id', task_ids)):
            for value in sorted(values):
                rows = connection.execute(f'SELECT * FROM session_workflow_node_associations WHERE {column} = ?',
                        (value,)).fetchall()
                for row in rows:
                    item = {key: _sqlite_value(cell) for key, cell in zip(association_columns, row)}
                    if item not in associations:
                        associations.append(item)
        associations.sort(key=canonical_bytes)
        return {'schema_fingerprints': fingerprints, 'sessions': sessions, 'associations': associations}
    except sqlite3.Error as error:
        raise OperationError(f'read-only Session query failed: {error}') from error
    finally:
        if connection is not None:
            connection.close()

def _kent_delete_command(kent: Path, workflow_id: str, confirm: bool) -> list[str]:
    command = [str(kent), 'workflow', 'delete', workflow_id]
    if confirm:
        command.append('--confirm')
    command.append('--json')
    return command

def _d9_expected_post(member: Mapping[str, Any], before: Mapping[str, Any]) -> dict[str, Any]:
    sqlite_before = before['sqlite']
    sessions = []
    for value in sqlite_before['sessions']:
        row = dict(value['row'])
        row['task_id'] = None
        sessions.append({**value, 'row': row})
    task_ids = {task['id'] for task in member['tasks']}
    associations = [row for row in sqlite_before['associations'] if row.get('task_id') not in task_ids]
    return {'workflow': {'present': False, 'workflow_id': member['workflow_id'], 'revision': None}, 'links': [],
            'default': None, 'graph_sha256': None, 'validation_sha256': None, 'tasks': [], 'task_details': [],
            'sessions': [], 'session_manifests': before['session_manifests'], 'sqlite': {'schema_fingerprints':
            sqlite_before['schema_fingerprints'], 'sessions': sessions, 'associations': associations}, 'worktrees':
            before['worktrees'], 'retained': before['retained'], 'absent': before['absent'], 'preview_sha256': None}

def _effect_attempts(state: Mapping[str, Any]) -> int:
    return sum((int(effect.get('attempt', 0)) for effect in (state.get('effects') or {}).values() if isinstance(effect,
            dict)))

def _d9_validate_batch_inventory(parsed: Mapping[str, Any], inventory: Mapping[str, Any], statuses: Mapping[str, str],
        prepared: Mapping[str, Any], settling: str | None=None) -> None:
    if inventory.get('database_schema') != parsed['schema']:
        raise EffectBlocked('D9 database schema identity drifted')
    for key in ('project', 'database_schema'):
        if canonical_sha256(inventory.get(key)) != canonical_sha256(prepared.get(key)):
            raise EffectBlocked(f'D9 global preimage drifted: {key}')
    live_members = inventory.get('members')
    old_members = prepared.get('members')
    if not isinstance(live_members, dict) or not isinstance(old_members, dict):
        raise EffectBlocked('D9 member inventory is malformed')
    for member in parsed['members']:
        wid = member['workflow_id']
        live = live_members.get(wid)
        before = old_members.get(wid)
        if not isinstance(live, dict) or not isinstance(before, dict):
            raise EffectBlocked(f'D9 member inventory is missing for {wid}')
        status = statuses.get(wid, 'pending')
        if status == 'pending' and settling == wid:
            post = _d9_expected_post(member, before)
            if canonical_sha256(live) == canonical_sha256(before):
                _d9_member_live_gate(member, live)
                expected = before
            elif canonical_sha256(live) == canonical_sha256(post):
                expected = post
            else:
                raise EffectBlocked(f'D9 settling snapshot is ambiguous for {wid}')
        elif status == 'pending':
            _d9_member_live_gate(member, live)
            expected = before
        elif status == 'verified':
            expected = _d9_expected_post(member, before)
        else:
            raise JournalError(f'D9 member status is invalid: {status!r}')
        if canonical_sha256(live) != canonical_sha256(expected):
            raise EffectBlocked(f'D9 complete member snapshot drifted for {wid}')

def retire_workflow_batch(plan: LoadedPlan, *, mode: str, kent: str | Path | None=None) -> dict[str, Any]:
    parsed = _validate_d9_plan(plan)
    if mode not in {'preview', 'apply', 'resume'}:
        raise PlanValidationError('retirement mode must be preview, apply, or resume')
    if kent is not None and str(kent) != str(parsed['kent']):
        raise PlanValidationError('runtime --kent differs from the plan-bound executable')
    workflow_ids = [member['workflow_id'] for member in parsed['members']]
    if mode == 'preview':
        inventory = _d9_read_inventory(parsed)
        _d9_validate_batch_inventory(parsed, inventory, {}, inventory)
        return {'schema': 'workflow-retirement-batch-report-v2', 'plan_sha256': plan.sha256, 'phase': 'preview',
                'inventory_sha256': _d9_inventory_digest(inventory), 'effects_released': 0}
    with OperationJournal(parsed['state_dir'], 'workflow-retirement-batch', plan) as journal:
        if journal.state is None:
            inventory = _d9_read_inventory(parsed)
            _d9_validate_batch_inventory(parsed, inventory, {}, inventory)
            journal.persist({'phase': 'prepared', 'inventory': inventory, 'inventory_sha256':
                    _d9_inventory_digest(inventory), 'members': [{'workflow_id': workflow_id, 'status': 'pending'} for
                    workflow_id in workflow_ids], 'effects': {}})
        state = journal.require_phase({'prepared', 'in_progress', 'complete'})
        statuses = {row['workflow_id']: row['status'] for row in state.get('members', [])}
        if set(statuses) != set(workflow_ids):
            raise JournalError('D9 journal member inventory drifted')
        if state['phase'] == 'complete':
            final = _d9_read_inventory(parsed)
            _d9_validate_batch_inventory(parsed, final, statuses, state['inventory'])
            return {'schema': 'workflow-retirement-batch-report-v2', 'plan_sha256': plan.sha256, 'phase': 'complete',
                    'resumed': True, 'members_verified': len(workflow_ids)}
        for member in parsed['members']:
            wid = member['workflow_id']
            if statuses[wid] == 'verified':
                continue
            key = f'delete:{wid}'
            existing = (journal.state.get('effects') or {}).get(key)
            resumable = isinstance(existing, dict) and existing.get('status') in {'attempted', 'unresolved',
                    'ambiguous', 'verified'}
            inventory = _d9_read_inventory(parsed)
            _d9_validate_batch_inventory(parsed, inventory, statuses, state['inventory'], settling=wid if resumable else
                    None)
            before = state['inventory']['members'][wid]
            expected = _d9_expected_post(member, before)
            preimage = canonical_sha256(before)
            postimage = canonical_sha256(expected)
            command = _kent_delete_command(parsed['kent'], wid, True)

            def current() -> str:
                return canonical_sha256(_d9_read_inventory(parsed)['members'][wid])
            journal.persist({**journal.state, 'phase': 'in_progress'})
            settled = _settle_or_run(journal, effect_key=key, command=command, cwd=parsed['project_root'],
                    preimage_sha256=preimage, postimage_sha256=postimage, current_sha256=current)
            if settled == 'preimage':
                return {'schema': 'workflow-retirement-batch-report-v2', 'plan_sha256': plan.sha256, 'phase':
                        'in_progress', 'settled': 'preimage', 'effects_released': _effect_attempts(journal.state)}
            if current() != postimage:
                raise EffectBlocked('D9 exact member postimage drifted')
            statuses[wid] = 'verified'
            journal.persist({**journal.state, 'members': [{'workflow_id': workflow_id, 'status': statuses[workflow_id]}
                    for workflow_id in workflow_ids]})
        final = _d9_read_inventory(parsed)
        _d9_validate_batch_inventory(parsed, final, statuses, state['inventory'])
        journal.persist({**journal.state, 'phase': 'complete'})
        return {'schema': 'workflow-retirement-batch-report-v2', 'plan_sha256': plan.sha256, 'phase': 'complete',
                'members_verified': len(workflow_ids), 'effects_released': _effect_attempts(journal.state)}

def _plan_execution_target(value: Any, label: str) -> str:
    target = _string(value, label)
    try:
        validate_execution_target(target)
    except SpecError as error:
        raise PlanValidationError(f'{label} is unsupported') from error
    return target


def _validate_canonical_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(plan.value, {'schema', 'state_dir', 'project_root', 'kent', 'd9', 'workflows'}, 'canonical plan')
    state_dir = _absolute_path(_required(data, 'state_dir', 'canonical plan'), 'state_dir')
    project_root = _absolute_path(_required(data, 'project_root', 'canonical plan'), 'project_root')
    kent = _closed(_required(data, 'kent', 'canonical plan'), {'path', 'sha256'}, 'kent')
    kent_path = _kent_path(_required(kent, 'path', 'kent'), 'kent.path')
    kent_sha = _sha256(_required(kent, 'sha256', 'kent'), 'kent.sha256')
    _verify_executable(kent_path, kent_sha)
    raw_d9 = _closed(_required(data, 'd9', 'canonical plan'), {'none', 'path', 'sha256', 'operation', 'phase',
            'members'}, 'd9')
    if raw_d9.get('none') is True:
        if set(raw_d9) != {'none'}:
            raise PlanValidationError('canonical d9 none marker is not closed')
        d9 = None
    else:
        if raw_d9.get('none') is not False:
            raise PlanValidationError('canonical d9 dependency must declare none=false')
        d9 = {'path': _absolute_path(_required(raw_d9, 'path', 'd9'), 'd9.path'), 'sha256': _sha256(_required(raw_d9,
                'sha256', 'd9'), 'd9.sha256'), 'operation': _string(_required(raw_d9, 'operation', 'd9'),
                'd9.operation'), 'phase': _string(_required(raw_d9, 'phase', 'd9'), 'd9.phase'), 'members':
                sorted((_uuid(member, 'd9.member') for member in _bounded_list(_required(raw_d9, 'members', 'd9'),
                'd9.members')))}
        _unique(d9['members'], 'd9 members')
        if d9['operation'] != 'workflow-retirement-batch' or d9['phase'] != 'complete':
            raise PlanValidationError('canonical d9 dependency is unsupported')
    workflows: list[dict[str, Any]] = []
    raw_workflows = _bounded_list(_required(data, 'workflows', 'canonical plan'), 'workflows')
    for index, raw in enumerate(raw_workflows):
        label = f'workflows[{index}]'
        _reject_raw_protocol_fields(raw, label)
        item = dict(_closed(raw, {'workflow_id', 'project_id', 'intent', 'expected_version', 'graph', 'metadata',
                'terminal_tasks', 'terminal_anchors', 'links', 'default'}, label))
        item['workflow_id'] = _uuid(_required(item, 'workflow_id', label), f'{label}.workflow_id')
        item['project_id'] = _native_project_id(_required(item, 'project_id', label), f'{label}.project_id')
        intent = _string(_required(item, 'intent', label), f'{label}.intent')
        if intent not in {'graph-only', 'metadata-only', 'graph-and-metadata'}:
            raise PlanValidationError('canonical intent is unsupported')
        item['intent'] = intent
        version = _required(item, 'expected_version', label)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise PlanValidationError('canonical expected_version is invalid')
        tasks: list[dict[str, str]] = []
        for raw_task in _bounded_list(_required(item, 'terminal_tasks', label), f'{label}.terminal_tasks'):
            task = _closed(raw_task, {'id', 'status'}, 'terminal task')
            tasks.append({'id': _string(_required(task, 'id', 'terminal task'), 'task.id'), 'status':
                    _string(_required(task, 'status', 'terminal task'), 'task.status')})
        tasks.sort(key=lambda task: task['id'])
        _unique([task['id'] for task in tasks], 'canonical terminal Tasks')
        item['terminal_tasks'] = tasks
        anchors: list[dict[str, str]] = []
        for raw_anchor in _bounded_list(_required(item, 'terminal_anchors', label), f'{label}.terminal_anchors'):
            anchor = _closed(raw_anchor, {'id', 'kind'}, 'terminal anchor')
            anchors.append({'id': _string(_required(anchor, 'id', 'terminal anchor'), 'anchor.id'), 'kind':
                    _string(_required(anchor, 'kind', 'terminal anchor'), 'anchor.kind')})
        anchors.sort(key=lambda anchor: anchor['id'])
        _unique([anchor['id'] for anchor in anchors], 'canonical terminal anchors')
        item['terminal_anchors'] = anchors
        links: list[dict[str, Any]] = []
        for raw_link in _bounded_list(_required(item, 'links', label), f'{label}.links'):
            link = _closed(raw_link, {'project_id', 'workflow_id', 'is_default'}, 'link')
            parsed_link = {'project_id': _native_project_id(_required(link, 'project_id', 'link'), 'link.project_id'), 'workflow_id':
                    _uuid(_required(link, 'workflow_id', 'link'), 'link.workflow_id'), 'is_default': _required(link,
                    'is_default', 'link')}
            if parsed_link['project_id'] != item['project_id']:
                raise PlanValidationError('canonical link project drifted')
            if parsed_link['workflow_id'] != item['workflow_id']:
                raise PlanValidationError('canonical link Workflow drifted')
            if not isinstance(parsed_link['is_default'], bool):
                raise PlanValidationError('canonical link default flag is invalid')
            links.append(parsed_link)
        links.sort(key=lambda link: (link['project_id'], link['workflow_id']))
        item['links'] = links
        default_raw = _required(item, 'default', label)
        item['default'] = _uuid(default_raw, f'{label}.default') if default_raw is not None else None
        expected_default = item['workflow_id'] if any((link['is_default'] for link in links)) else None
        if item['default'] != expected_default:
            raise PlanValidationError('canonical default does not match the project link')
        if intent in {'graph-only', 'graph-and-metadata'}:
            graph = dict(_closed(_required(item, 'graph', label), {'version', 'nodes', 'edges', 'node_groups',
                    'transition_groups'}, f'{label}.graph'))
            if graph.get('version') != version + 1:
                raise PlanValidationError('target graph must advance exactly one version')
            for key in ('nodes', 'edges', 'node_groups', 'transition_groups'):
                _bounded_list(_required(graph, key, f'{label}.graph'), f'graph.{key}')
            item['graph'] = graph
            node_kinds = {node.get('id', node.get('node_id')): node.get('kind') for node in graph['nodes'] if
                    isinstance(node, dict)}
            if any((node_kinds.get(anchor['id']) != anchor['kind'] for anchor in anchors)):
                raise PlanValidationError('target graph does not preserve terminal anchors')
        elif 'graph' in item:
            raise PlanValidationError('metadata-only workflow cannot carry graph')
        if intent in {'metadata-only', 'graph-and-metadata'}:
            metadata = _closed(_required(item, 'metadata', label), {'name', 'description',
                    'execution_target'}, f'{label}.metadata')
            item['metadata'] = {'name': _string(_required(metadata, 'name', 'metadata'), 'metadata.name'),
                    'description': _string(_required(metadata, 'description', 'metadata'), 'metadata.description',
                    nonempty=False), 'execution_target': _string(_required(metadata, 'execution_target', 'metadata'),
                    'metadata.execution_target')}
            item['metadata']['execution_target'] = _plan_execution_target(
                item['metadata']['execution_target'],
                'metadata.execution_target',
            )
        elif 'metadata' in item:
            raise PlanValidationError('graph-only workflow cannot carry metadata')
        workflows.append(item)
    if not workflows:
        raise PlanValidationError('canonical plan has no workflows')
    workflows.sort(key=lambda item: item['workflow_id'])
    _unique([item['workflow_id'] for item in workflows], 'canonical Workflows')
    if d9 and (not {item['workflow_id'] for item in workflows}.issubset(d9['members'])):
        raise PlanValidationError('canonical Workflows are not covered by D9')
    return {'state_dir': state_dir, 'project_root': project_root, 'kent': kent_path, 'kent_sha256': kent_sha, 'd9': d9,
            'workflows': workflows}


CANONICAL_V2_SCHEMA = 'canonical-workflow-reconcile-plan-v2'
CANONICAL_V2_SNAPSHOT_SCHEMA = 'canonical-workflow-compatibility-snapshot-v1'
CANONICAL_V2_REVIEW_SCHEMA = 'canonical-workflow-compatibility-review-v1'
CANONICAL_V2_MAX_PLAN = 1024 * 1024
CANONICAL_V2_MAX_REFERENCE = 1024 * 1024
NATIVE_EVIDENCE_SCHEMA = 'kent-native-evidence-v1'
NATIVE_COMPONENTS_SCHEMA = 'kent-native-components-v1'
NATIVE_COMPONENTS_V2_SCHEMA = 'kent-native-components-v2'
NATIVE_MAX_TASKS = 4096
NATIVE_MAX_TERMINAL_IDENTITIES = 4096
NATIVE_MAX_EVENT_FILE_BYTES = 256 * 1024 * 1024
NATIVE_MAX_EVENT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
NATIVE_MAX_EVENT_LINE_BYTES = 8 * 1024 * 1024
NATIVE_MAX_SELECTED_EVENTS_PER_SESSION = 256
NATIVE_MAX_SELECTED_EVENT_BYTES = 32 * 1024 * 1024
NATIVE_MAX_DB_JSON_BYTES = 8 * 1024 * 1024
NATIVE_MAX_DB_COMPONENT_BYTES = 32 * 1024 * 1024
SOURCE_ADMISSION_SCHEMA = 'delivery-source-admission-v1'
SOURCE_RECEIPT_SCHEMA = 'delivery-runtime-publication-v1'
SOURCE_BINDING_SCHEMA = 'r2-installation-binding-v2'
SOURCE_INSPECTION_SCHEMA = 'delivery-source-inspection-v1'
SOURCE_RECEIPT_FILE_LIMIT = 512
SOURCE_RECEIPT_BYTES_LIMIT = 1024 * 1024
SOURCE_FILE_BYTES_LIMIT = 8 * 1024 * 1024
SOURCE_TOTAL_BYTES_LIMIT = 32 * 1024 * 1024
SOURCE_INSPECTOR_TIMEOUT = 120

_VERIFIED_SOURCE_ADMISSION_TOKEN = object()


@dataclass(frozen=True, init=False)
class VerifiedSourceAdmission:
    """Bound result of native, installation and published-source admission.

    The mapping properties decode fresh values from private canonical bytes.
    Mutating a returned dictionary cannot change the gate, digests or identity.
    Instances are created only by :func:`admit_source_admission`.
    """

    project_id: str
    workflow_id: str
    project_root: str
    primary_workspace_id: str
    primary_root: str
    kit_root: str
    source_admission_sha256: str
    receipt_sha256: str
    gate: str
    _value_raw: bytes
    _inspection_raw: bytes
    _receipt_files_raw: bytes

    def __init__(
        self,
        *,
        project_id: str,
        workflow_id: str,
        project_root: str,
        primary_workspace_id: str,
        primary_root: str,
        kit_root: str,
        source_admission_sha256: str,
        receipt_sha256: str,
        gate: str,
        value_raw: bytes,
        inspection_raw: bytes,
        receipt_files_raw: bytes,
        _token: object,
    ) -> None:
        if _token is not _VERIFIED_SOURCE_ADMISSION_TOKEN:
            raise TypeError("VerifiedSourceAdmission is created by source admission")
        for name, value in (
            ("project_id", project_id),
            ("workflow_id", workflow_id),
            ("project_root", project_root),
            ("primary_workspace_id", primary_workspace_id),
            ("primary_root", primary_root),
            ("kit_root", kit_root),
            ("source_admission_sha256", source_admission_sha256),
            ("receipt_sha256", receipt_sha256),
            ("gate", gate),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_value_raw", bytes(value_raw))
        object.__setattr__(self, "_inspection_raw", bytes(inspection_raw))
        object.__setattr__(self, "_receipt_files_raw", bytes(receipt_files_raw))

    @property
    def value(self) -> dict[str, Any]:
        return json.loads(self._value_raw.decode("utf-8"))

    @property
    def inspection(self) -> dict[str, Any]:
        return json.loads(self._inspection_raw.decode("utf-8"))

    @property
    def receipt_files(self) -> list[dict[str, Any]]:
        """Return a fresh copy of the verified publication file inventory."""
        return json.loads(self._receipt_files_raw.decode("utf-8"))


def _no_symlink_path(path: Path, label: str) -> Path:
    path = _absolute_path(str(path), label)
    if path.is_symlink():
        raise PlanValidationError(f'{label} is a symlink')
    try:
        physical = path.resolve(strict=True)
    except OSError as error:
        raise PlanValidationError(f'{label} is absent or inaccessible') from error
    current = Path('/')
    for part in physical.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise PlanValidationError(f'{label} is absent or inaccessible') from error
        if stat.S_ISLNK(info.st_mode):
            raise PlanValidationError(f'{label} contains a symlink')
    return path


def _read_reference(value: Any, label: str, *, limit: int = CANONICAL_V2_MAX_REFERENCE) -> tuple[Path, bytes]:
    ref = _closed(value, {'path', 'sha256'}, label)
    path = _no_symlink_path(
        _absolute_path(_required(ref, 'path', label), f'{label}.path'),
        f'{label}.path',
    )
    expected = _sha256(_required(ref, 'sha256', label), f'{label}.sha256')
    physical = path.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        descriptor = os.open(physical, flags)
    except OSError as error:
        raise PlanValidationError(f'{label} cannot be opened safely') from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise PlanValidationError(f'{label} is not a bounded regular file')
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(MANIFEST_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise PlanValidationError(f'{label} exceeds the size bound')
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _manifest_fingerprint(before) != _manifest_fingerprint(after):
            raise PlanValidationError(f'{label} changed while being read')
        raw = b''.join(chunks)
    except OSError as error:
        raise PlanValidationError(f'{label} could not be read safely') from error
    finally:
        os.close(descriptor)
    if digest.hexdigest() != expected:
        raise PlanValidationError(f'{label} digest drifted')
    return path, raw


def _source_canonical_path(value: Any, label: str) -> Path:
    raw = _string(value, label)
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts or str(path) != raw:
        raise PlanValidationError(f'{label} must be a canonical absolute path')
    return path


def _source_canonical_root(value: Any, label: str) -> Path:
    path = _source_canonical_path(value, label)
    try:
        physical = path.resolve(strict=True)
    except OSError as error:
        raise PlanValidationError(f'{label} is absent or inaccessible') from error
    if physical != path or not path.is_dir():
        raise PlanValidationError(f'{label} is not a canonical directory')
    current = Path('/')
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise PlanValidationError(f'{label} is absent or inaccessible') from error
        if stat.S_ISLNK(info.st_mode):
            raise PlanValidationError(f'{label} contains a symlink')
    return path


def _read_source_record(
    value: Any,
    *,
    label: str,
    maximum: int,
    private: bool,
) -> tuple[Path, bytes, int]:
    """Descriptor-relative no-follow read for a private admission record."""
    path = _source_canonical_path(str(value) if isinstance(value, Path) else value, label)
    descriptors: list[int] = []
    bindings: list[tuple[int, str, int]] = []
    try:
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_CLOEXEC', 0)
        root_fd = os.open('/', flags)
        descriptors.append(root_fd)
        for component in path.parent.parts[1:]:
            parent = descriptors[-1]
            child = os.open(
                component,
                flags | getattr(os, 'O_NOFOLLOW', 0),
                dir_fd=parent,
            )
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                raise PlanValidationError(f'{label} has an unsafe directory')
            descriptors.append(child)
            bindings.append((parent, component, child))
        parent_fd = descriptors[-1]
        parent_info = os.fstat(parent_fd)
        if private and (
            parent_info.st_uid != os.getuid()
            or parent_info.st_mode & 0o7077
        ):
            raise PlanValidationError(f'{label} parent directory is not private')
        file_flags = (
            os.O_RDONLY
            | getattr(os, 'O_NOFOLLOW', 0)
            | getattr(os, 'O_NONBLOCK', 0)
            | getattr(os, 'O_CLOEXEC', 0)
        )
        descriptor = os.open(path.name, file_flags, dir_fd=parent_fd)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        forbidden = 0o7077 if private else 0o7022
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum
            or before.st_mode & forbidden
            or (private and before.st_uid != os.getuid())
        ):
            raise PlanValidationError(f'{label} is not a safe bounded regular file')
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(MANIFEST_READ_CHUNK, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise PlanValidationError(f'{label} exceeds its byte limit')
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fingerprint = lambda info: (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if fingerprint(before) != fingerprint(after) or size != before.st_size:
            raise PlanValidationError(f'{label} changed while being read')
        linked = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if fingerprint(linked) != fingerprint(after):
            raise PlanValidationError(f'{label} file binding changed')
        for parent, component, child in bindings:
            linked = os.stat(component, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
                raise PlanValidationError(f'{label} directory binding changed')
        return path, b''.join(chunks), stat.S_IMODE(before.st_mode)
    except PlanValidationError:
        raise
    except OSError as error:
        raise PlanValidationError(f'{label} cannot be read safely') from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _source_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode('utf-8'),
            object_pairs_hook=_duplicate_free,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PlanValidationError(f'{label} contains a non-finite JSON number')
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise PlanValidationError(f'{label} is not valid JSON') from error
    if not isinstance(value, dict):
        raise PlanValidationError(f'{label} must be an object')
    return value


def _v2_source_admission(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _closed(value, {
        'schema', 'receipt_ref', 'installation_binding_sha256', 'primary_workspace_id',
        'primary_root', 'kit_root', 'source_ref', 'kit_ref', 'ci_helper', 'helper_bindings',
    }, label)
    if item.get('schema') != SOURCE_ADMISSION_SCHEMA:
        raise PlanValidationError(f'{label}.schema is unsupported')
    receipt_ref = _closed(
        _required(item, 'receipt_ref', label),
        {'path', 'sha256'},
        f'{label}.receipt_ref',
    )
    receipt_path = _source_canonical_path(
        _required(receipt_ref, 'path', f'{label}.receipt_ref'),
        f'{label}.receipt_ref.path',
    )
    receipt_sha256 = _sha256(
        _required(receipt_ref, 'sha256', f'{label}.receipt_ref'),
        f'{label}.receipt_ref.sha256',
    )
    binding_sha256 = _sha256(
        _required(item, 'installation_binding_sha256', label),
        f'{label}.installation_binding_sha256',
    )
    workspace_id = _string(
        _required(item, 'primary_workspace_id', label),
        f'{label}.primary_workspace_id',
    )
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,255}', workspace_id):
        raise PlanValidationError(f'{label}.primary_workspace_id is invalid')
    primary_root = _source_canonical_path(
        _required(item, 'primary_root', label),
        f'{label}.primary_root',
    )
    kit_root = _source_canonical_path(
        _required(item, 'kit_root', label),
        f'{label}.kit_root',
    )
    source_ref = _string(_required(item, 'source_ref', label), f'{label}.source_ref')
    kit_ref = _string(_required(item, 'kit_ref', label), f'{label}.kit_ref')
    for ref, ref_label in ((source_ref, 'source_ref'), (kit_ref, 'kit_ref')):
        if not ref.startswith(('refs/remotes/', 'refs/tags/')) or ref.endswith('/HEAD'):
            raise PlanValidationError(f'{label}.{ref_label} is not a full publication ref')
    ci_helper = _source_canonical_path(
        _required(item, 'ci_helper', label),
        f'{label}.ci_helper',
    )
    raw_helpers = _required(item, 'helper_bindings', label)
    if not isinstance(raw_helpers, list) or len(raw_helpers) > 64:
        raise PlanValidationError(f'{label}.helper_bindings exceeds its bound')
    helper_bindings: list[dict[str, str]] = []
    for index, raw_helper in enumerate(raw_helpers):
        helper_label = f'{label}.helper_bindings[{index}]'
        helper = _closed(raw_helper, {'link', 'target'}, helper_label)
        link = _source_canonical_path(
            _required(helper, 'link', helper_label),
            f'{helper_label}.link',
        )
        target = _source_canonical_path(
            _required(helper, 'target', helper_label),
            f'{helper_label}.target',
        )
        helper_bindings.append({'link': str(link), 'target': str(target)})
    _unique([row['link'] for row in helper_bindings], f'{label}.helper_bindings')
    return {
        'schema': SOURCE_ADMISSION_SCHEMA,
        'receipt_ref': {'path': str(receipt_path), 'sha256': receipt_sha256},
        'installation_binding_sha256': binding_sha256,
        'primary_workspace_id': workspace_id,
        'primary_root': str(primary_root),
        'kit_root': str(kit_root),
        'source_ref': source_ref,
        'kit_ref': kit_ref,
        'ci_helper': str(ci_helper),
        'helper_bindings': helper_bindings,
    }


def _source_native_identity(
    *,
    project_id: str,
    project_root: Path,
    primary_root: Path,
    kent_path: Path,
) -> str:
    projects = [row for row in _project_rows(kent_path, project_root) if row['id'] == project_id]
    if len(projects) != 1:
        raise PlanValidationError('native project identity/root differs from source admission')
    try:
        native_root = Path(projects[0]['path']).expanduser()
        if not native_root.is_absolute():
            native_root = project_root / native_root
        native_root = native_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise PlanValidationError('native project identity/root differs from source admission') from error
    if native_root != primary_root:
        raise PlanValidationError('native project identity/root differs from source admission')
    code, output, _ = _run(
        [str(kent_path), 'project', str(primary_root)],
        cwd=project_root,
    )
    if code != 0 or output.decode('utf-8', errors='replace').strip() != project_id:
        raise PlanValidationError('native project readback differs from source admission')
    pages = _kent_pages(kent_path, ['task', 'list', '--project', project_id], cwd=project_root)
    rows = _page_rows(pages, ('items', 'tasks'))
    task_ids = [_row_id(row, 'id', 'task_id') for row in rows]
    _unique(task_ids, 'native source-admission Task inventory')
    for task_id in sorted(task_ids):
        detail = _kent_json(
            kent_path,
            ['task', 'show', task_id, '--project', project_id, '--json'],
            cwd=project_root,
        )
        if _row_id(detail, 'id', 'task_id') != task_id:
            raise PlanValidationError('native Task identity changed during source admission')
        summary = detail.get('summary') if isinstance(detail.get('summary'), dict) else {}
        if summary.get('project_id') != project_id:
            raise PlanValidationError('native Task project differs during source admission')
        source = detail.get('source_workspace')
        if not isinstance(source, dict) or not isinstance(source.get('is_primary'), bool):
            raise PlanValidationError('native Task primary workspace evidence is malformed')
        project = detail.get('project')
        if not isinstance(project, dict):
            raise PlanValidationError('native primary workspace identity is inconsistent')
        source_root_path = _source_canonical_path(
            _required(source, 'root_path', f'Task {task_id}.source_workspace'),
            f'Task {task_id}.source_workspace.root_path',
        )
        workspace_id = _string(
            _required(source, 'workspace_id', f'Task {task_id}.source_workspace'),
            f'Task {task_id}.source_workspace.workspace_id',
        )
        default_workspace_id = _string(
            _required(project, 'default_workspace_id', f'Task {task_id}.project'),
            f'Task {task_id}.project.default_workspace_id',
        )
        if (
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,255}', workspace_id)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,255}', default_workspace_id)
        ):
            raise PlanValidationError('native primary workspace identity is inconsistent')
        if source['is_primary'] is False:
            try:
                source_root = source_root_path.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise PlanValidationError('native primary workspace identity is inconsistent') from error
            if source_root == primary_root or workspace_id == default_workspace_id:
                raise PlanValidationError('native primary workspace identity is inconsistent')
            continue
        source_root = _source_canonical_root(
            str(source_root_path),
            f'Task {task_id}.source_workspace.root_path',
        )
        if (
            source_root != primary_root
            or default_workspace_id != workspace_id
        ):
            raise PlanValidationError('native primary workspace identity is inconsistent')
        return workspace_id
    raise PlanValidationError('native primary workspace identity is unavailable or ambiguous')


def _source_binding(
    value: Mapping[str, Any],
    *,
    project_id: str,
    project_root: Path,
    kent_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes, bytes, str]:
    primary_root = _source_canonical_root(value['primary_root'], 'source_admission.primary_root')
    kit_root = _source_canonical_root(value['kit_root'], 'source_admission.kit_root')
    if primary_root != project_root:
        raise PlanValidationError('source primary root differs from the plan project root')
    native_workspace_id = _source_native_identity(
        project_id=project_id,
        project_root=project_root,
        primary_root=primary_root,
        kent_path=kent_path,
    )
    if native_workspace_id != value['primary_workspace_id']:
        raise PlanValidationError('source primary workspace ID differs from native readback')
    binding_path = Path.home() / '.kent' / 'project-memory-bindings' / f'{project_id}.json'
    _, binding_raw, _ = _read_source_record(
        binding_path,
        label='native installation binding',
        maximum=SOURCE_RECEIPT_BYTES_LIMIT,
        private=True,
    )
    if sha256_bytes(binding_raw) != value['installation_binding_sha256']:
        raise PlanValidationError('native installation binding digest differs')
    binding = _source_json(binding_raw, 'native installation binding')
    binding = _closed(binding, {
        'schema', 'project_id', 'primary_root', 'primary_workspace_id', 'profile_sha256',
        'receipt_path', 'receipt_sha256', 'kit_root', 'source_ref', 'kit_ref',
        'ci_helper', 'helper_bindings',
    }, 'native installation binding')
    if binding.get('schema') != SOURCE_BINDING_SCHEMA:
        raise PlanValidationError('native installation binding schema is unsupported')
    if binding.get('project_id') != project_id:
        raise PlanValidationError('native installation binding project differs')
    binding_profile = _sha256(
        _required(binding, 'profile_sha256', 'native installation binding'),
        'native installation binding.profile_sha256',
    )
    binding_helpers = _required(binding, 'helper_bindings', 'native installation binding')
    if not isinstance(binding_helpers, list) or len(binding_helpers) > 64:
        raise PlanValidationError('native installation helper inventory is invalid')
    normalized_binding_helpers: list[dict[str, str]] = []
    for index, raw_helper in enumerate(binding_helpers):
        helper_label = f'native installation binding.helper_bindings[{index}]'
        helper = _closed(raw_helper, {'link', 'target'}, helper_label)
        normalized_binding_helpers.append({
            'link': str(_source_canonical_path(
                _required(helper, 'link', helper_label), f'{helper_label}.link',
            )),
            'target': str(_source_canonical_path(
                _required(helper, 'target', helper_label), f'{helper_label}.target',
            )),
        })
    _unique([row['link'] for row in normalized_binding_helpers], 'native installation helper bindings')
    expected_binding = {
        'project_id': project_id,
        'primary_root': str(primary_root),
        'primary_workspace_id': native_workspace_id,
        'receipt_path': value['receipt_ref']['path'],
        'receipt_sha256': value['receipt_ref']['sha256'],
        'kit_root': str(kit_root),
        'source_ref': value['source_ref'],
        'kit_ref': value['kit_ref'],
        'ci_helper': value['ci_helper'],
    }
    if any(binding.get(key) != expected for key, expected in expected_binding.items()):
        raise PlanValidationError('native installation binding differs from source admission')
    sort_helpers = lambda rows: sorted(rows, key=lambda row: (row['link'], row['target']))
    if sort_helpers(normalized_binding_helpers) != sort_helpers(value['helper_bindings']):
        raise PlanValidationError('native installed helper bindings differ from source admission')
    expected_adapter = {
        'link': str(Path.home() / '.kent' / 'bin' / 'kent-project-memory'),
        'target': str(kit_root / '.kent/scripts/workflow-project-memory'),
    }
    if (
        expected_adapter not in normalized_binding_helpers
        or value['ci_helper'] != str(Path.home() / '.kent' / 'bin' / 'kent-prepare-github-ci')
    ):
        raise PlanValidationError('native Kit helper bindings are incomplete')
    profile_path = primary_root / '.kent/workflow-profile.toml'
    _, profile_raw, _ = _read_source_record(
        profile_path,
        label='installed project profile',
        maximum=SOURCE_RECEIPT_BYTES_LIMIT,
        private=False,
    )
    if sha256_bytes(profile_raw) != binding_profile:
        raise PlanValidationError('installed project profile differs from its binding')
    try:
        profile = tomllib.loads(profile_raw.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PlanValidationError('installed project profile is invalid') from error
    project_memory = profile.get('project_memory')
    if not isinstance(project_memory, dict) or project_memory.get('mode') != 'r2':
        raise PlanValidationError('installed project is not configured for R2 source admission')
    receipt_path, receipt_raw, _ = _read_source_record(
        value['receipt_ref']['path'],
        label='source publication receipt',
        maximum=SOURCE_RECEIPT_BYTES_LIMIT,
        private=True,
    )
    if sha256_bytes(receipt_raw) != value['receipt_ref']['sha256']:
        raise PlanValidationError('source publication receipt digest differs')
    receipt = _source_json(receipt_raw, 'source publication receipt')
    v2 = receipt.get('schema') == 'delivery-runtime-publication-v2'
    receipt = _closed(receipt, {
        'schema', 'project_id', 'workflow_id', 'source_commit', 'kit_commit',
        'runtime_roots', 'files', 'policy', 'project_memory',
    } | ({'local_inputs'} if v2 else set()), 'source publication receipt')
    if (
        receipt.get('schema') not in (SOURCE_RECEIPT_SCHEMA, 'delivery-runtime-publication-v2')
        or receipt.get('project_id') != project_id
        or receipt.get('policy') != 'delivery_reliability_v1'
    ):
        raise PlanValidationError('source publication receipt identity differs')
    bootstrap_workflow_id = _uuid(receipt.get('workflow_id'), 'source receipt workflow_id')
    for name in ('source_commit', 'kit_commit'):
        _sha1(receipt.get(name), f'source publication receipt.{name}')
    roots = receipt.get('runtime_roots')
    if not isinstance(roots, list) or not roots or len(roots) > MAX_LIST:
        raise PlanValidationError('source publication runtime roots are invalid')
    normalized_roots = [_source_canonical_root(root, 'source publication runtime root') for root in roots]
    _unique([str(root) for root in normalized_roots], 'source publication runtime roots')
    if primary_root not in normalized_roots or kit_root not in normalized_roots:
        raise PlanValidationError('source publication roots differ from installation binding')
    files = receipt.get('files')
    if (
        not isinstance(files, list)
        or not files
        or len(files) > SOURCE_RECEIPT_FILE_LIMIT
    ):
        raise PlanValidationError('source publication file inventory is invalid')
    local_inputs = receipt['local_inputs'] if v2 else []
    if (not isinstance(local_inputs, list)
            or len(files) + len(local_inputs) > SOURCE_RECEIPT_FILE_LIMIT):
        raise PlanValidationError('source publication local inventory is invalid')
    seen: set[tuple[str, str]] = set()
    total = 0
    for local, items in ((False, files), (True, local_inputs)):
        for item in items:
            item = _closed(item, {'root', 'relative_path', 'sha256', 'byte_size', 'mode'}
                           | ({'kind'} if local else set()), 'source publication entry')
            name = item['relative_path']
            source_root = item['root']
            if (not isinstance(name, str) or not name or name.startswith('/')
                    or '\\' in name or '\x00' in name
                    or any(part in ('', '.', '..') for part in name.split('/'))
                    or not isinstance(source_root, str) or source_root not in roots):
                raise PlanValidationError('source publication path is invalid')
            _sha256(item['sha256'], 'source publication entry.sha256')
            if (type(item['byte_size']) is not int
                    or not 0 <= item['byte_size'] <= SOURCE_FILE_BYTES_LIMIT
                    or type(item['mode']) is not int or not 0 <= item['mode'] <= 0o777
                    or item['mode'] & 0o022):
                raise PlanValidationError('source publication metadata is invalid')
            if (source_root, name) in seen:
                raise PlanValidationError('duplicate source publication entry')
            if local and (
                item['kind'] != 'local-instruction'
                or source_root not in {str(primary_root), str(kit_root)}
                or not name.startswith('.kent/skills/') or not name.endswith('.md')
                or any(c in name for c in '*?[]')
                or item['mode'] & 0o111
                or any(other_root == source_root and (
                    other.startswith(name + '/') or name.startswith(other + '/'))
                    for other_root, other in seen)
            ):
                raise PlanValidationError('source publication local instruction is invalid')
            seen.add((source_root, name))
            total += item['byte_size']
            if total > SOURCE_TOTAL_BYTES_LIMIT:
                raise PlanValidationError('source publication exceeds byte limit')
    memory = receipt.get('project_memory')
    if not isinstance(memory, dict) or set(memory) != {
        'root', 'project_key', 'project_uid', 'legacy_task_ids',
    }:
        raise PlanValidationError('source publication project-memory declaration is invalid')
    for name in ('root', 'project_key', 'project_uid'):
        if project_memory.get(name) != memory.get(name):
            raise PlanValidationError('source publication project-memory binding differs')
    return binding, receipt, binding_raw, receipt_raw, profile_raw, bootstrap_workflow_id


def admit_source_admission(
    admission: Any,
    *,
    project_id: str,
    workflow_id: str,
    project_root: str | Path,
    kent_path: str | Path,
) -> VerifiedSourceAdmission:
    """Verify native project identity, private install binding and pinned sources."""
    parsed = _v2_source_admission(admission, 'source_admission')
    if parsed is None:
        raise PlanValidationError('source admission is required')
    project_id = _native_project_id(project_id, 'source admission project_id')
    workflow_id = _uuid(workflow_id, 'source admission workflow_id')
    root = _source_canonical_root(str(project_root), 'source admission project_root')
    kent = _kent_path(str(kent_path), 'source admission kent')
    _verify_executable(kent, sha256_bytes(kent.read_bytes()))
    binding, receipt, binding_raw, receipt_raw, profile_raw, receipt_workflow_id = _source_binding(
        parsed,
        project_id=project_id,
        project_root=root,
        kent_path=kent,
    )
    if receipt_workflow_id != workflow_id:
        raise PlanValidationError('source publication receipt Workflow differs')
    if parsed['receipt_ref']['path'] != binding['receipt_path']:
        raise PlanValidationError('source publication receipt path differs from native binding')
    kit_root = Path(parsed['kit_root'])
    receipt_files = receipt['files']
    inspector_entries = [
        row for row in receipt_files
        if isinstance(row, dict)
        and row.get('root') == str(kit_root)
        and row.get('relative_path') == 'scripts/inspect-delivery-source'
    ]
    if len(inspector_entries) != 1:
        raise PlanValidationError('source publication receipt lacks a unique qualified inspector')
    inspector_entry = _closed(
        inspector_entries[0],
        {'root', 'relative_path', 'sha256', 'byte_size', 'mode'},
        'source inspector receipt entry',
    )
    inspector_digest = _sha256(
        _required(inspector_entry, 'sha256', 'source inspector receipt entry'),
        'source inspector receipt entry.sha256',
    )
    inspector_size = _required(inspector_entry, 'byte_size', 'source inspector receipt entry')
    inspector_mode = _required(inspector_entry, 'mode', 'source inspector receipt entry')
    if (
        not isinstance(inspector_size, int)
        or isinstance(inspector_size, bool)
        or not 0 <= inspector_size <= SOURCE_FILE_BYTES_LIMIT
        or not isinstance(inspector_mode, int)
        or isinstance(inspector_mode, bool)
        or not 0 <= inspector_mode <= 0o777
        or inspector_mode & 0o022
    ):
        raise PlanValidationError('source inspector receipt metadata is invalid')
    inspector_path = kit_root / 'scripts/inspect-delivery-source'
    _, inspector_raw, actual_mode = _read_source_record(
        inspector_path,
        label='qualified source inspector',
        maximum=SOURCE_FILE_BYTES_LIMIT,
        private=False,
    )
    if (
        len(inspector_raw) != inspector_size
        or sha256_bytes(inspector_raw) != inspector_digest
        or bool(actual_mode & 0o100) != bool(inspector_mode & 0o100)
    ):
        raise PlanValidationError('installed source inspector differs from receipt')
    from .delivery import source_admission_gate

    gate = source_admission_gate(
        parsed,
        project_id=project_id,
        workflow_id=workflow_id,
        project_root=str(root),
    )
    helper_args: list[str] = []
    for helper in sorted(parsed['helper_bindings'], key=lambda row: row['link']):
        helper_args.extend(('--installed-helper', helper['link'], helper['target']))
    command = [
        sys.executable,
        '-I',
        '-S',
        '-c',
        (
            "import sys\n"
            "filename = sys.argv.pop(1)\n"
            "sys.argv[0] = filename\n"
            "source = sys.stdin.buffer.read(8388609)\n"
            "if len(source) > 8388608:\n"
            "    raise SystemExit(2)\n"
            "namespace = {'__file__': filename, '__name__': '__main__'}\n"
            "exec(compile(source, filename, 'exec'), namespace)\n"
        ),
        str(inspector_path),
        '--receipt', parsed['receipt_ref']['path'],
        '--receipt-sha256', parsed['receipt_ref']['sha256'],
        '--project-id', project_id,
        '--workflow-id', workflow_id,
        '--project-root', str(root),
        '--kit-root', parsed['kit_root'],
        '--primary-root', parsed['primary_root'],
        '--workspace', str(root),
        '--source-ref', parsed['source_ref'],
        '--kit-ref', parsed['kit_ref'],
        '--ci-helper', parsed['ci_helper'],
        *helper_args,
    ]
    if sum(len(value.encode('utf-8')) + 1 for value in command) > 128 * 1024:
        raise PlanValidationError('source inspector argument set exceeds its bound')
    try:
        with tempfile.TemporaryFile() as inspector_input, \
                tempfile.TemporaryFile() as stdout_file, \
                tempfile.TemporaryFile() as stderr_file:
            inspector_input.write(inspector_raw)
            inspector_input.seek(0)
            process = subprocess.Popen(
                command,
                cwd=str(root),
                env=_safe_env(),
                stdin=inspector_input,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
            )
            try:
                return_code = process.wait(timeout=SOURCE_INSPECTOR_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=2)
                raise PlanValidationError('source inspection timed out') from error
            stdout_file.seek(0)
            output = stdout_file.read(MAX_OUTPUT + 1)
            stderr_file.seek(0)
            stderr = stderr_file.read(MAX_OUTPUT + 1)
    except (OSError, subprocess.SubprocessError) as error:
        raise PlanValidationError('source inspector could not be executed safely') from error
    if return_code != 0 or len(output) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT:
        raise PlanValidationError('published source inspection was not admitted')
    inspection = _source_json(output, 'source inspector result')
    inspection = _closed(inspection, {
        'schema', 'status', 'project_id', 'workflow_id', 'receipt_sha256',
        'source_commit', 'kit_commit', 'file_count', 'source_bytes', 'workspace_path',
        'source_ref', 'source_ref_commit', 'kit_ref', 'kit_ref_commit', 'python_cache_tag',
    }, 'source inspector result')
    file_count = inspection.get('file_count')
    source_bytes = inspection.get('source_bytes')
    if (
        inspection.get('schema') != SOURCE_INSPECTION_SCHEMA
        or inspection.get('status') != 'admitted'
        or inspection.get('project_id') != project_id
        or inspection.get('workflow_id') != workflow_id
        or inspection.get('receipt_sha256') != parsed['receipt_ref']['sha256']
        or inspection.get('source_commit') != receipt.get('source_commit')
        or inspection.get('kit_commit') != receipt.get('kit_commit')
        or inspection.get('workspace_path') != str(root)
        or inspection.get('source_ref') != parsed['source_ref']
        or inspection.get('kit_ref') != parsed['kit_ref']
        or not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count != len(receipt_files) + len(receipt.get('local_inputs', []))
        or not 1 <= file_count <= SOURCE_RECEIPT_FILE_LIMIT
        or not isinstance(source_bytes, int)
        or isinstance(source_bytes, bool)
        or not 0 <= source_bytes <= SOURCE_TOTAL_BYTES_LIMIT
        or not re.fullmatch(r'[0-9a-f]{40}', str(inspection.get('source_ref_commit', '')))
        or not re.fullmatch(r'[0-9a-f]{40}', str(inspection.get('kit_ref_commit', '')))
        or inspection.get('python_cache_tag') != sys.implementation.cache_tag
    ):
        raise PlanValidationError('source inspector result differs from bound admission')
    _, binding_after, _ = _read_source_record(
        Path.home() / '.kent' / 'project-memory-bindings' / f'{project_id}.json',
        label='native installation binding',
        maximum=SOURCE_RECEIPT_BYTES_LIMIT,
        private=True,
    )
    _, receipt_after, _ = _read_source_record(
        parsed['receipt_ref']['path'],
        label='source publication receipt',
        maximum=SOURCE_RECEIPT_BYTES_LIMIT,
        private=True,
    )
    _, profile_after, _ = _read_source_record(
        Path(parsed['primary_root']) / '.kent/workflow-profile.toml',
        label='installed project profile',
        maximum=SOURCE_RECEIPT_BYTES_LIMIT,
        private=False,
    )
    if (
        binding_after != binding_raw
        or receipt_after != receipt_raw
        or profile_after != profile_raw
    ):
        raise PlanValidationError('source admission inputs changed during inspection')
    value_raw = canonical_bytes(parsed)
    inspection_raw = canonical_bytes(inspection)
    return VerifiedSourceAdmission(
        project_id=project_id,
        workflow_id=workflow_id,
        project_root=str(root),
        primary_workspace_id=parsed['primary_workspace_id'],
        primary_root=parsed['primary_root'],
        kit_root=parsed['kit_root'],
        source_admission_sha256=sha256_bytes(value_raw),
        receipt_sha256=parsed['receipt_ref']['sha256'],
        gate=gate,
        value_raw=value_raw,
        inspection_raw=inspection_raw,
        receipt_files_raw=canonical_bytes(receipt_files),
        _token=_VERIFIED_SOURCE_ADMISSION_TOKEN,
    )


def _v2_ref_list(
    value: Any,
    label: str,
    *,
    verify_files: bool = True,
) -> list[dict[str, Any]]:
    refs = []
    for index, raw in enumerate(_bounded_list(value, label)):
        ref_label = f'{label}[{index}]'
        ref = _closed(raw, {'path', 'sha256'}, ref_label)
        path = _absolute_path(_required(ref, 'path', ref_label), f'{ref_label}.path')
        expected = _sha256(_required(ref, 'sha256', ref_label), f'{ref_label}.sha256')
        if verify_files:
            path, contents = _read_reference(raw, ref_label)
            expected = sha256_bytes(contents)
        refs.append({'path': str(path), 'sha256': expected})
    refs.sort(key=lambda row: row['path'])
    _unique([row['path'] for row in refs], label)
    return refs


def _native_components(value: Any, label: str) -> dict[str, Any]:
    v2 = isinstance(value, dict) and value.get('schema') == NATIVE_COMPONENTS_V2_SCHEMA
    raw = _closed(value, {
        'schema', 'approvals', 'questions', 'sessions', 'terminal_inventory_sha256',
    } | ({'task_execution_bindings_sha256'} if v2 else set()), label)
    if raw.get('schema') not in (NATIVE_COMPONENTS_SCHEMA, NATIVE_COMPONENTS_V2_SCHEMA):
        raise PlanValidationError(f'{label}.schema is unsupported')
    approvals = []
    for index, row_value in enumerate(_bounded_list(_required(raw, 'approvals', label), f'{label}.approvals')):
        row_label = f'{label}.approvals[{index}]'
        row = _closed(row_value, {'task_id', 'approval_id', 'record_sha256'}, row_label)
        approvals.append({
            'task_id': _string(_required(row, 'task_id', row_label), f'{row_label}.task_id'),
            'approval_id': _string(_required(row, 'approval_id', row_label), f'{row_label}.approval_id'),
            'record_sha256': _sha256(
                _required(row, 'record_sha256', row_label), f'{row_label}.record_sha256',
            ),
        })
    approvals.sort(key=lambda row: (row['task_id'], row['approval_id']))
    _unique([f"{row['task_id']}:{row['approval_id']}" for row in approvals], f'{label}.approvals')
    questions = []
    for index, row_value in enumerate(_bounded_list(_required(raw, 'questions', label), f'{label}.questions')):
        row_label = f'{label}.questions[{index}]'
        row = _closed(row_value, {
            'task_id', 'session_id', 'call_id', 'event_sequence', 'input_sha256',
        }, row_label)
        sequence = _required(row, 'event_sequence', row_label)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise PlanValidationError(f'{row_label}.event_sequence is invalid')
        questions.append({
            'task_id': _string(_required(row, 'task_id', row_label), f'{row_label}.task_id'),
            'session_id': _string(_required(row, 'session_id', row_label), f'{row_label}.session_id'),
            'call_id': _string(_required(row, 'call_id', row_label), f'{row_label}.call_id'),
            'event_sequence': sequence,
            'input_sha256': _sha256(
                _required(row, 'input_sha256', row_label), f'{row_label}.input_sha256',
            ),
        })
    questions.sort(key=lambda row: (
        row['task_id'], row['session_id'], row['call_id'], row['event_sequence'],
    ))
    _unique([
        f"{row['session_id']}:{row['call_id']}:{row['event_sequence']}" for row in questions
    ], f'{label}.questions')
    sessions = []
    for index, row_value in enumerate(_bounded_list(_required(raw, 'sessions', label), f'{label}.sessions')):
        row_label = f'{label}.sessions[{index}]'
        row = _closed(row_value, {'task_id', 'session_id', 'persisted_sha256', 'events'}, row_label)
        events = []
        for event_index, event_value in enumerate(_bounded_list(
            _required(row, 'events', row_label), f'{row_label}.events',
        )):
            event_label = f'{row_label}.events[{event_index}]'
            event = _closed(event_value, {'sequence', 'kind', 'sha256'}, event_label)
            sequence = _required(event, 'sequence', event_label)
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                raise PlanValidationError(f'{event_label}.sequence is invalid')
            events.append({
                'sequence': sequence,
                'kind': _string(_required(event, 'kind', event_label), f'{event_label}.kind'),
                'sha256': _sha256(_required(event, 'sha256', event_label), f'{event_label}.sha256'),
            })
        if events != sorted(events, key=lambda event: (event['sequence'], event['kind'])):
            raise PlanValidationError(f'{row_label}.events are not sorted')
        _unique([str(event['sequence']) for event in events], f'{row_label}.events')
        sessions.append({
            'task_id': _string(_required(row, 'task_id', row_label), f'{row_label}.task_id'),
            'session_id': _string(_required(row, 'session_id', row_label), f'{row_label}.session_id'),
            'persisted_sha256': _sha256(
                _required(row, 'persisted_sha256', row_label), f'{row_label}.persisted_sha256',
            ),
            'events': events,
        })
    if len(sessions) > MAX_LIST:
        raise PlanValidationError(f'{label}.sessions exceeds the detailed Session bound')
    sessions.sort(key=lambda row: (row['task_id'], row['session_id']))
    _unique([row['session_id'] for row in sessions], f'{label}.sessions')
    result = {
        'schema': raw['schema'],
        'approvals': approvals,
        'questions': questions,
        'sessions': sessions,
        'terminal_inventory_sha256': _sha256(
            _required(raw, 'terminal_inventory_sha256', label),
            f'{label}.terminal_inventory_sha256',
        ),
    }
    if v2:
        result['task_execution_bindings_sha256'] = _sha256(
            _required(raw, 'task_execution_bindings_sha256', label),
            f'{label}.task_execution_bindings_sha256',
        )
    return result


def _v2_native_descriptor(value: Any, label: str) -> dict[str, Any]:
    raw = _closed(value, {
        'schema', 'database_path', 'database_schema_sha256', 'session_roots',
    }, label)
    if raw.get('schema') != NATIVE_EVIDENCE_SCHEMA:
        raise PlanValidationError(f'{label}.schema is unsupported')
    database = _absolute_path(
        _required(raw, 'database_path', label), f'{label}.database_path',
    )
    roots = []
    for index, root_value in enumerate(_bounded_list(
        _required(raw, 'session_roots', label), f'{label}.session_roots',
    )):
        roots.append(str(_absolute_path(root_value, f'{label}.session_roots[{index}]')))
    roots.sort()
    if not roots:
        raise PlanValidationError(f'{label}.session_roots cannot be empty')
    _unique(roots, f'{label}.session_roots')
    return {
        'schema': NATIVE_EVIDENCE_SCHEMA,
        'database_path': str(database),
        'database_schema_sha256': _sha256(
            _required(raw, 'database_schema_sha256', label), f'{label}.database_schema_sha256',
        ),
        'session_roots': roots,
    }


def _native_path_stat(path: Path, label: str, *, directory: bool = False) -> os.stat_result:
    path = _absolute_path(str(path), label)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise EffectBlocked(f'{label} is absent or inaccessible') from error
        if stat.S_ISLNK(info.st_mode):
            raise EffectBlocked(f'{label} traverses a symlink')
    try:
        info = path.lstat()
    except OSError as error:
        raise EffectBlocked(f'{label} is absent or inaccessible') from error
    if (
        info.st_uid != os.geteuid()
        or (directory and not stat.S_ISDIR(info.st_mode))
        or (not directory and not stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)
    ):
        raise EffectBlocked(f'{label} has an unsupported owner, kind, or link count')
    return info


def _native_open_nofollow(path: Path, label: str) -> int:
    if not path.is_absolute() or not path.parts:
        raise EffectBlocked(f'{label} is not an absolute path')
    directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    file_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    current = -1
    try:
        current = os.open(path.anchor, directory_flags)
        for part in path.parts[1:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current)
            os.close(current)
            current = next_fd
        descriptor = os.open(path.parts[-1], file_flags, dir_fd=current)
        return descriptor
    except OSError as error:
        raise EffectBlocked(f'{label} cannot be opened without following symlinks') from error
    finally:
        if current >= 0:
            os.close(current)


def _native_expected_paths(
    descriptor: Mapping[str, Any],
    project_id: str,
) -> tuple[Path, Path, Path]:
    environment = _safe_env()
    home_value = environment.get('HOME')
    if not isinstance(home_value, str) or not Path(home_value).is_absolute():
        raise EffectBlocked('native evidence requires an absolute HOME in the replacement environment')
    home = Path(home_value)
    root = home / '.kent'
    database = root / 'db' / 'main.sqlite3'
    session_root = root / 'projects' / project_id / 'sessions'
    if (
        descriptor['database_path'] != str(database)
        or descriptor['session_roots'] != [str(session_root)]
    ):
        raise EffectBlocked('native evidence paths do not bind the default local persistence root')
    _native_path_stat(root, 'native persistence root', directory=True)
    _native_path_stat(database, 'native persistence database')
    _native_path_stat(session_root, 'native Session root', directory=True)
    return root, database, session_root


def _native_process_evidence(
    pid: int,
    kent: Path,
    root: Path,
    database: Path,
    *,
    system: str | None = None,
    proc_root: Path = Path('/proc'),
) -> dict[str, Any]:
    system = sys.platform if system is None else system
    try:
        expected_executable = kent.resolve(strict=True)
        executable_stat = expected_executable.stat()
        database_stat = database.stat()
    except OSError as error:
        raise EffectBlocked('native process proof inputs are unavailable') from error
    if system == 'darwin':
        code, output, _ = _run([
            '/bin/ps', '-ww', '-p', str(pid), '-o', 'pid=,uid=,lstart=,command=',
        ], cwd=root, extra_env={'LANG': 'C', 'LC_ALL': 'C'})
        if code != 0:
            raise EffectBlocked('Darwin native process identity is unavailable')
        try:
            lines = output.decode('utf-8', errors='strict').splitlines()
            parts = lines[0].split(None, 7) if len(lines) == 1 else []
            if len(parts) != 8 or int(parts[0]) != pid or int(parts[1]) != os.geteuid():
                raise ValueError
            start = ' '.join(parts[2:7])
            argv = shlex.split(parts[7], posix=True)
            if not argv or Path(argv[0]).resolve(strict=True) != expected_executable:
                raise ValueError
        except (UnicodeDecodeError, ValueError, OSError) as error:
            raise EffectBlocked('Darwin native process command is ambiguous') from error
        if argv[1:] != ['serve', '--persistence-root', str(root)]:
            raise EffectBlocked('Darwin native process serves a different root or command')
        code, output, _ = _run([
            '/usr/sbin/lsof', '-n', '-P', '-l', '-a', '-p', str(pid), '-FpcfutDin',
        ], cwd=root)
        if code != 0 or len(output) > MAX_OUTPUT:
            raise EffectBlocked('Darwin native process descriptors are unavailable')
        try:
            records: list[dict[str, str]] = []
            current: dict[str, str] = {}
            process_id = None
            process_uid = None
            for line in output.decode('utf-8', errors='strict').splitlines():
                if not line:
                    continue
                field, value = line[0], line[1:]
                if field == 'p':
                    process_id = int(value)
                elif field == 'u':
                    process_uid = int(value)
                elif field == 'f':
                    if current:
                        records.append(current)
                    current = {'fd': value}
                elif field in {'t', 'D', 'i', 'n'}:
                    current[field] = value
            if current:
                records.append(current)
            if process_id != pid or process_uid != os.geteuid():
                raise ValueError
            executable_rows = [
                row for row in records
                if (
                    row.get('fd') == 'txt'
                    and row.get('n') == str(expected_executable)
                    and row.get('t') == 'REG'
                )
            ]
            database_rows = [
                row for row in records
                if row.get('n') == str(database) and row.get('t') == 'REG'
            ]
            if len(executable_rows) != 1 or not database_rows:
                raise ValueError
            executable_row = executable_rows[0]
            exe_identity = (int(executable_row['D'], 0), int(executable_row['i']))
            db_identities = {
                (int(row['D'], 0), int(row['i']))
                for row in database_rows
            }
            if (
                exe_identity != (executable_stat.st_dev, executable_stat.st_ino)
                or db_identities != {(database_stat.st_dev, database_stat.st_ino)}
            ):
                raise ValueError
            db_identity = next(iter(db_identities))
        except (UnicodeDecodeError, ValueError, KeyError) as error:
            raise EffectBlocked('Darwin native process executable/database descriptors differ') from error
        return {
            'pid': pid,
            'uid': os.geteuid(),
            'start': start,
            'argv': argv,
            'executable': list(exe_identity),
            'database': list(db_identity),
        }
    if system == 'linux':
        process = proc_root / str(pid)
        try:
            process_stat = process.stat()
            raw_cmdline = (process / 'cmdline').read_bytes()
            raw_stat = (process / 'stat').read_text(encoding='ascii')
            proc_executable = process / 'exe'
            proc_executable_stat = proc_executable.stat()
            descriptors = sorted(
                (entry for entry in (process / 'fd').iterdir() if entry.name.isdigit()),
                key=lambda entry: int(entry.name),
            )
            if process_stat.st_uid != os.geteuid() or len(descriptors) > 4096:
                raise ValueError
            if not raw_cmdline or not raw_cmdline.endswith(b'\x00') or len(raw_cmdline) > 32 * 1024:
                raise ValueError
            argv = [value.decode('utf-8', errors='strict') for value in raw_cmdline[:-1].split(b'\x00')]
            if not argv or Path(argv[0]).resolve(strict=True) != expected_executable:
                raise ValueError
            if argv[1:] != ['serve', '--persistence-root', str(root)]:
                raise ValueError
            after_comm = raw_stat[raw_stat.rfind(')') + 1:].split()
            start = int(after_comm[19])
            if (
                proc_executable_stat.st_dev != executable_stat.st_dev
                or proc_executable_stat.st_ino != executable_stat.st_ino
            ):
                raise ValueError
            db_matches = [
                descriptor for descriptor in descriptors
                if (
                    descriptor.stat().st_dev == database_stat.st_dev
                    and descriptor.stat().st_ino == database_stat.st_ino
                )
            ]
            if not db_matches:
                raise ValueError
        except (OSError, UnicodeDecodeError, ValueError, IndexError) as error:
            raise EffectBlocked('Linux native process identity or descriptors differ') from error
        return {
            'pid': pid,
            'uid': os.geteuid(),
            'start': start,
            'argv': argv,
            'executable': [executable_stat.st_dev, executable_stat.st_ino],
            'database': [database_stat.st_dev, database_stat.st_ino],
        }
    raise EffectBlocked(f'native process proof is unsupported on {system}')


NATIVE_COMMENTS_CONTEXT_SCHEMA = 'kit-native-local-context-v1'
NATIVE_COMMENTS_SCHEMA = 'kit-native-task-comments-v1'
NATIVE_COMMENTS_MAX_COUNT = 4096
NATIVE_COMMENTS_MAX_BYTES = 4 * 1024 * 1024
NATIVE_COMMENTS_SNAPSHOT_SECONDS = 2.0
NATIVE_COMMENT_FIELDS = (
    'id', 'task_id', 'body', 'author_kind', 'author_id',
    'created_at_unix_ms', 'updated_at_unix_ms',
)


def _native_listener_evidence(
    pid: int, port: int, root: Path, *, system: str | None = None,
    proc_root: Path = Path('/proc'),
) -> dict[str, Any]:
    """Prove socket ownership, not merely a PID reported by the health route."""
    system = sys.platform if system is None else system
    if system == 'darwin':
        code, output, _ = _run([
            '/usr/sbin/lsof', '-n', '-P', '-l', '-iTCP:' + str(port),
            '-sTCP:LISTEN', '-FpuftnT',
        ], cwd=root, timeout=5.0)
        if code:
            raise EffectBlocked('local native listener observation failed')
        try:
            rows = []
            owner = uid = None
            current: dict[str, Any] = {}
            for line in output.decode('utf-8', errors='strict').splitlines():
                if not line:
                    continue
                field, value = line[0], line[1:]
                if field in {'p', 'f'} and current:
                    rows.append(current)
                    current = {}
                if field == 'p':
                    owner, uid = int(value), None
                elif field == 'u':
                    uid = int(value)
                elif field == 'f':
                    current = {'pid': owner, 'uid': uid, 'fd': value}
                elif field in {'t', 'n'} or (field == 'T' and value.startswith('ST=')):
                    current[field] = value
            if current:
                rows.append(current)
            if (
                len(rows) != 1 or rows[0].get('pid') != pid
                or rows[0].get('uid') != os.geteuid()
                or rows[0].get('n') != f'127.0.0.1:{port}'
                or rows[0].get('t') != 'IPv4'
                or rows[0].get('T') != 'ST=LISTEN'
            ):
                raise ValueError
            return rows[0]
        except (UnicodeError, ValueError) as error:
            raise EffectBlocked('local native listener ownership is ambiguous') from error
    if system == 'linux':
        try:
            process = proc_root / str(pid)
            with (process / 'net' / 'tcp').open('rb') as stream:
                raw = stream.read(MAX_OUTPUT + 1)
            if len(raw) > MAX_OUTPUT:
                raise ValueError
            matches = []
            for line in raw.decode('ascii').splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10:
                    raise ValueError
                if fields[1] == f'0100007F:{port:04X}' and fields[3] == '0A':
                    if int(fields[7]) != os.geteuid():
                        raise ValueError
                    matches.append(fields[9])
            if len(matches) != 1:
                raise ValueError
            owners = []
            for index, entry in enumerate((process / 'fd').iterdir()):
                if index >= 4096:
                    raise ValueError
                if os.readlink(entry) == f'socket:[{matches[0]}]':
                    owners.append(entry.name)
            if not owners:
                raise ValueError
            return {'pid': pid, 'uid': os.geteuid(), 'inode': matches[0], 'fds': sorted(owners)}
        except (OSError, UnicodeError, ValueError) as error:
            raise EffectBlocked('local native listener ownership is unavailable') from error
    raise EffectBlocked('local native listener observation is unsupported')


def _native_comments_status(kent: Path, project_root: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    code, output, _ = _run([
        str(kent), 'service', 'status', '--json',
        '--persistence-root', environment['KENT_PERSISTENCE_ROOT'],
    ], cwd=project_root, extra_env=environment, timeout=5.0)
    if code:
        raise EffectBlocked('local native service status failed')
    try:
        value = json.loads(output.decode('utf-8'), object_pairs_hook=_duplicate_free)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (UnicodeError, ValueError, OperationError) as error:
        raise EffectBlocked('local native service status is invalid') from error


def native_local_context(*, kent: str, project_root: str) -> dict[str, Any]:
    """Discover a fixed local-only native CLI context; never read credentials."""
    executable = _kent_path(kent)
    workspace = _absolute_path(project_root, 'project_root')
    _native_path_stat(workspace, 'native execution root', directory=True)
    home = _safe_env().get('HOME', '')
    if not Path(home).is_absolute():
        raise EffectBlocked('local native context requires an absolute HOME')
    root = Path(home) / '.kent'
    database = root / 'db' / 'main.sqlite3'
    _native_path_stat(root, 'native persistence root', directory=True)
    before = _native_path_stat(database, 'native persistence database')
    environment = {'KENT_SERVER_HOST': '127.0.0.1', 'KENT_PERSISTENCE_ROOT': str(root)}
    digest = sha256_bytes(executable.read_bytes())
    status = _native_comments_status(executable, workspace, environment)
    endpoint = status.get('endpoint')
    match = re.fullmatch(r'http://127\.0\.0\.1:([0-9]{1,5})', endpoint) if isinstance(endpoint, str) else None
    if not match or not 1 <= int(match[1]) <= 65535:
        raise EffectBlocked('native service endpoint is not an unambiguous loopback port')
    port = int(match[1])
    environment['KENT_SERVER_PORT'] = str(port)
    pid = status.get('health_pid')
    command = status.get('command')
    if (
        status.get('health_status') != 'ok'
        or type(pid) is not int or pid <= 0
        or not isinstance(command, list) or not command
        or not all(isinstance(value, str) for value in command)
        or not Path(command[0]).is_absolute()
        or Path(command[0]).resolve() != executable.resolve()
        or command[1:] != ['serve', '--persistence-root', str(root)]
    ):
        raise EffectBlocked('native service identity does not bind the local root')
    process = _native_process_evidence(pid, executable, root, database)
    listener = _native_listener_evidence(pid, port, root)
    # Close discovery races before giving another caller a usable connection.
    repeated = _native_comments_status(executable, workspace, environment)
    after_process = _native_process_evidence(pid, executable, root, database)
    after_listener = _native_listener_evidence(pid, port, root)
    after = _native_path_stat(database, 'native persistence database')
    if (
        any(repeated.get(key) != status.get(key) for key in ('endpoint', 'health_pid', 'health_status', 'command'))
        or process != after_process or listener != after_listener
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or digest != sha256_bytes(executable.read_bytes())
    ):
        raise EffectBlocked('local native context changed during discovery')
    return {
        'schema': NATIVE_COMMENTS_CONTEXT_SCHEMA, 'kent': str(executable),
        'kent_sha256': digest, 'project_root': str(workspace),
        'persistence_root': str(root), 'host': '127.0.0.1', 'port': port,
        'process': process, 'listener': listener,
        'database': {'device': before.st_dev, 'inode': before.st_ino},
        'environment': environment,
    }


def validate_native_local_context(context: Any) -> dict[str, Any]:
    fields = {
        'schema', 'kent', 'kent_sha256', 'project_root', 'persistence_root',
        'host', 'port', 'process', 'listener', 'database', 'environment',
    }
    value = _closed(context, fields, 'local native context')
    if set(value) != fields or len(canonical_bytes(value)) > 16 * 1024:
        raise EffectBlocked('local native context is incomplete or oversized')
    fresh = native_local_context(kent=value['kent'], project_root=value['project_root'])
    if canonical_bytes(fresh) != canonical_bytes(value):
        raise EffectBlocked('local native context no longer matches the local service')
    return fresh


def _native_comments_columns(connection: sqlite3.Connection) -> None:
    # Only consumed columns/types form this observer's storage compatibility gate.
    tables = {
        'task_records': {'id': 'TEXT', 'project_id': 'TEXT', 'workflow_id': 'BLOB',
                         'source_workspace_id': 'TEXT', 'managed_worktree_id': 'TEXT'},
        'workspaces': {'id': 'TEXT', 'project_id': 'TEXT', 'canonical_root_path': 'TEXT'},
        'worktrees': {'id': 'TEXT', 'workspace_id': 'TEXT', 'canonical_root_path': 'TEXT'},
        'projects': {'id': 'TEXT'},
        'workflows': {'id': 'BLOB'},
        'task_comments': {key: ('INTEGER' if key.endswith('_unix_ms') else 'TEXT') for key in NATIVE_COMMENT_FIELDS},
    }
    for table, required in tables.items():
        columns = {row[1]: row[2].upper() for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if any(columns.get(key) != declared for key, declared in required.items()):
            raise EffectBlocked('native comments required columns/types differ')


def _native_prefixed_uuid(value: Any, prefix: str) -> str:
    value = _string(value, prefix + ' identity')
    if not value.startswith(prefix + '-') or _uuid(value[len(prefix) + 1:], prefix) != value[len(prefix) + 1:]:
        raise EffectBlocked('native identity has an invalid format')
    return value


def read_native_task_comments(
    context: Any, *, task_id: str, project_id: str, workflow_id: str,
) -> dict[str, Any]:
    """Read one bounded WAL-aware snapshot, returning all authority or none."""
    task_id = _native_prefixed_uuid(task_id, 'task')
    project_id = _native_project_id(project_id, 'project_id')
    workflow_id = _uuid(workflow_id, 'workflow_id')
    validated = validate_native_local_context(context)
    database = Path(validated['persistence_root']) / 'db' / 'main.sqlite3'
    before = _native_path_stat(database, 'native persistence database')
    deadline = time.monotonic() + NATIVE_COMMENTS_SNAPSHOT_SECONDS
    connection = None
    try:
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.25, isolation_level=None)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, NATIVE_COMMENTS_MAX_BYTES)
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA busy_timeout=250')
        connection.execute('BEGIN')
        if connection.execute('PRAGMA query_only').fetchone() != (1,):
            raise EffectBlocked('native comments snapshot is not query-only')
        if [row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main'] != [str(database)]:
            raise EffectBlocked('native comments database path differs')
        _native_comments_columns(connection)
        bindings = connection.execute(
            'SELECT t.project_id,t.workflow_id,w.project_id,w.canonical_root_path,'
            't.managed_worktree_id,wt.workspace_id,wt.canonical_root_path '
            'FROM task_records t JOIN projects p ON p.id=t.project_id '
            'JOIN workflows f ON f.id=t.workflow_id '
            'JOIN workspaces w ON w.id=t.source_workspace_id '
            'LEFT JOIN worktrees wt ON wt.id=t.managed_worktree_id WHERE t.id=? LIMIT 2',
            (task_id,),
        ).fetchall()
        if len(bindings) != 1:
            raise EffectBlocked('native Task binding is missing or ambiguous')
        binding = bindings[0]
        source_workspace = connection.execute(
            'SELECT source_workspace_id FROM task_records WHERE id=? LIMIT 2', (task_id,),
        ).fetchall()
        expected_root = binding[6] if binding[4] is not None else binding[3]
        if (
            binding[0] != project_id or binding[1] != uuid.UUID(workflow_id).bytes
            or binding[2] != project_id
            or (binding[4] is not None and binding[5] != source_workspace[0][0])
            or not isinstance(expected_root, str) or not Path(expected_root).is_absolute()
            or Path(expected_root).resolve() != Path(validated['project_root']).resolve()
        ):
            raise EffectBlocked('native Task project/Workflow/execution root differs')
        size = connection.execute(
            'SELECT count(*),coalesce(sum(coalesce(length(CAST(id AS BLOB)),0)+'
            'coalesce(length(CAST(task_id AS BLOB)),0)+coalesce(length(CAST(body AS BLOB)),0)+'
            'coalesce(length(CAST(author_kind AS BLOB)),0)+coalesce(length(CAST(author_id AS BLOB)),0)),0),'
            'coalesce(max(length(CAST(body AS BLOB))),0) FROM task_comments WHERE task_id=?',
            (task_id,),
        ).fetchone()
        if size[0] > NATIVE_COMMENTS_MAX_COUNT or size[1] > NATIVE_COMMENTS_MAX_BYTES or size[2] > 262144:
            raise EffectBlocked('native comments snapshot exceeds count/byte limits')
        rows = connection.execute(
            'SELECT ' + ','.join(NATIVE_COMMENT_FIELDS)
            + ' FROM task_comments WHERE task_id=? ORDER BY created_at_unix_ms,id LIMIT ?',
            (task_id, NATIVE_COMMENTS_MAX_COUNT + 1),
        ).fetchall()
        comments = []
        seen = set()
        for row in rows:
            comment = dict(zip(NATIVE_COMMENT_FIELDS, row))
            identity = _native_prefixed_uuid(comment['id'], 'comment')
            if (
                identity in seen or comment['task_id'] != task_id
                or comment['author_kind'] not in {'user', 'agent'}
                or not isinstance(comment['body'], str) or not comment['body'].strip()
                or not isinstance(comment['author_id'], str)
                or len(comment['author_id'].encode('utf-8')) > 4096
                or any(type(comment[key]) is not int or not 0 <= comment[key] <= 2**63 - 1
                       for key in ('created_at_unix_ms', 'updated_at_unix_ms'))
                or comment['updated_at_unix_ms'] < comment['created_at_unix_ms']
            ):
                raise EffectBlocked('native comment identity/author/timestamp is invalid')
            seen.add(identity)
            comments.append(comment)
        if len(rows) != size[0] or time.monotonic() > deadline:
            raise EffectBlocked('native comments snapshot exceeded bounds')
    except (sqlite3.Error, UnicodeError, TypeError, ValueError) as error:
        # Do not leak database bodies, provider data, or SQL parameter payloads.
        raise EffectBlocked('native comments snapshot is unavailable or incompatible') from error
    finally:
        if connection is not None:
            connection.close()
    after = _native_path_stat(database, 'native persistence database')
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise EffectBlocked('native comments database identity changed')
    validate_native_local_context(validated)
    return {
        'schema': NATIVE_COMMENTS_SCHEMA, 'context': validated,
        'task_id': task_id, 'project_id': project_id, 'workflow_id': workflow_id,
        'comments': comments,
    }


def _native_process_binding(
    parsed: Mapping[str, Any],
    descriptor: Mapping[str, Any],
    project_id: str,
) -> dict[str, Any]:
    root, database, _ = _native_expected_paths(descriptor, project_id)
    try:
        status = _kent_json(parsed['kent'], ['service', 'status', '--json'], cwd=parsed['project_root'])
        command = status.get('command')
        pid = status.get('health_pid')
        if (
            status.get('health_status') != 'ok'
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(command, list)
            or len(command) != 4
            or not isinstance(command[0], str)
            or not Path(command[0]).is_absolute()
            or Path(command[0]).resolve(strict=True) != parsed['kent'].resolve(strict=True)
            or command[1:] != ['serve', '--persistence-root', str(root)]
        ):
            raise EffectBlocked('native service status does not bind the pinned executable and default root')
        evidence = _native_process_evidence(pid, parsed['kent'], root, database)
    except EffectBlocked:
        raise
    except (OperationError, OSError, subprocess.SubprocessError) as error:
        raise EffectBlocked('native service/process identity could not be collected') from error
    if not isinstance(evidence, dict):
        raise EffectBlocked('native service status does not bind the pinned executable and default root')
    return evidence


def _native_schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    if not rows:
        raise EffectBlocked('native persistence schema is empty')
    return canonical_sha256([list(row) for row in rows])


def _native_json(raw: str | bytes, label: str) -> Any:
    raw_size = len(raw) if isinstance(raw, bytes) else len(raw.encode('utf-8'))
    if raw_size > NATIVE_MAX_DB_JSON_BYTES:
        raise EffectBlocked(f'{label} exceeds 8 MiB')

    def reject_constant(value: str) -> None:
        raise ValueError(f'non-finite JSON constant: {value}')

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f'non-finite JSON number: {value}')
        return parsed

    try:
        text = raw.decode('utf-8', errors='strict') if isinstance(raw, bytes) else raw
        return json.loads(
            text,
            object_pairs_hook=_duplicate_free,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, PlanValidationError) as error:
        raise EffectBlocked(f'{label} is malformed JSON') from error


def _native_rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    try:
        cursor = connection.execute(query, tuple(parameters))
        names = [column[0] for column in cursor.description or ()]
        rows = []
        while True:
            chunk = cursor.fetchmany(256)
            if not chunk:
                break
            rows.extend(dict(zip(names, row)) for row in chunk)
            if len(rows) > NATIVE_MAX_TERMINAL_IDENTITIES:
                raise EffectBlocked('native persistence result exceeds the identity bound')
        return rows
    except sqlite3.Error as error:
        raise EffectBlocked(f'native read-only persistence query failed: {error}') from error


def _native_rows_by_ids(
    connection: sqlite3.Connection,
    query_prefix: str,
    ids: Sequence[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for start in range(0, len(ids), 128):
        chunk = ids[start:start + 128]
        placeholders = ','.join('?' for _ in chunk)
        rows.extend(_native_rows(connection, f'{query_prefix} ({placeholders})', chunk))
    return rows


def _native_event_selector(
    event: Mapping[str, Any],
    latest: dict[str, tuple[int, str, Mapping[str, Any], int]],
) -> None:
    seq = event.get('seq')
    kind = event.get('kind')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1 or not isinstance(kind, str):
        raise EffectBlocked('native Session event identity is malformed')

    def record(selector: str, source: Mapping[str, Any]) -> None:
        digest_size = len(canonical_bytes(source))
        previous = latest.get(selector)
        if previous is None or seq >= previous[0]:
            latest[selector] = (seq, kind, source, digest_size)
            unique_events = {
                (candidate[0], candidate[1]): candidate[3]
                for candidate in latest.values()
            }
            if sum(unique_events.values()) > NATIVE_MAX_SELECTED_EVENT_BYTES:
                raise EffectBlocked('native selected Session event components exceed 32 MiB')

    if kind in {'generation_context', 'configuration_updated', 'history_replaced'}:
        record(kind, event)
    if kind == 'configuration_updated':
        payload = event.get('payload')
        item = payload.get('item') if isinstance(payload, Mapping) else None
        raw = item.get('raw') if isinstance(item, Mapping) else None
        reasoning = raw.get('reasoning') if isinstance(raw, Mapping) else None
        if (
            not isinstance(item, Mapping)
            or item.get('type') != 'configuration_update'
            or not isinstance(item.get('configuration_effort'), str)
            or not isinstance(raw, Mapping)
            or raw.get('type') != 'configuration_update'
            or not isinstance(reasoning, Mapping)
            or not isinstance(reasoning.get('effort'), str)
        ):
            raise EffectBlocked('native configuration event shape is unsupported')
    if kind == 'history_replaced':
        payload = event.get('payload')
        cache_metadata_shape = (
            isinstance(payload, Mapping)
            and (
                'cache_metadata' not in payload
                or isinstance(payload.get('cache_metadata'), (Mapping, list))
            )
        )
        compacted_output_shape = (
            isinstance(payload, Mapping)
            and (
                isinstance(payload.get('compacted_output'), Mapping)
                or isinstance(payload.get('items'), list)
            )
        )
        if (
            not isinstance(payload, Mapping)
            or not {'committed_entry_start', 'compaction_number', 'engine', 'mode'}.issubset(payload)
            or (
                not isinstance(payload.get('committed_entry_start'), int)
                or isinstance(payload.get('committed_entry_start'), bool)
                or payload.get('committed_entry_start') < 0
            )
            or not isinstance(payload.get('compaction_number'), int)
            or isinstance(payload.get('compaction_number'), bool)
            or payload.get('compaction_number') < 1
            or not isinstance(payload.get('engine'), str)
            or not payload.get('engine')
            or payload.get('mode') not in {'auto', 'handoff', 'manual', 'workflow_post_completion'}
            or not cache_metadata_shape
            or not compacted_output_shape
        ):
            raise EffectBlocked('native history-replacement event shape is unsupported')
    if kind == 'message':
        payload = event.get('payload')
        if isinstance(payload, Mapping) and payload.get('role') == 'developer':
            message_type = payload.get('message_type')
            if message_type not in {'background_notice', 'compaction_soon_reminder'}:
                record(f'developer:{seq}', event)
            if message_type in {'environment', 'workflow_mode', 'subagents', 'skills', 'agents.md'}:
                record(f'message:{message_type}', event)
    if kind == 'generation_context':
        payload = event.get('payload')
        if not isinstance(payload, Mapping) or set(payload) != {'before_summary', 'after_summary'}:
            raise EffectBlocked('native generation-context event shape is unsupported')
        for field in ('before_summary', 'after_summary'):
            messages = payload[field]
            if not isinstance(messages, list):
                raise EffectBlocked('native generation-context messages are malformed')
            for message in messages:
                if not isinstance(message, Mapping) or message.get('role') != 'developer':
                    continue
                message_type = message.get('message_type')
                if message_type in {'environment', 'workflow_mode', 'subagents', 'skills', 'agents.md'}:
                    record(f'message:{message_type}', event)


def _native_scan_events(
    path: Path,
    *,
    expected_last_sequence: int,
    aggregate_bytes: list[int],
    selected_bytes: list[int],
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
    set[str],
]:
    before = _native_path_stat(path, 'native Session event file')
    if before.st_size > NATIVE_MAX_EVENT_FILE_BYTES:
        raise EffectBlocked('native Session event file exceeds 256 MiB')
    aggregate_bytes[0] += before.st_size
    if aggregate_bytes[0] > NATIVE_MAX_EVENT_TOTAL_BYTES:
        raise EffectBlocked('native Session event inventory exceeds 2 GiB')
    descriptor = _native_open_nofollow(path, 'native Session event file')
    latest: dict[str, tuple[int, str, Mapping[str, Any], int]] = {}
    last_event: dict[str, Any] | None = None
    header_seen = False
    last_sequence = 0
    line_count = 0
    bytes_read = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
        ):
            raise EffectBlocked('native Session event file changed before reading')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            while True:
                line = stream.readline(NATIVE_MAX_EVENT_LINE_BYTES + 1)
                if not line:
                    break
                line_count += 1
                if len(line) > NATIVE_MAX_EVENT_LINE_BYTES:
                    raise EffectBlocked('native Session event record exceeds 8 MiB')
                bytes_read += len(line)
                if bytes_read > NATIVE_MAX_EVENT_FILE_BYTES:
                    raise EffectBlocked('native Session event file exceeds 256 MiB while streaming')
                if bytes_read > before.st_size:
                    aggregate_bytes[0] += bytes_read - before.st_size
                    if aggregate_bytes[0] > NATIVE_MAX_EVENT_TOTAL_BYTES:
                        raise EffectBlocked('native Session event inventory exceeds 2 GiB while streaming')
                if not line.endswith(b'\n'):
                    raise EffectBlocked('native Session event record is not newline terminated')
                record = line[:-1]
                if record.endswith(b'\r'):
                    record = record[:-1]
                value = _native_json(record, 'native Session event')
                if not isinstance(value, dict):
                    raise EffectBlocked('native Session event is not an object')
                if not header_seen:
                    if line_count != 1 or value != {'contract': 'kent.session.events', 'version': 2}:
                        raise EffectBlocked('native Session event contract header is absent or unsupported')
                    header_seen = True
                    continue
                sequence = value.get('seq')
                if (
                    not isinstance(sequence, int)
                    or isinstance(sequence, bool)
                    or sequence <= last_sequence
                    or not isinstance(value.get('kind'), str)
                    or not isinstance(value.get('payload'), dict)
                ):
                    raise EffectBlocked('native Session event sequence or envelope is malformed')
                last_sequence = sequence
                last_event = value
                _native_event_selector(value, latest)
        after = os.fstat(descriptor)
        path_after = _native_path_stat(path, 'native Session event file')
        if (
            (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                opened.st_ctime_ns, opened.st_mode, opened.st_uid, opened.st_nlink,
            )
            != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink,
            )
            or (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink,
            )
            != (
                path_after.st_dev, path_after.st_ino, path_after.st_size, path_after.st_mtime_ns,
                path_after.st_ctime_ns, path_after.st_mode, path_after.st_uid, path_after.st_nlink,
            )
        ):
            raise EffectBlocked('native Session event file changed during reading')
    finally:
        os.close(descriptor)
    if last_sequence != expected_last_sequence:
        raise EffectBlocked('native Session event sequence differs from persisted last_sequence')
    if not header_seen:
        raise EffectBlocked('native Session event contract header is absent')
    chosen = {
        (sequence, kind): (event, size)
        for sequence, kind, event, size in latest.values()
    }
    events = []
    selected_size = 0
    for identity, (event, size) in sorted(chosen.items()):
        selected_size += size
        events.append({
            'sequence': identity[0],
            'kind': identity[1],
            'sha256': canonical_sha256(event),
        })
    if len(events) > NATIVE_MAX_SELECTED_EVENTS_PER_SESSION:
        raise EffectBlocked('native selected Session event count exceeds 256')
    if selected_size > NATIVE_MAX_SELECTED_EVENT_BYTES:
        raise EffectBlocked('native selected Session event components exceed 32 MiB')
    selected_bytes[0] += selected_size
    if selected_bytes[0] > NATIVE_MAX_SELECTED_EVENT_BYTES:
        raise EffectBlocked('native selected event components exceed 32 MiB')
    history_event = latest.get('history_replaced')
    return (
        events,
        last_event,
        history_event[2] if history_event is not None else None,
        set(latest),
    )


def _native_question_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        'question', 'suggestions', 'recommended_option_index',
    }:
        raise EffectBlocked('native pending Question input is malformed')
    question = value.get('question')
    suggestions = value.get('suggestions')
    recommended = value.get('recommended_option_index')
    if (
        not isinstance(question, str)
        or not question.strip()
        or not isinstance(suggestions, list)
        or len(suggestions) > MAX_LIST
        or any(not isinstance(option, str) or not option for option in suggestions)
        or (
            recommended is not None
            and (
                not isinstance(recommended, int)
                or isinstance(recommended, bool)
                or not 1 <= recommended <= len(suggestions)
            )
        )
    ):
        raise EffectBlocked('native pending Question fields are malformed')
    return dict(value)


def _native_question_matches_cli(
    kent: Path,
    root: Path,
    project_id: str,
    session_id: str,
    value: Any,
) -> None:
    question = _native_question_input(value)
    lines = [question['question']]
    if question['suggestions']:
        lines.append('Suggestions:')
        for index, suggestion in enumerate(question['suggestions'], start=1):
            rendered = f'{index}. {suggestion}'
            if question['recommended_option_index'] == index:
                rendered += ' (recommended)'
            lines.append(rendered)
    expected = ('\n'.join(lines) + '\n').encode('utf-8')
    code, output, error_output = _run(
        [
            str(kent),
            'question',
            '--session',
            session_id,
            '--project',
            project_id,
        ],
        cwd=root,
        stdin=subprocess.DEVNULL,
    )
    if code != 0 or error_output or output != expected:
        raise EffectBlocked('native pending Question output differs from its persisted input')


def _native_execution_bindings(
    connection: sqlite3.Connection,
    project_id: str,
    tasks: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Observe only persisted bindings, without requalifying historical roots."""
    task_keys = (
        'id', 'source_workspace_id', 'managed_worktree_id', 'execution_target_mode',
        'execution_target_requested_ref', 'execution_target_resolved_ref',
        'execution_target_commit_oid', 'execution_target_provenance',
    )
    task_bindings = sorted(
        [{key: row[key] for key in task_keys} for row in tasks], key=lambda row: row['id'],
    )

    def references(rows: Sequence[Mapping[str, Any]], key: str) -> set[str]:
        values = set()
        for row in rows:
            value = row[key]
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise EffectBlocked('native execution binding identity is invalid')
                values.add(value)
        return values

    worktree_ids = references(tasks, 'managed_worktree_id') | references(sessions, 'worktree_id')
    workspace_ids = references(tasks, 'source_workspace_id') | references(sessions, 'workspace_id')
    if len(worktree_ids) > NATIVE_MAX_TERMINAL_IDENTITIES:
        raise EffectBlocked('native execution worktrees exceed the identity bound')
    worktrees = _native_rows_by_ids(
        connection,
        'SELECT id,workspace_id,canonical_root_path,creation_base_commit_oid FROM worktrees WHERE id IN',
        sorted(worktree_ids),
    )
    if len(worktrees) != len(worktree_ids) or {row['id'] for row in worktrees} != worktree_ids:
        raise EffectBlocked('native execution worktree join is missing or duplicated')
    workspace_ids |= references(worktrees, 'workspace_id')
    if any(row['workspace_id'] is None for row in worktrees):
        raise EffectBlocked('native execution worktree has no workspace')
    if len(workspace_ids) > NATIVE_MAX_TERMINAL_IDENTITIES:
        raise EffectBlocked('native execution workspaces exceed the identity bound')
    workspaces = _native_rows_by_ids(
        connection,
        'SELECT id,project_id,canonical_root_path FROM workspaces WHERE id IN',
        sorted(workspace_ids),
    )
    if (
        len(workspaces) != len(workspace_ids)
        or {row['id'] for row in workspaces} != workspace_ids
        or any(row['project_id'] != project_id for row in workspaces)
    ):
        raise EffectBlocked('native execution workspace join is missing, duplicated or foreign')
    for row in [*worktrees, *workspaces]:
        root = row['canonical_root_path']
        if not isinstance(root, str) or not Path(root).is_absolute() or os.path.normpath(root) != root:
            raise EffectBlocked('native execution canonical root is invalid')
    return {
        'tasks': task_bindings,
        'worktrees': sorted(worktrees, key=lambda row: row['id']),
        'workspaces': sorted(workspaces, key=lambda row: row['id']),
    }


def _native_approval_payload(
    approval: Mapping[str, Any],
    branches: Sequence[Mapping[str, Any]],
    task_nodes: Sequence[Mapping[str, Any]],
    workflow_id: str,
    graph: Mapping[str, Any],
) -> dict[str, Any]:
    # The version and prompt bytes belong to the frozen decision, not the
    # current graph. Its exact raw fingerprint is checked against the reviewed
    # snapshot; graph ownership/preimages are checked independently.
    version = approval.get('workflow_version')
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise EffectBlocked('native pending Approval workflow version is invalid')
    task_id = approval.get('source_task_id')
    source_node = approval.get('source_node_id')
    source_branch = approval.get('source_transition_branch_key')
    matching_nodes = [
        row for row in task_nodes
        if row.get('node_id') == source_node
        and row.get('transition_branch_key') == source_branch
    ]
    if len(matching_nodes) != 1:
        raise EffectBlocked('native pending Approval source is not a current Task node')
    if (
        approval.get('source_session_id') is not None
        and matching_nodes[0].get('session_id') != approval.get('source_session_id')
    ):
        raise EffectBlocked('native pending Approval Session differs from its current Task node')
    decoded_approval: dict[str, Any] = {}
    for column in ('transition_snapshot_json', 'materialized_values_json'):
        value = _native_json(approval.get(column), f'native Approval {column}')
        if not isinstance(value, dict):
            raise EffectBlocked('native pending Approval JSON is not an object')
        decoded_approval[column] = value
    transition = decoded_approval['transition_snapshot_json']
    groups = [
        row for row in graph.get('transition_groups', [])
        if isinstance(row, Mapping)
        and row.get('id') == transition.get('id')
        and row.get('transition_id') == transition.get('transition_id')
    ]
    if (
        transition.get('workflow_id') != workflow_id
        or transition.get('source_node_id') != source_node
        or len(groups) != 1
        or groups[0].get('source_node_id') != source_node
    ):
        raise EffectBlocked('native pending Approval transition differs from the current Workflow graph')
    normalized_branches = []
    if not branches:
        raise EffectBlocked('native pending Approval has no transition branches')
    for branch in branches:
        decoded_branch: dict[str, Any] = {}
        for column in (
            'target_snapshot_json',
            'effective_edge_configuration_json',
            'context_source_resolution_json',
        ):
            value = _native_json(branch.get(column), f'native Approval branch {column}')
            if not isinstance(value, dict):
                raise EffectBlocked('native pending Approval branch JSON is not an object')
            decoded_branch[column] = value
        target = decoded_branch['target_snapshot_json']
        edge = decoded_branch['effective_edge_configuration_json']
        context = decoded_branch['context_source_resolution_json']
        graph_edges = [
            row for row in graph.get('edges', [])
            if isinstance(row, Mapping) and row.get('id') == edge.get('id')
        ]
        graph_nodes = [
            row for row in graph.get('nodes', [])
            if isinstance(row, Mapping) and row.get('id', row.get('node_id')) == target.get('node_id')
        ]
        prompt_keys = {'prompt', 'prompt_template'}
        graph_edge_keys = (
            (set(graph_edges[0]) | {'workflow_id'}) - prompt_keys
            if len(graph_edges) == 1 else set()
        )
        extra_edge_keys = set(edge) - prompt_keys - graph_edge_keys
        edge_matches_graph = (
            len(graph_edges) == 1
            and graph_edge_keys.issubset(edge)
            and extra_edge_keys.issubset({'input_bindings', 'output_requirements'})
            and all(
                edge.get(key) == value
                for key, value in graph_edges[0].items()
                if key not in prompt_keys
            )
            and all(edge[key] is None for key in extra_edge_keys)
        )
        if (
            branch.get('transition_branch_key') != transition.get('transition_id')
            or len(graph_edges) != 1
            or not edge_matches_graph
            or edge.get('workflow_id') != workflow_id
            or edge.get('transition_group_id') != groups[0].get('id')
            or target.get('entered_by_edge_id') != edge.get('id')
            or target.get('node_id') != edge.get('target_node_id')
            or len(graph_nodes) != 1
            or target.get('node_kind') != graph_nodes[0].get('kind')
            or set(context) != {'active_source', 'target_session'}
            or context.get('active_source') != {
                'kind': 'exact',
                'session_id': approval.get('source_session_id'),
            }
            or context.get('target_session') != {
                'kind': 'reuse',
                'session_id': approval.get('source_session_id'),
            }
        ):
            raise EffectBlocked('native pending Approval branch differs from the reviewed Workflow context')
        normalized_branches.append(dict(branch))
    normalized_branches.sort(key=lambda row: row['transition_branch_key'])
    if len({
        row['transition_branch_key'] for row in normalized_branches
    }) != len(normalized_branches):
        raise EffectBlocked('native pending Approval branches are duplicated')
    return {'approval': dict(approval), 'branches': normalized_branches}


def _native_observe(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
    live: Mapping[str, Any],
    task_facts: Sequence[Mapping[str, Any]],
    raw_details: Mapping[str, Mapping[str, Any]],
    cli_sessions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    descriptor = parsed.get('native_evidence')
    if not isinstance(descriptor, Mapping):
        raise EffectBlocked('native evidence descriptor is absent')
    root, database, session_root = _native_expected_paths(descriptor, item['project_id'])
    database_before = _native_path_stat(database, 'native persistence database')
    process_before = _native_process_binding(parsed, descriptor, item['project_id'])
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f'{database.as_uri()}?mode=ro',
            uri=True,
            timeout=1.0,
            isolation_level=None,
        )
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA busy_timeout=1000')
        connection.execute('BEGIN')
        if connection.execute('PRAGMA query_only').fetchone() != (1,):
            raise EffectBlocked('native SQLite connection is not query-only')
        if connection.execute('PRAGMA busy_timeout').fetchone() != (1000,):
            raise EffectBlocked('native SQLite busy timeout differs')
        database_rows = connection.execute('PRAGMA database_list').fetchall()
        main_paths = [row[2] for row in database_rows if row[1] == 'main']
        if main_paths != [str(database)]:
            raise EffectBlocked('native SQLite connection opened a different database path')
        observed_schema = _native_schema_fingerprint(connection)
        if observed_schema != descriptor['database_schema_sha256']:
            raise EffectBlocked('native SQLite schema fingerprint differs from the reviewed descriptor')

        project_rows = _native_rows(
            connection,
            'SELECT id,primary_workspace_id FROM projects WHERE id=?',
            (item['project_id'],),
        )
        if len(project_rows) != 1:
            raise EffectBlocked('native project identity is absent or duplicated')
        primary_workspace_id = project_rows[0]['primary_workspace_id']
        workspaces = _native_rows(
            connection,
            'SELECT id,project_id,canonical_root_path FROM workspaces WHERE id=? AND project_id=?',
            (primary_workspace_id, item['project_id']),
        )
        if (
            len(workspaces) != 1
            or Path(workspaces[0]['canonical_root_path']).resolve()
            != Path(live['project_path']).resolve()
        ):
            raise EffectBlocked('native project workspace differs from the CLI project root')
        workflow_bytes = bytes.fromhex(item['workflow_id'].replace('-', ''))
        workflow_rows = _native_rows(
            connection,
            'SELECT id,name,description,version,execution_target_policy,execution_target_custom_ref '
            'FROM workflows WHERE id=?',
            (workflow_bytes,),
        )
        if len(workflow_rows) != 1:
            raise EffectBlocked('native Workflow identity is absent or duplicated')
        native_workflow = workflow_rows[0]
        if (
            native_workflow['version'] != live['version']
            or native_workflow['name'] != live['metadata']['name']
            or native_workflow['description'] != live['metadata']['description']
            or _live_execution_target({
                'mode': native_workflow['execution_target_policy'],
                **(
                    {'custom_ref': native_workflow['execution_target_custom_ref']}
                    if native_workflow['execution_target_custom_ref'] is not None else {}
                ),
            }, 'native Workflow') != live['metadata']['execution_target']
        ):
            raise EffectBlocked('native Workflow metadata differs from the CLI')
        native_links = _native_rows(
            connection,
            'SELECT l.project_id,l.workflow_id,l.id,p.default_project_workflow_link_id '
            'FROM project_workflow_links l JOIN projects p ON p.id=l.project_id '
            'WHERE l.workflow_id=?',
            (workflow_bytes,),
        )
        normalized_links = []
        for row in native_links:
            workflow_id = (
                str(uuid.UUID(bytes=bytes(row['workflow_id'])))
                if isinstance(row['workflow_id'], bytes) else None
            )
            normalized_links.append({
                'project_id': row['project_id'],
                'workflow_id': workflow_id,
                'is_default': row['id'] == row['default_project_workflow_link_id'],
            })
        normalized_links.sort(key=lambda row: (row['project_id'], row['workflow_id'] or ''))
        if canonical_sha256(normalized_links) != canonical_sha256(live['all_links']):
            raise EffectBlocked('native project Workflow links differ from the CLI')

        task_rows = _native_rows(
            connection,
            'SELECT id,project_id,workflow_id,project_workflow_link_id,workflow_revision_seen,'
            'source_workspace_id,managed_worktree_id,execution_target_mode,execution_target_requested_ref,'
            'execution_target_resolved_ref,execution_target_commit_oid,execution_target_provenance '
            'FROM task_records WHERE project_id=? AND workflow_id=? ORDER BY id',
            (item['project_id'], workflow_bytes),
        )
        native_task_ids = [row['id'] for row in task_rows]
        if (
            len(native_task_ids) > NATIVE_MAX_TASKS
            or len(native_task_ids) != len(set(native_task_ids))
            or set(native_task_ids) != {row['id'] for row in live['tasks']}
        ):
            raise EffectBlocked('native task_records inventory differs from the CLI')
        selected_link_ids = {
            row['id'] for row in native_links if row['project_id'] == item['project_id']
        }
        if len(selected_link_ids) != 1 or any(
            row['project_workflow_link_id'] not in selected_link_ids
            for row in task_rows
        ):
            raise EffectBlocked('native Task Workflow-link ownership differs')
        current_nodes = _native_rows_by_ids(
            connection,
            'SELECT task_id,node_id,transition_branch_key,current_input_values_json,'
            'prior_node_values_json,session_id,scheduling_state,interruption_reason,'
            'interruption_detail_json,interrupted_at_unix_ms,entered_by_edge_id,'
            'effective_assignee,effective_thinking,assignee_origin '
            'FROM task_current_nodes WHERE task_id IN',
            native_task_ids,
        )
        nodes_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in native_task_ids}
        for row in current_nodes:
            for key in ('current_input_values_json', 'prior_node_values_json', 'interruption_detail_json'):
                if row.get(key) is not None:
                    parsed_json = _native_json(row[key], f'native current node {key}')
                    if not isinstance(parsed_json, dict):
                        raise EffectBlocked('native current-node JSON is not an object')
            nodes_by_task[row['task_id']].append(row)
        cli_task_by_id = {row['id']: row for row in task_facts}
        graph_node_kinds = {
            node.get('id', node.get('node_id')): node.get('kind')
            for node in live['graph']['nodes']
            if isinstance(node, Mapping)
        }
        for task_id, rows in nodes_by_task.items():
            rows.sort(key=lambda row: (
                row['node_id'], row['transition_branch_key'] or '',
            ))
            cli_detail = raw_details[task_id]
            cli_current = cli_detail.get('current_nodes')
            if cli_current is None:
                summary = cli_detail.get('summary')
                cli_current = summary.get('current_nodes', []) if isinstance(summary, Mapping) else []
            if not isinstance(cli_current, list):
                raise EffectBlocked('native CLI current-node inventory is malformed')
            cli_node_ids = []
            for cli_node in cli_current:
                if isinstance(cli_node, str):
                    node_id = cli_node
                elif isinstance(cli_node, Mapping):
                    node_id = cli_node.get('node_id', cli_node.get('id'))
                else:
                    raise EffectBlocked('native CLI current-node record is malformed')
                if not isinstance(node_id, str) or not node_id:
                    raise EffectBlocked('native CLI current-node identity is malformed')
                cli_node_ids.append(node_id)
            cli_node_ids.sort()
            if cli_node_ids != sorted(row['node_id'] for row in rows):
                raise EffectBlocked('native current-node ownership differs from the CLI')
            cli_node_rows = [row for row in cli_current if isinstance(row, Mapping)]
            if cli_node_rows:
                cli_nodes_by_id = {
                    row.get('node_id', row.get('id')): row for row in cli_node_rows
                }
                if len(cli_nodes_by_id) != len(cli_node_rows):
                    raise EffectBlocked('native CLI current-node identities are duplicated')
                for native_node in rows:
                    cli_node = cli_nodes_by_id[native_node['node_id']]
                    for column in (
                        'transition_branch_key', 'session_id', 'scheduling_state',
                        'interruption_reason', 'entered_by_edge_id', 'effective_assignee',
                        'effective_thinking', 'assignee_origin',
                    ):
                        if column in cli_node and cli_node[column] != native_node.get(column):
                            raise EffectBlocked(f'native current-node {column} differs from the CLI')
            if cli_task_by_id[task_id]['terminal'] and any(
                graph_node_kinds.get(row['node_id']) != 'terminal'
                for row in rows
            ):
                raise EffectBlocked('native terminal Task retains a non-terminal current node')

        approval_rows = _native_rows_by_ids(
            connection,
            'SELECT id,source_task_id,source_node_id,source_transition_branch_key,source_session_id,'
            'workflow_version,transition_snapshot_json,materialized_values_json,created_at_unix_ms '
            'FROM task_pending_approvals WHERE source_task_id IN',
            native_task_ids,
        )
        approval_rows.sort(key=lambda row: (row['source_task_id'], row['id']))
        approval_ids = [row['id'] for row in approval_rows]
        branches = _native_rows_by_ids(
            connection,
            'SELECT approval_id,transition_branch_key,target_snapshot_json,'
            'effective_edge_configuration_json,context_source_resolution_json '
            'FROM task_pending_approval_branches WHERE approval_id IN',
            approval_ids,
        ) if approval_ids else []
        branches_by_approval: dict[str, list[dict[str, Any]]] = {}
        for row in branches:
            branches_by_approval.setdefault(row['approval_id'], []).append(row)
        approval_components = []
        approvals_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in native_task_ids}
        for approval in approval_rows:
            task_id = approval['source_task_id']
            envelope = _native_approval_payload(
                approval,
                branches_by_approval.get(approval['id'], []),
                nodes_by_task[task_id],
                item['workflow_id'],
                live['graph'],
            )
            digest = canonical_sha256(envelope)
            approvals_by_task[task_id].append({
                'id': approval['id'],
                'payload_sha256': digest,
            })
            approval_components.append({
                'task_id': task_id,
                'approval_id': approval['id'],
                'record_sha256': digest,
            })
        for task_id, task in cli_task_by_id.items():
            detail = raw_details[task_id]
            summary = detail.get('summary') if isinstance(detail.get('summary'), Mapping) else {}
            approvals_for_task = approvals_by_task[task_id]
            if bool(approvals_for_task) != _canonical_v2_approval_signaled(detail, summary):
                raise EffectBlocked('native approval rows differ from the CLI pending state')
            task['pending_decisions'] = sorted(approvals_for_task, key=lambda row: row['id'])
        sessions_rows = _native_rows_by_ids(
            connection,
            'SELECT id,project_id,workspace_id,worktree_id,artifact_relpath,continuation_json,'
            'locked_json,metadata_json,previous_session_id,parent_agent_session_id,task_id,'
            'completed_compaction_count,last_sequence '
            'FROM sessions WHERE task_id IN',
            native_task_ids,
        )
        if len(sessions_rows) > NATIVE_MAX_TERMINAL_IDENTITIES:
            raise EffectBlocked('native Session/Task inventory exceeds 4096 identities')
        session_ids = [row['id'] for row in sessions_rows]
        associations = _native_rows_by_ids(
            connection,
            'SELECT session_id,node_id,transition_branch_key,associated_at_unix_ms '
            'FROM session_workflow_node_associations WHERE session_id IN',
            session_ids,
        ) if session_ids else []
        associations_by_session: dict[str, list[dict[str, Any]]] = {}
        for row in associations:
            associations_by_session.setdefault(row['session_id'], []).append(row)
        task_by_id = {row['id']: row for row in task_rows}
        cli_sessions_by_id = {row['id']: row for row in cli_sessions}
        detailed_task_ids = {
            task_id for task_id, row in cli_task_by_id.items()
            if not row['terminal']
        }
        detailed_rows = [
            row for row in sessions_rows if row['task_id'] in detailed_task_ids
        ]
        if set(cli_sessions_by_id) != {row['id'] for row in detailed_rows}:
            raise EffectBlocked('native detailed Session inventory differs from the CLI')
        if len(detailed_rows) > MAX_LIST:
            raise EffectBlocked('native detailed Session inventory exceeds 256')
        detailed_session_ids = {row['id'] for row in detailed_rows}
        for task_id, task in cli_task_by_id.items():
            if task['native_state'] != 'waiting_ask':
                continue
            current = nodes_by_task[task_id]
            current_session_task = next(
                (
                    session['task_id'] for session in detailed_rows
                    if len(current) == 1 and session['id'] == current[0].get('session_id')
                ),
                None,
            )
            if (
                len(current) != 1
                or not isinstance(current[0].get('session_id'), str)
                or current[0]['session_id'] not in detailed_session_ids
                or current_session_task != task_id
            ):
                raise EffectBlocked('native waiting Question is not bound to its current Task Session')
        terminal_rows = [
            row for row in sessions_rows if row['task_id'] not in detailed_task_ids
        ]
        terminal_tasks = [
            {
                'task_id': task_id,
                'status': cli_task_by_id[task_id]['status'],
                'native_state': cli_task_by_id[task_id]['native_state'],
                'current_nodes': sorted(row['node_id'] for row in nodes_by_task[task_id]),
                'session_ids': sorted(row['id'] for row in terminal_rows if row['task_id'] == task_id),
            }
            for task_id in native_task_ids if cli_task_by_id[task_id]['terminal']
        ]
        terminal_associations = sorted(
            [
                row for row in associations
                if any(session['id'] == row['session_id'] for session in terminal_rows)
            ],
            key=lambda row: (
                row['session_id'], row['node_id'], row['transition_branch_key'] or '',
                row['associated_at_unix_ms'],
            ),
        )
        terminal_identity_count = (
            len(terminal_tasks)
            + len(terminal_rows)
            + len(terminal_associations)
        )
        if terminal_identity_count > NATIVE_MAX_TERMINAL_IDENTITIES:
            raise EffectBlocked('native terminal inventory exceeds 4096 identities')
        terminal_inventory_sha256 = canonical_sha256({
            'tasks': terminal_tasks,
            'sessions': sorted(
                [{'task_id': row['task_id'], 'session_id': row['id']} for row in terminal_rows],
                key=lambda row: (row['task_id'], row['session_id']),
            ),
            'associations': terminal_associations,
            'terminal_anchors': live.get('terminal_anchors', []),
            'anchors': item['anchors'],
        })
        session_components = []
        question_components = []
        session_facts_by_id = {row['id']: row for row in cli_sessions}
        aggregate_bytes = [0]
        selected_bytes = [0]
        execution_bindings = _native_execution_bindings(
            connection, item['project_id'], task_rows, detailed_rows,
        )
        db_component_bytes = [len(canonical_bytes(execution_bindings))]
        if db_component_bytes[0] > NATIVE_MAX_DB_COMPONENT_BYTES:
            raise EffectBlocked('native execution bindings exceed the component byte bound')
        for row in sorted(detailed_rows, key=lambda value: (value['task_id'], value['id'])):
            try:
                _uuid(row['id'], 'native Session identity')
            except PlanValidationError as error:
                raise EffectBlocked('native Session identity is malformed') from error
            if (
                row['project_id'] != item['project_id']
                or row['artifact_relpath']
                != f"projects/{item['project_id']}/sessions/{row['id']}"
            ):
                raise EffectBlocked('native Session project/path ownership differs')
            continuation = _native_json(row['continuation_json'], 'native Session continuation')
            metadata = _native_json(row['metadata_json'], 'native Session metadata')
            locked = _native_json(row['locked_json'], 'native Session lock')
            if not isinstance(continuation, dict) or not isinstance(metadata, dict) or not isinstance(locked, dict):
                raise EffectBlocked('native Session persisted state is not an object')
            session_associations = sorted(
                associations_by_session.get(row['id'], []),
                key=lambda assoc: (
                    assoc['node_id'], assoc['transition_branch_key'] or '',
                    assoc['associated_at_unix_ms'],
                ),
            )
            persisted = {
                'session': {
                    key: row[key] for key in (
                        'id', 'project_id', 'workspace_id', 'worktree_id', 'artifact_relpath',
                        'continuation_json', 'locked_json', 'metadata_json', 'previous_session_id',
                        'parent_agent_session_id', 'task_id', 'completed_compaction_count',
                    )
                },
                'task': {
                    'id': task_by_id[row['task_id']]['id'],
                    'project_id': task_by_id[row['task_id']]['project_id'],
                    'workflow_id_hex': bytes(
                        task_by_id[row['task_id']]['workflow_id']
                    ).hex(),
                    'project_workflow_link_id': task_by_id[row['task_id']]['project_workflow_link_id'],
                    'workflow_revision_seen': task_by_id[row['task_id']]['workflow_revision_seen'],
                },
                'associations': session_associations,
                'current_nodes': nodes_by_task[row['task_id']],
            }
            db_component_bytes[0] += len(canonical_bytes(persisted))
            if db_component_bytes[0] > NATIVE_MAX_DB_COMPONENT_BYTES:
                raise EffectBlocked('native persisted Session components exceed 32 MiB')
            event_path = session_root / row['id'] / 'events.jsonl'
            events, last_event, history_event, event_selectors = _native_scan_events(
                event_path,
                expected_last_sequence=row['last_sequence'],
                aggregate_bytes=aggregate_bytes,
                selected_bytes=selected_bytes,
            )
            if not locked:
                history_payload = (
                    history_event.get('payload')
                    if isinstance(history_event, Mapping) else None
                )
                if (
                    not isinstance(history_payload, Mapping)
                    or history_payload.get('mode') != 'workflow_post_completion'
                    or 'message:environment' not in event_selectors
                    or not any(
                        selector.startswith('developer:') or selector == 'generation_context'
                        for selector in event_selectors
                    )
                ):
                    raise EffectBlocked(
                        'empty native Session lock lacks exact post-completion and instruction history'
                    )
            persisted_digest = canonical_sha256(persisted)
            session_components.append({
                'task_id': row['task_id'],
                'session_id': row['id'],
                'persisted_sha256': persisted_digest,
                'events': events,
            })
            cli_session = session_facts_by_id.get(row['id'])
            if cli_session is None or cli_session.get('task_id') != row['task_id']:
                raise EffectBlocked('native Session Task ownership differs from the CLI')
            if cli_task_by_id[row['task_id']]['native_state'] == 'waiting_ask':
                current_nodes_for_task = nodes_by_task[row['task_id']]
                # Retained historical Sessions remain evidence, but only the current node Session owns the Question.
                current_session_nodes = [
                    node for node in current_nodes_for_task
                    if node.get('session_id') == row['id']
                ]
                if current_session_nodes:
                    if last_event is None:
                        raise EffectBlocked('native pending Question Session has no final event')
                    payload = last_event.get('payload')
                    tool_calls = payload.get('tool_calls') if isinstance(payload, Mapping) else None
                    if (
                        last_event.get('kind') != 'message'
                        or not isinstance(payload, Mapping)
                        or payload.get('role') != 'assistant'
                        or not isinstance(tool_calls, list)
                        or len(tool_calls) != 1
                        or not isinstance(tool_calls[0], Mapping)
                        or tool_calls[0].get('name') != 'ask_question'
                        or not isinstance(tool_calls[0].get('call_id'), str)
                    ):
                        raise EffectBlocked('native waiting Question is not the final Session event')
                    question_input = _native_question_input(tool_calls[0].get('input'))
                    _native_question_matches_cli(
                        parsed['kent'],
                        Path(live['project_path']),
                        item['project_id'],
                        row['id'],
                        question_input,
                    )
                    question_components.append({
                        'task_id': row['task_id'],
                        'session_id': row['id'],
                        'call_id': tool_calls[0]['call_id'],
                        'event_sequence': last_event['seq'],
                        'input_sha256': canonical_sha256(question_input),
                    })
        session_components.sort(key=lambda row: (row['task_id'], row['session_id']))
        approval_components.sort(key=lambda row: (row['task_id'], row['approval_id']))
        question_components.sort(key=lambda row: (
            row['task_id'], row['session_id'], row['call_id'], row['event_sequence'],
        ))
        components = {
            'schema': NATIVE_COMPONENTS_V2_SCHEMA,
            'approvals': approval_components,
            'questions': question_components,
            'sessions': session_components,
            'terminal_inventory_sha256': terminal_inventory_sha256,
            'task_execution_bindings_sha256': canonical_sha256(execution_bindings),
        }
        detailed_cli_sessions = [
            dict(row) for row in cli_sessions if row['task_id'] in detailed_task_ids
        ]
        component_by_session = {
            row['session_id']: row for row in session_components
        }
        for row in detailed_cli_sessions:
            component = component_by_session.get(row['id'])
            if component is None:
                raise EffectBlocked('native detailed Session component is absent')
            row['contract_fingerprint'] = canonical_sha256(component)
        connection.rollback()
        connection.close()
        connection = None
        database_after = _native_path_stat(database, 'native persistence database')
        if (
            database_before.st_dev != database_after.st_dev
            or database_before.st_ino != database_after.st_ino
            or database_before.st_uid != database_after.st_uid
            or database_before.st_nlink != database_after.st_nlink
        ):
            raise EffectBlocked('native SQLite database identity changed during observation')
        after_process = _native_process_binding(parsed, descriptor, item['project_id'])
        if after_process != process_before:
            raise EffectBlocked('native Kent process identity drifted during observation')
        if len(canonical_bytes(components)) > 1024 * 1024:
            raise EffectBlocked('native compatibility component document exceeds the plan bound')
        return components, detailed_cli_sessions
    except sqlite3.Error as error:
        raise EffectBlocked(f'native read-only SQLite observation failed: {error}') from error
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()


def _v2_snapshot(
    value: Any,
    label: str,
    *,
    verify_files: bool = True,
    native_mode: bool = False,
) -> dict[str, Any]:
    fields = {
        'schema', 'workflow_id', 'project_id', 'expected_version', 'execution_target',
        'execution_root', 'tasks', 'sessions', 'live_executions',
    }
    if native_mode:
        fields.add('native_components')
    raw = _closed(value, fields, label)
    if raw.get('schema') != CANONICAL_V2_SNAPSHOT_SCHEMA:
        raise PlanValidationError(f'{label}.schema is unsupported')
    components = (
        _native_components(_required(raw, 'native_components', label), f'{label}.native_components')
        if native_mode else None
    )
    workflow_id = _uuid(_required(raw, 'workflow_id', label), f'{label}.workflow_id')
    project_id = _native_project_id(_required(raw, 'project_id', label), f'{label}.project_id')
    version = _required(raw, 'expected_version', label)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise PlanValidationError(f'{label}.expected_version is invalid')
    target = _plan_execution_target(_required(raw, 'execution_target', label), f'{label}.execution_target')
    root = _absolute_path(_required(raw, 'execution_root', label), f'{label}.execution_root')
    tasks = []
    for index, raw_task in enumerate(_bounded_list(_required(raw, 'tasks', label), f'{label}.tasks')):
        task_label = f'{label}.tasks[{index}]'
        task = _closed(raw_task, {
            'id', 'status', 'native_state', 'terminal', 'current_nodes', 'pending_decisions',
            'history_classification', 'history_refs',
        }, task_label)
        status = _string(_required(task, 'status', task_label), f'{task_label}.status')
        native_state = task.get('native_state')
        if native_state is not None:
            native_state = _string(native_state, f'{task_label}.native_state')
        terminal = _required(task, 'terminal', task_label)
        if not isinstance(terminal, bool):
            raise PlanValidationError(f'{task_label}.terminal is invalid')
        current_nodes = [
            _string(row, f'{task_label}.current_nodes[{node_index}]')
            for node_index, row in enumerate(_bounded_list(
                _required(task, 'current_nodes', task_label), f'{task_label}.current_nodes',
            ))
        ]
        current_nodes.sort()
        pending = []
        for decision_index, raw_decision in enumerate(_bounded_list(
            _required(task, 'pending_decisions', task_label), f'{task_label}.pending_decisions',
        )):
            decision_label = f'{task_label}.pending_decisions[{decision_index}]'
            decision = _closed(raw_decision, {'id', 'payload_sha256'}, decision_label)
            pending.append({
                'id': _string(_required(decision, 'id', decision_label), f'{decision_label}.id'),
                'payload_sha256': _sha256(
                    _required(decision, 'payload_sha256', decision_label),
                    f'{decision_label}.payload_sha256',
                ),
            })
        pending.sort(key=lambda row: row['id'])
        _unique([row['id'] for row in pending], f'{task_label}.pending_decisions')
        history = _string(
            _required(task, 'history_classification', task_label),
            f'{task_label}.history_classification',
        )
        refs = _v2_ref_list(
            _required(task, 'history_refs', task_label),
            f'{task_label}.history_refs',
            verify_files=verify_files,
        )
        tasks.append({
            'id': _string(_required(task, 'id', task_label), f'{task_label}.id'),
            'status': status,
            'native_state': native_state,
            'terminal': terminal,
            'current_nodes': current_nodes,
            'pending_decisions': pending,
            'history_classification': history,
            'history_refs': refs,
        })
    tasks.sort(key=lambda row: row['id'])
    _unique([row['id'] for row in tasks], f'{label}.tasks')
    sessions = []
    for index, raw_session in enumerate(_bounded_list(_required(raw, 'sessions', label), f'{label}.sessions')):
        session_label = f'{label}.sessions[{index}]'
        session = _closed(raw_session, {
            'id', 'task_id', 'status', 'retained', 'live_owner', 'contract_fingerprint', 'source_refs',
        }, session_label)
        owner = session.get('live_owner')
        if owner is not None:
            owner = _string(owner, f'{session_label}.live_owner')
        retained = _required(session, 'retained', session_label)
        if not isinstance(retained, bool):
            raise PlanValidationError(f'{session_label}.retained is invalid')
        refs = _v2_ref_list(
            _required(session, 'source_refs', session_label),
            f'{session_label}.source_refs',
            verify_files=verify_files and not native_mode,
        )
        fingerprint = _sha256(
            _required(session, 'contract_fingerprint', session_label),
            f'{session_label}.contract_fingerprint',
        )
        session_id = _string(_required(session, 'id', session_label), f'{session_label}.id')
        if components is None:
            if fingerprint != canonical_sha256({'session_id': session_id, 'source_refs': refs}):
                raise PlanValidationError(f'{session_label}.contract_fingerprint does not bind its sources')
        else:
            native_session = next(
                (row for row in components['sessions'] if row['session_id'] == session_id),
                None,
            )
            if (
                native_session is None
                or native_session['task_id'] != session.get('task_id')
                or fingerprint != canonical_sha256(native_session)
            ):
                raise PlanValidationError(
                    f'{session_label}.contract_fingerprint does not bind native Session components'
                )
        sessions.append({
            'id': session_id,
            'task_id': _string(_required(session, 'task_id', session_label), f'{session_label}.task_id'),
            'status': _string(_required(session, 'status', session_label), f'{session_label}.status'),
            'retained': retained,
            'live_owner': owner,
            'contract_fingerprint': fingerprint,
            'source_refs': refs,
        })
    sessions.sort(key=lambda row: row['id'])
    _unique([row['id'] for row in sessions], f'{label}.sessions')
    task_ids = {task['id'] for task in tasks}
    if any(session['task_id'] not in task_ids for session in sessions):
        raise PlanValidationError(f'{label}.sessions contains an unknown Task')
    if components is not None:
        session_owners = {session['id']: session['task_id'] for session in sessions}
        component_owners = {
            session['session_id']: session['task_id']
            for session in components['sessions']
        }
        if component_owners != session_owners:
            raise PlanValidationError(f'{label}.native_components Session inventory differs')
        if any(row['task_id'] not in task_ids for row in components['approvals']):
            raise PlanValidationError(f'{label}.native_components contains an unknown approval Task')
        if any(
            session_owners.get(row['session_id']) != row['task_id']
            for row in components['questions']
        ):
            raise PlanValidationError(f'{label}.native_components contains an unknown Question Session')
    executions = []
    for index, raw_execution in enumerate(_bounded_list(
        _required(raw, 'live_executions', label), f'{label}.live_executions',
    )):
        execution_label = f'{label}.live_executions[{index}]'
        execution = _closed(raw_execution, {'id', 'state'}, execution_label)
        executions.append({
            'id': _string(_required(execution, 'id', execution_label), f'{execution_label}.id'),
            'state': _string(_required(execution, 'state', execution_label), f'{execution_label}.state'),
        })
    executions.sort(key=lambda row: row['id'])
    _unique([row['id'] for row in executions], f'{label}.live_executions')
    result = {
        'schema': CANONICAL_V2_SNAPSHOT_SCHEMA,
        'workflow_id': workflow_id,
        'project_id': project_id,
        'expected_version': version,
        'execution_target': target,
        'execution_root': str(root),
        'tasks': tasks,
        'sessions': sessions,
        'live_executions': executions,
    }
    if components is not None:
        result['native_components'] = components
    return result


def _v2_graph_compatibility(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    runtime_refs: Sequence[Mapping[str, Any]],
) -> None:
    if set(source) != {'version', 'nodes', 'edges', 'node_groups', 'transition_groups'}:
        raise PlanValidationError('v2 source graph fields are unsupported')
    if canonical_sha256(source) == canonical_sha256(target):
        return
    if set(target) != set(source) or target.get('version') != source.get('version', 0) + 1:
        raise PlanValidationError('v2 target graph revision/fields are invalid')
    for key in ('nodes', 'edges', 'node_groups', 'transition_groups'):
        _bounded_list(source.get(key), f'source_graph.{key}')
        _bounded_list(target.get(key), f'target_graph.{key}')
    for name in ('nodes', 'edges', 'node_groups', 'transition_groups'):
        old_rows, new_rows = source[name], target[name]
        old_ids = [row.get('id') for row in old_rows if isinstance(row, dict)]
        new_ids = [row.get('id') for row in new_rows if isinstance(row, dict)]
        if len(old_ids) != len(old_rows) or len(new_ids) != len(new_rows):
            raise PlanValidationError(f'v2 {name} entries must be objects')
        if len(old_ids) != len(set(old_ids)) or len(new_ids) != len(set(new_ids)):
            raise PlanValidationError(f'v2 {name} identities are duplicated')
        old_by_id = {row['id']: row for row in old_rows}
        new_by_id = {row['id']: row for row in new_rows}
        added = set(new_by_id) - set(old_by_id)
        removed = set(old_by_id) - set(new_by_id)
        if removed:
            raise PlanValidationError(f'v2 target removes existing {name}')
        if name in {'nodes', 'node_groups'} and added:
            raise PlanValidationError(f'v2 target adds unsupported {name}')
        if name == 'transition_groups' and len(added) > 1:
            raise PlanValidationError('v2 target adds more than one transition group')
        if name == 'edges' and len(added) > 1:
            raise PlanValidationError('v2 target adds more than one edge')
        for identity in set(old_by_id) & set(new_by_id):
            before = dict(old_by_id[identity])
            after = dict(new_by_id[identity])
            if name == 'nodes' and before != after:
                before_script = before.pop('script_path', None)
                after_script = after.pop('script_path', None)
                qualified_paths = {
                    ref['path']
                    for ref in runtime_refs
                    if isinstance(ref.get('path'), str)
                    and isinstance(ref.get('mode'), int)
                    and not isinstance(ref.get('mode'), bool)
                    and ref['mode'] & 0o111
                }
                if (
                    before != after
                    or old_by_id[identity].get('kind') != 'script'
                    or new_by_id[identity].get('kind') != 'script'
                    or not isinstance(before_script, str)
                    or not before_script
                    or old_by_id[identity].get('script_path') == new_by_id[identity].get('script_path')
                    or not isinstance(after_script, str)
                    or not Path(after_script).is_absolute()
                    or after_script not in qualified_paths
                ):
                    raise PlanValidationError(
                        f'v2 target changes protected {name} semantics'
                    )
                continue
            for key in ('prompt_template', 'prompt'):
                before.pop(key, None)
                after.pop(key, None)
            if before != after:
                raise PlanValidationError(f'v2 target changes protected {name} semantics')
    if not any(
        canonical_sha256(source[name]) != canonical_sha256(target[name])
        for name in ('nodes', 'edges', 'node_groups', 'transition_groups')
    ):
        raise PlanValidationError('v2 graph revision contains no reviewed semantic delta')
    nodes = {row.get('id'): row for row in target['nodes']}
    keys = {row.get('key'): row for row in target['nodes']}
    new_groups = [row for row in target['transition_groups'] if row['id'] not in {
        item['id'] for item in source['transition_groups']
    }]
    new_edges = [row for row in target['edges'] if row['id'] not in {
        item['id'] for item in source['edges']
    }]
    if not new_groups and not new_edges:
        return
    if len(new_groups) != 1 or len(new_edges) != 1:
        raise PlanValidationError('v2 structural change must add exactly implement_replan')
    group, edge = new_groups[0], new_edges[0]
    fix_edges = [row for row in source['edges'] if row.get('key') == 'fix_replan']
    if (
        group.get('source_node_id') != keys.get('implement', {}).get('id')
        or group.get('transition_id') != 'implement_replan'
        or edge.get('key') != 'implement_replan'
        or edge.get('transition_group_id') != group.get('id')
        or edge.get('target_node_id') != keys.get('plan_revalidation', {}).get('id')
        or edge.get('requires_approval') is not False
        or edge.get('context_mode') != 'continue_session'
        or edge.get('context_source') != {'kind': 'selected_node', 'node_key': 'plan'}
        or keys.get('plan') is None
        or len(fix_edges) != 1
        or canonical_sha256(edge.get('parameters')) != canonical_sha256(fix_edges[0].get('parameters'))
        or edge.get('assignee_selection') != 'configured'
        or edge.get('thinking_selection') != 'configured'
        or not isinstance(edge.get('prompt_template'), str)
        or not edge['prompt_template'].strip()
    ):
        raise PlanValidationError('v2 additive route is not the approved implement_replan edge')
    if nodes.get(edge.get('target_node_id'), {}).get('key') != 'plan_revalidation':
        raise PlanValidationError('v2 implement_replan target identity is invalid')


def _v2_ingress_edge(graph: Mapping[str, Any], label: str) -> dict[str, Any]:
    nodes = {
        row.get('id', row.get('node_id')): row
        for row in graph.get('nodes', [])
        if isinstance(row, dict)
    }
    groups = {
        row.get('id', row.get('transition_group_id')): row
        for row in graph.get('transition_groups', [])
        if isinstance(row, dict)
    }
    candidates = []
    for edge in graph.get('edges', []):
        if not isinstance(edge, dict):
            continue
        group = groups.get(edge.get('transition_group_id'))
        if not isinstance(group, dict):
            continue
        source = nodes.get(group.get('source_node_id'))
        target = nodes.get(edge.get('target_node_id'))
        if (
            isinstance(source, dict)
            and isinstance(target, dict)
            and source.get('key') == 'backlog'
            and target.get('key') == 'plan'
        ):
            candidates.append(edge)
    if len(candidates) != 1:
        raise PlanValidationError(f'{label} must contain one unique Backlog-to-Plan ingress')
    return candidates[0]


def _v2_source_gate_check(
    source_graph: Mapping[str, Any],
    target_graph: Mapping[str, Any],
    admission: Mapping[str, Any],
    *,
    project_id: str,
    workflow_id: str,
    project_root: Path,
) -> None:
    from .delivery import SOURCE_ADMISSION_BEGIN, SOURCE_ADMISSION_END, source_admission_gate

    _v2_ingress_edge(source_graph, 'source graph')
    target_ingress = _v2_ingress_edge(target_graph, 'target graph')
    target_prompt = target_ingress.get('prompt_template', target_ingress.get('prompt'))
    if not isinstance(target_prompt, str):
        raise PlanValidationError('source gate ingress prompt is absent')
    gate = source_admission_gate(
        dict(admission),
        project_id=project_id,
        workflow_id=workflow_id,
        project_root=str(project_root),
    )
    if not target_prompt.startswith(f'{gate}\n\n'):
        raise PlanValidationError('target ingress does not contain the exact bound source admission gate')
    starts = sum(
        prompt.count(SOURCE_ADMISSION_BEGIN)
        for collection in ('nodes', 'edges', 'node_groups', 'transition_groups')
        for edge in target_graph.get(collection, [])
        if isinstance(edge, dict)
        for prompt in [edge.get('prompt_template'), edge.get('prompt')]
        if isinstance(prompt, str)
    )
    ends = sum(
        prompt.count(SOURCE_ADMISSION_END)
        for collection in ('nodes', 'edges', 'node_groups', 'transition_groups')
        for edge in target_graph.get(collection, [])
        if isinstance(edge, dict)
        for prompt in [edge.get('prompt_template'), edge.get('prompt')]
        if isinstance(prompt, str)
    )
    if starts != 1 or ends != 1:
        raise PlanValidationError('target graph must contain exactly one source admission frame')


def _v2_runtime_refs(value: Any, *, verify_sources: bool = True) -> list[dict[str, Any]]:
    refs = []
    for index, raw in enumerate(_bounded_list(value, 'runtime_refs')):
        label = f'runtime_refs[{index}]'
        ref = _closed(raw, {
            'path', 'sha256', 'root', 'relative_path', 'mode', 'byte_size', 'published_commit',
        }, label)
        root = _absolute_path(_required(ref, 'root', label), f'{label}.root')
        relative = Path(_string(_required(ref, 'relative_path', label), f'{label}.relative_path'))
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise PlanValidationError(f'{label}.relative_path is invalid')
        path = _absolute_path(_required(ref, 'path', label), f'{label}.path')
        if path != root / relative:
            raise PlanValidationError(f'{label}.path does not match its root-relative binding')
        mode = _required(ref, 'mode', label)
        size = _required(ref, 'byte_size', label)
        if not isinstance(mode, int) or isinstance(mode, bool) or mode < 0:
            raise PlanValidationError(f'{label}.mode is invalid')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > CANONICAL_V2_MAX_REFERENCE:
            raise PlanValidationError(f'{label}.byte_size is invalid')
        commit = _sha1(_required(ref, 'published_commit', label), f'{label}.published_commit')
        expected_digest = _sha256(_required(ref, 'sha256', label), f'{label}.sha256')
        if verify_sources:
            path, raw_bytes = _read_reference(
                {'path': str(path), 'sha256': expected_digest},
                label,
            )
            info = path.stat(follow_symlinks=False)
            if (
                stat.S_IMODE(info.st_mode) != mode
                or info.st_size != size
                or not (mode & 0o111)
            ):
                raise PlanValidationError(f'{label} mode/size is not qualified')
            if not root.is_dir() or root.is_symlink():
                raise PlanValidationError(f'{label}.root is unsafe')
            top = _git(root, 'rev-parse', '--show-toplevel').strip()
            head = _git(root, 'rev-parse', 'HEAD').strip()
            dirty = _git(root, 'status', '--porcelain=v1', '--untracked-files=all')
            if Path(top).resolve() != root.resolve() or head != commit or dirty:
                raise PlanValidationError(f'{label} is not at its clean published revision')
        refs.append({
            'path': str(path),
            'sha256': expected_digest,
            'root': str(root),
            'relative_path': relative.as_posix(),
            'mode': mode,
            'byte_size': size,
            'published_commit': commit,
        })
    refs.sort(key=lambda row: row['path'])
    _unique([row['path'] for row in refs], 'runtime_refs')
    if not refs:
        raise PlanValidationError('v2 plan requires at least one qualified runtime reference')
    return refs


def _v2_review(
    value: Any,
    *,
    workflow_id: str,
    project_id: str,
    source_graph_sha256: str,
    source_metadata_sha256: str,
    target_graph_sha256: str,
    target_metadata_sha256: str,
    snapshot: Mapping[str, Any],
    runtime_refs: Sequence[Mapping[str, Any]],
    source_admission_sha256: str | None,
    native_evidence_sha256: str | None,
    required_paths: set[str],
) -> None:
    path, raw = _read_reference(value, 'compatibility_review_ref')
    try:
        review = json.loads(raw.decode('utf-8'), object_pairs_hook=_duplicate_free)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlanValidationError('compatibility review is not valid JSON') from error
    if canonical_bytes(review) != raw:
        raise PlanValidationError('compatibility review is not canonical JSON')
    review = _closed(review, {
        'schema', 'workflow_id', 'project_id', 'source_graph_sha256', 'source_metadata_sha256',
        'target_graph_sha256', 'target_metadata_sha256', 'compatibility_snapshot_sha256',
        'runtime_refs_sha256', 'source_admission_sha256', 'native_evidence_sha256',
        'outcome', 'reviewer_session_id', 'dispositions',
    }, 'compatibility review')
    if native_evidence_sha256 is not None and 'native_evidence_sha256' not in review:
        raise PlanValidationError('native compatibility review omits native_evidence_sha256')
    if (
        native_evidence_sha256 is None
        and review.get('native_evidence_sha256') is not None
    ):
        raise PlanValidationError('legacy compatibility review cannot bind native evidence')
    expected = {
        'schema': CANONICAL_V2_REVIEW_SCHEMA,
        'workflow_id': workflow_id,
        'project_id': project_id,
        'source_graph_sha256': source_graph_sha256,
        'source_metadata_sha256': source_metadata_sha256,
        'target_graph_sha256': target_graph_sha256,
        'target_metadata_sha256': target_metadata_sha256,
        'compatibility_snapshot_sha256': canonical_sha256(snapshot),
        'runtime_refs_sha256': canonical_sha256(list(runtime_refs)),
        'source_admission_sha256': source_admission_sha256,
        'outcome': 'PASS',
    }
    if native_evidence_sha256 is not None:
        expected['native_evidence_sha256'] = native_evidence_sha256
    if any(review.get(key) != expected_value for key, expected_value in expected.items()):
        raise PlanValidationError('compatibility review does not bind the exact v2 plan inputs')
    _uuid(_required(review, 'reviewer_session_id', 'compatibility review'), 'reviewer_session_id')
    dispositions: dict[str, str] = {}
    for index, raw_disposition in enumerate(_bounded_list(
        _required(review, 'dispositions', 'compatibility review'), 'compatibility review.dispositions',
    )):
        label = f'compatibility review.dispositions[{index}]'
        disposition = _closed(raw_disposition, {'path', 'disposition', 'evidence'}, label)
        path_key = _string(_required(disposition, 'path', label), f'{label}.path')
        outcome = _string(_required(disposition, 'disposition', label), f'{label}.disposition')
        if outcome not in {'compatible', 'non-impact'} or path_key in dispositions:
            raise PlanValidationError('compatibility review disposition is unsupported or duplicated')
        if not _v2_ref_list(_required(disposition, 'evidence', label), f'{label}.evidence'):
            raise PlanValidationError('compatibility disposition requires evidence references')
        dispositions[path_key] = outcome
    if not required_paths.issubset(dispositions):
        raise PlanValidationError('compatibility review omits a changed semantic path')


def _validate_canonical_v2_plan(
    plan: LoadedPlan,
    *,
    verify_evidence: bool = True,
) -> dict[str, Any]:
    if len(plan.raw) > CANONICAL_V2_MAX_PLAN:
        raise PlanValidationError('v2 plan exceeds the bounded size')
    data = _closed(
        plan.value,
        {'schema', 'state_dir', 'project_root', 'kent', 'workflows', 'native_evidence'},
        'canonical v2 plan',
    )
    if data.get('schema') != CANONICAL_V2_SCHEMA:
        raise PlanValidationError('canonical v2 schema is unsupported')
    state_dir = _absolute_path(_required(data, 'state_dir', 'canonical v2 plan'), 'state_dir')
    project_root = _no_symlink_path(
        _absolute_path(_required(data, 'project_root', 'canonical v2 plan'), 'project_root'),
        'project_root',
    )
    kent = _closed(_required(data, 'kent', 'canonical v2 plan'), {'path', 'sha256'}, 'kent')
    kent_path = _kent_path(_required(kent, 'path', 'kent'), 'kent.path')
    kent_sha = _sha256(_required(kent, 'sha256', 'kent'), 'kent.sha256')
    _verify_executable(kent_path, kent_sha)
    native_descriptor = (
        _v2_native_descriptor(data['native_evidence'], 'native_evidence')
        if 'native_evidence' in data else None
    )
    raw_workflows = _bounded_list(_required(data, 'workflows', 'canonical v2 plan'), 'workflows')
    if len(raw_workflows) != 1:
        raise PlanValidationError('canonical v2 plan must bind exactly one existing Workflow')
    workflows = []
    for index, raw in enumerate(raw_workflows):
        label = f'workflows[{index}]'
        _reject_raw_protocol_fields(raw, label)
        item = dict(_closed(raw, {
            'workflow_id', 'project_id', 'intent', 'expected_version', 'source_graph_sha256',
            'source_metadata_sha256', 'target_graph', 'target_metadata', 'links', 'default',
            'anchors', 'compatibility_snapshot', 'runtime_refs', 'compatibility_review_ref',
            'source_admission',
        }, label))
        item['workflow_id'] = _uuid(_required(item, 'workflow_id', label), f'{label}.workflow_id')
        item['project_id'] = _native_project_id(_required(item, 'project_id', label), f'{label}.project_id')
        if native_descriptor is not None:
            item['_native_evidence_sha256'] = canonical_sha256(native_descriptor)
        intent = _string(_required(item, 'intent', label), f'{label}.intent')
        if intent not in {'graph-only', 'metadata-only', 'graph-and-metadata'}:
            raise PlanValidationError('canonical v2 intent is unsupported')
        item['intent'] = intent
        item['source_admission'] = _v2_source_admission(
            _required(item, 'source_admission', label),
            f'{label}.source_admission',
        )
        version = _required(item, 'expected_version', label)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise PlanValidationError('canonical v2 expected_version is invalid')
        item['expected_version'] = version
        item['source_graph_sha256'] = _sha256(
            _required(item, 'source_graph_sha256', label), f'{label}.source_graph_sha256',
        )
        item['source_metadata_sha256'] = _sha256(
            _required(item, 'source_metadata_sha256', label), f'{label}.source_metadata_sha256',
        )
        item['compatibility_snapshot'] = _v2_snapshot(
            _required(item, 'compatibility_snapshot', label), f'{label}.compatibility_snapshot',
            verify_files=verify_evidence,
            native_mode=native_descriptor is not None,
        )
        snapshot = item['compatibility_snapshot']
        if (
            snapshot['workflow_id'] != item['workflow_id']
            or snapshot['project_id'] != item['project_id']
            or snapshot['expected_version'] != version
            or Path(snapshot['execution_root']).resolve() != project_root.resolve()
        ):
            raise PlanValidationError('v2 compatibility snapshot identity/root drifted')
        if (
            item['source_admission'] is not None
            and item['source_admission']['primary_root'] != str(project_root)
        ):
            raise PlanValidationError('v2 source admission primary root differs from project root')
        links = []
        for link_index, raw_link in enumerate(_bounded_list(
            _required(item, 'links', label), f'{label}.links',
        )):
            link_label = f'{label}.links[{link_index}]'
            link = _closed(raw_link, {'project_id', 'workflow_id', 'is_default'}, link_label)
            parsed_link = {
                'project_id': _native_project_id(
                    _required(link, 'project_id', link_label), f'{link_label}.project_id',
                ),
                'workflow_id': _uuid(
                    _required(link, 'workflow_id', link_label), f'{link_label}.workflow_id',
                ),
                'is_default': _required(link, 'is_default', link_label),
            }
            if (
                parsed_link['project_id'] != item['project_id']
                or parsed_link['workflow_id'] != item['workflow_id']
                or not isinstance(parsed_link['is_default'], bool)
            ):
                raise PlanValidationError('v2 link identity/default is invalid')
            links.append(parsed_link)
        links.sort(key=lambda row: (row['project_id'], row['workflow_id']))
        if len(links) != 1 or links[0]['is_default'] != (_required(item, 'default', label) is not None):
            raise PlanValidationError('v2 plan must preserve one explicit existing project link')
        item['links'] = links
        default_value = item['default']
        item['default'] = _uuid(default_value, f'{label}.default') if default_value is not None else None
        if item['default'] != (item['workflow_id'] if links[0]['is_default'] else None):
            raise PlanValidationError('v2 default does not match its existing project link')
        anchors = []
        for anchor_index, raw_anchor in enumerate(_bounded_list(
            _required(item, 'anchors', label), f'{label}.anchors',
        )):
            anchor_label = f'{label}.anchors[{anchor_index}]'
            anchor = _closed(raw_anchor, {'id', 'kind'}, anchor_label)
            anchors.append({
                'id': _string(_required(anchor, 'id', anchor_label), f'{anchor_label}.id'),
                'kind': _string(_required(anchor, 'kind', anchor_label), f'{anchor_label}.kind'),
            })
        anchors.sort(key=lambda row: row['id'])
        _unique([row['id'] for row in anchors], f'{label}.anchors')
        item['anchors'] = anchors
        if intent in {'graph-only', 'graph-and-metadata'}:
            target_graph = dict(_closed(
                _required(item, 'target_graph', label),
                {'version', 'nodes', 'edges', 'node_groups', 'transition_groups'},
                f'{label}.target_graph',
            ))
            if target_graph.get('version') != version + 1:
                raise PlanValidationError('v2 target graph must advance exactly one version')
            item['target_graph'] = target_graph
        elif 'target_graph' in item:
            raise PlanValidationError('v2 metadata-only plan cannot carry a graph')
        if intent in {'metadata-only', 'graph-and-metadata'}:
            metadata = _closed(
                _required(item, 'target_metadata', label),
                {'name', 'description', 'execution_target'},
                f'{label}.target_metadata',
            )
            item['target_metadata'] = {
                'name': _string(_required(metadata, 'name', label), f'{label}.target_metadata.name'),
                'description': _string(
                    _required(metadata, 'description', label),
                    f'{label}.target_metadata.description',
                    nonempty=False,
                ),
                'execution_target': _plan_execution_target(
                    _required(metadata, 'execution_target', label),
                    f'{label}.target_metadata.execution_target',
                ),
            }
        elif 'target_metadata' in item:
            raise PlanValidationError('v2 graph-only plan cannot carry metadata')
        item['runtime_refs'] = _v2_runtime_refs(
            _required(item, 'runtime_refs', label),
            verify_sources=verify_evidence,
        )
        item['compatibility_review_ref'] = _closed(
            _required(item, 'compatibility_review_ref', label),
            {'path', 'sha256'},
            f'{label}.compatibility_review_ref',
        )
        if item['source_admission'] is None and intent != 'metadata-only':
            raise PlanValidationError('v2 graph-changing intent requires source_admission')
        workflows.append(item)
    item = workflows[0]
    return {
        'state_dir': state_dir,
        'project_root': project_root,
        'kent': kent_path,
        'kent_sha256': kent_sha,
        'native_evidence': native_descriptor,
        'workflows': workflows,
    }


def _v2_required_review_paths(
    source_graph: Mapping[str, Any],
    target_graph: Mapping[str, Any],
    source_metadata: Mapping[str, Any],
    target_metadata: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> set[str]:
    paths = {f"task:{task['id']}" for task in snapshot['tasks']}
    paths.update(f"session:{session['id']}" for session in snapshot['sessions'])
    if canonical_sha256(source_graph) != canonical_sha256(target_graph):
        for name in ('nodes', 'edges', 'node_groups', 'transition_groups'):
            before = {row['id']: row for row in source_graph[name]}
            after = {row['id']: row for row in target_graph[name]}
            additions = set(after) - set(before)
            if name == 'edges' and any(after[identity].get('key') == 'implement_replan' for identity in additions):
                paths.add('graph.added_edge:implement_replan')
            for identity in set(before) & set(after):
                if (
                    name == 'nodes'
                    and before[identity].get('script_path') != after[identity].get('script_path')
                ):
                    paths.add(f'graph.nodes:{identity}.script_path')
                for prompt_key in ('prompt_template', 'prompt'):
                    if before[identity].get(prompt_key) != after[identity].get(prompt_key):
                        paths.add(f'graph.{name}:{identity}.{prompt_key}')
    for key in set(source_metadata) | set(target_metadata):
        if source_metadata.get(key) != target_metadata.get(key):
            paths.add(f'metadata.{key}')
    return paths


def _validate_canonical_v2_target(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
    source: Mapping[str, Any],
    expected_workflow_id: str | None = None,
    expected_spec: Any = None,
    *,
    verify_evidence: bool = True,
) -> dict[str, Any]:
    if expected_workflow_id is not None and _uuid(
        expected_workflow_id, 'expected_workflow_id',
    ) != item['workflow_id']:
        raise PlanValidationError('compatibility plan Workflow UUID differs from the selector')
    if source['workflow_id'] != item['workflow_id'] or source['project_id'] != item['project_id']:
        raise EffectBlocked('v2 source Workflow/project identity drifted')
    if source['version'] != item['expected_version']:
        raise EffectBlocked('v2 source Workflow revision drifted')
    if canonical_sha256(source['graph']) != item['source_graph_sha256']:
        raise EffectBlocked('v2 source graph digest drifted')
    if canonical_sha256(source['metadata']) != item['source_metadata_sha256']:
        raise EffectBlocked('v2 source metadata digest drifted')
    if (
        canonical_sha256(source['links']) != canonical_sha256(item['links'])
        or source['default'] != item['default']
    ):
        raise EffectBlocked('v2 linked ownership/default state drifted')
    graph = item.get('target_graph', source['graph'])
    metadata = item.get('target_metadata', source['metadata'])
    _v2_graph_compatibility(source['graph'], graph, item['runtime_refs'])
    source_admission = item['source_admission']
    if source_admission is None:
        if (
            canonical_sha256(source['graph']) != canonical_sha256(graph)
            or metadata['execution_target'] != source['metadata']['execution_target']
        ):
            raise PlanValidationError(
                'source_admission null is limited to metadata-only name/description changes'
            )
    else:
        if (
            metadata['execution_target'] != source['metadata']['execution_target']
            and item['intent'] != 'graph-and-metadata'
        ):
            raise PlanValidationError(
                'execution-target changes require graph-and-metadata with source admission'
            )
        _v2_source_gate_check(
            source['graph'],
            graph,
            source_admission,
            project_id=item['project_id'],
            workflow_id=item['workflow_id'],
            project_root=parsed['project_root'],
        )
    source_semantics = {
        key: source['graph'][key] for key in ('nodes', 'edges', 'node_groups', 'transition_groups')
    }
    target_semantics = {
        key: graph[key] for key in ('nodes', 'edges', 'node_groups', 'transition_groups')
    }
    if (
        canonical_sha256(source_semantics) == canonical_sha256(target_semantics)
        and canonical_sha256(source['metadata']) == canonical_sha256(metadata)
    ):
        raise PlanValidationError('v2 plan has no reviewed semantic change')
    for node in graph['nodes']:
        if not isinstance(node, dict) or node.get('kind') != 'script':
            continue
        script_path = node.get('script_path')
        if not isinstance(script_path, str) or not script_path:
            raise PlanValidationError('v2 script node is missing its qualified path')
        candidate = Path(script_path)
        if not candidate.is_absolute():
            candidate = parsed['project_root'] / candidate
        candidate = candidate.resolve()
        if not any(
            Path(ref['path']).resolve() == candidate
            or (
                Path(ref['root']).resolve() == parsed['project_root'].resolve()
                and (parsed['project_root'] / ref['relative_path']).resolve() == candidate
            )
            for ref in item['runtime_refs']
        ):
            raise PlanValidationError('v2 script node has no matching runtime qualification')
    kinds = {
        node.get('id', node.get('node_id')): node.get('kind')
        for node in graph['nodes'] if isinstance(node, dict)
    }
    if any(kinds.get(anchor['id']) != anchor['kind'] for anchor in item['anchors']):
        raise PlanValidationError('v2 target graph does not preserve its reviewed anchors')
    if verify_evidence:
        _v2_review(
            item['compatibility_review_ref'],
            workflow_id=item['workflow_id'],
            project_id=item['project_id'],
            source_graph_sha256=item['source_graph_sha256'],
            source_metadata_sha256=item['source_metadata_sha256'],
            target_graph_sha256=canonical_sha256(graph),
            target_metadata_sha256=canonical_sha256(metadata),
            snapshot=item['compatibility_snapshot'],
            runtime_refs=item['runtime_refs'],
            source_admission_sha256=(
                canonical_sha256(source_admission)
                if source_admission is not None else None
            ),
            native_evidence_sha256=item.get('_native_evidence_sha256'),
            required_paths=_v2_required_review_paths(
                source['graph'], graph, source['metadata'], metadata, item['compatibility_snapshot'],
            ),
        )
    if expected_spec is not None:
        from .graph import graph_matches_spec

        if (
            expected_spec.name != metadata['name']
            or expected_spec.description != metadata['description']
            or expected_spec.execution_target != metadata['execution_target']
            or not graph_matches_spec(expected_spec, {
                'workflow_id': item['workflow_id'],
                'expected_version': graph['version'],
                'graph': {key: graph[key] for key in ('nodes', 'edges', 'node_groups', 'transition_groups')},
            })
        ):
            raise PlanValidationError('v2 target does not match the explicit WorkflowSpec')
    return {'graph': graph, 'metadata': metadata}


def _canonical_metadata(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EffectBlocked('Workflow summary is malformed')
    try:
        workflow_id = _string(value.get('id'), 'Workflow summary identity')
    except PlanValidationError as error:
        raise EffectBlocked('Workflow inspect identity is malformed') from error
    source = _summary_source(value, workflow_id)
    return {
        'name': _string(source['name'], 'Workflow name'),
        'description': _string(source['description'], 'Workflow description', nonempty=False),
        'execution_target': _live_execution_target(source['execution_target_policy'], 'Workflow'),
    }

def _canonical_graph(value: Mapping[str, Any], version: int) -> dict[str, Any]:
    body = value.get('graph')
    if not isinstance(body, dict) or value.get('expected_version', version) != version:
        raise EffectBlocked('canonical graph readback revision is malformed')
    graph = {'version': version}
    for key in ('nodes', 'edges', 'node_groups', 'transition_groups'):
        rows = body.get(key)
        if not isinstance(rows, list):
            raise EffectBlocked(f'canonical graph {key} readback is malformed')
        graph[key] = rows
    return graph

def _canonical_read(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    include_raw_task_details: bool = False,
) -> dict[str, Any]:
    kent = parsed['kent']
    root = parsed['project_root']
    wid = item['workflow_id']
    project_id = item['project_id']
    project_rows = _project_rows(kent, root)
    projects = [row for row in project_rows if row['id'] == project_id]
    if len(projects) != 1 or (root / Path(projects[0]['path']).expanduser()).resolve() != root.resolve():
        raise EffectBlocked('canonical project identity/root drifted')
    pages = _kent_pages(kent, ['workflow', 'list', '--project', project_id], cwd=root)
    links, default, listed_version = _workflow_link_state(_page_rows(pages, ('items', 'workflows')), project_id, wid)
    if listed_version is None:
        raise EffectBlocked('canonical Workflow is absent from the project')
    inspect = _kent_json(kent, ['workflow', 'inspect', wid, '--summary', '--json'], cwd=root)
    summary = _workflow_summary(inspect, wid)
    if summary['revision'] != listed_version:
        raise EffectBlocked('canonical list/inspect revisions disagree')
    graph_raw = _kent_json(
        kent,
        ['workflow', 'graph', 'inspect', wid, '--json'],
        cwd=root,
        output_limit=CANONICAL_GRAPH_MAX_OUTPUT,
    )
    graph = _canonical_graph(graph_raw, listed_version)
    validation = _kent_json(kent, ['workflow', 'validate', wid, '--json'], cwd=root)
    if validation.get('valid') is not True:
        raise EffectBlocked('canonical Workflow validation failed')
    task_pages = _kent_pages(kent, ['task', 'list', '--project', project_id, '--workflow', wid], cwd=root)
    task_rows = _page_rows(task_pages, ('items', 'tasks'))
    tasks = sorted((_task_state(row) for row in task_rows), key=lambda row: row['id'])
    details: list[dict[str, Any]] = []
    raw_details: list[dict[str, Any]] = []
    anchor_ids: set[str] = set()
    for task in tasks:
        detail = _kent_json(kent, ['task', 'show', task['id'], '--project', project_id, '--json'], cwd=root)
        details.append(_task_state(detail, task['id']))
        if include_raw_task_details:
            raw_details.append(detail)
        rows = detail.get('terminal_anchors', detail.get('current_nodes', []))
        if not isinstance(rows, list):
            raise EffectBlocked('canonical terminal anchor readback is malformed')
        for row in rows:
            node_id = row if isinstance(row, str) else row.get('id', row.get('node_id'))
            if isinstance(node_id, str):
                anchor_ids.add(node_id)
    details.sort(key=lambda row: row['id'])
    node_kinds = {node.get('id', node.get('node_id')): node.get('kind') for node in graph['nodes'] if isinstance(node,
            dict)}
    anchors = sorted(({'id': node_id, 'kind': node_kinds.get(node_id)} for node_id in anchor_ids), key=lambda anchor:
            anchor['id'])
    if any((not isinstance(anchor['kind'], str) for anchor in anchors)):
        raise EffectBlocked('canonical terminal anchor is absent from the graph')
    result = {'workflow_id': wid, 'project_id': project_id, 'version': listed_version, 'metadata':
            _canonical_metadata(inspect), 'graph': graph, 'tasks': tasks, 'task_details': details, 'terminal_anchors':
            anchors, 'links': links, 'default': default, 'valid': True}
    if include_raw_task_details:
        result['raw_task_details'] = sorted(
            raw_details,
            key=lambda row: _row_id(row, 'id', 'task_id'),
        )
    return result

def _canonical_v2_pending_approvals(
    detail: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    approval_signaled = False
    explicit_pending: list[bool] = []
    for status in (detail.get('status'), summary.get('status')):
        if isinstance(status, str):
            approval_signaled = approval_signaled or status == 'waiting_approval'
        elif isinstance(status, Mapping):
            approval_signaled = approval_signaled or any(
                status.get(key) == 'waiting_approval'
                for key in ('kind', 'status', 'native_state')
            )
            if 'approval_pending' in status:
                value = status['approval_pending']
                if not isinstance(value, bool):
                    raise EffectBlocked('v2 Task approval-pending flag is malformed')
                approval_signaled = approval_signaled or value
                explicit_pending.append(value)
            if 'attention_types' in status:
                attention = status['attention_types']
                if not isinstance(attention, list) or len(attention) > MAX_LIST or any(
                    not isinstance(value, str) for value in attention
                ):
                    raise EffectBlocked('v2 Task approval attention inventory is malformed')
                approval_signaled = approval_signaled or 'approval' in attention
        elif status is not None:
            raise EffectBlocked('v2 Task approval status is malformed')
    for source in (detail, summary):
        if 'attention_types' in source:
            attention = source['attention_types']
            if not isinstance(attention, list) or len(attention) > MAX_LIST or any(
                not isinstance(value, str) for value in attention
            ):
                raise EffectBlocked('v2 Task approval attention inventory is malformed')
            approval_signaled = approval_signaled or 'approval' in attention

    for source in (detail, summary):
        if 'approval_pending' in source:
            value = source['approval_pending']
            if not isinstance(value, bool):
                raise EffectBlocked('v2 Task approval-pending flag is malformed')
            explicit_pending.append(value)
            approval_signaled = approval_signaled or value
    if len(set(explicit_pending)) > 1:
        raise EffectBlocked('v2 Task approval-pending flags disagree')

    present = [
        source['pending_approvals']
        for source in (detail, summary)
        if 'pending_approvals' in source
    ]
    if len(present) == 2 and canonical_sha256(present[0]) != canonical_sha256(present[1]):
        raise EffectBlocked('v2 Task pending approval payloads disagree')
    if present:
        rows = present[0]
        if not isinstance(rows, list) or len(rows) > MAX_LIST:
            raise EffectBlocked('v2 pending approval inventory is malformed')
        if approval_signaled and not rows:
            raise EffectBlocked('v2 pending approval inventory is missing or empty')
        pending = []
        for row in rows:
            if not isinstance(row, dict):
                raise EffectBlocked('v2 pending approval record is malformed')
            pending.append({
                'id': _row_id(row, 'id', 'approval_id'),
                'payload_sha256': canonical_sha256(row),
            })
        pending.sort(key=lambda row: row['id'])
        if len({row['id'] for row in pending}) != len(pending):
            raise EffectBlocked('v2 pending approval identities are duplicated')
        if explicit_pending and not explicit_pending[0] and pending:
            raise EffectBlocked('v2 Task approval flag conflicts with its pending payload')
        return pending
    if approval_signaled:
        raise EffectBlocked('v2 pending approval inventory is unavailable')
    if explicit_pending and not explicit_pending[0]:
        return []
    return None


def _canonical_v2_approval_signaled(
    detail: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> bool:
    signaled = False
    pending_flags: list[bool] = []
    for source in (detail, summary):
        for status in (source.get('status'),):
            if isinstance(status, str):
                signaled = signaled or status == 'waiting_approval'
            elif isinstance(status, Mapping):
                signaled = signaled or any(
                    status.get(key) == 'waiting_approval'
                    for key in ('kind', 'status', 'native_state')
                )
                flag = status.get('approval_pending')
                if flag is not None:
                    if not isinstance(flag, bool):
                        raise EffectBlocked('v2 Task approval-pending flag is malformed')
                    pending_flags.append(flag)
                    signaled = signaled or flag
                attention = status.get('attention_types')
                if attention is not None:
                    if not isinstance(attention, list) or len(attention) > MAX_LIST or any(
                        not isinstance(row, str) for row in attention
                    ):
                        raise EffectBlocked('v2 Task approval attention inventory is malformed')
                    signaled = signaled or 'approval' in attention
        flag = source.get('approval_pending')
        if flag is not None:
            if not isinstance(flag, bool):
                raise EffectBlocked('v2 Task approval-pending flag is malformed')
            pending_flags.append(flag)
            signaled = signaled or flag
        attention = source.get('attention_types')
        if attention is not None:
            if not isinstance(attention, list) or len(attention) > MAX_LIST or any(
                not isinstance(row, str) for row in attention
            ):
                raise EffectBlocked('v2 Task approval attention inventory is malformed')
            signaled = signaled or 'approval' in attention
    if len(set(pending_flags)) > 1:
        raise EffectBlocked('v2 Task approval-pending flags disagree')
    if pending_flags and not pending_flags[0] and signaled:
        raise EffectBlocked('v2 Task approval flag conflicts with its pending state')
    return signaled


def _canonical_v2_current_scripts(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    scripts = detail.get('current_scripts')
    if not isinstance(scripts, list) or len(scripts) > MAX_LIST:
        raise EffectBlocked('v2 current Script inventory is absent or malformed')
    for row in scripts:
        if not isinstance(row, dict):
            raise EffectBlocked('v2 current Script record is malformed')
        current_node = row.get('current_node')
        if (
            not isinstance(row.get('path'), str) or not row['path']
            or not isinstance(current_node, Mapping)
            or not isinstance(current_node.get('node_id'), str)
            or not current_node['node_id']
            or (
                'branch' in current_node
                and current_node['branch'] is not None
                and not isinstance(current_node['branch'], str)
            )
        ):
            raise EffectBlocked('v2 current Script record is malformed')
    if scripts:
        raise EffectBlocked('v2 active native Script execution prevents reconciliation')
    return scripts

def _canonical_v2_live_executions(
    detail: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> list[dict[str, str]]:
    present = [
        source['live_executions']
        for source in (detail, summary)
        if 'live_executions' in source
    ]
    if len(present) == 2 and canonical_sha256(present[0]) != canonical_sha256(present[1]):
        raise EffectBlocked('v2 live execution inventories disagree')
    if not present:
        return []
    executions = present[0]
    if not isinstance(executions, list) or len(executions) > MAX_LIST:
        raise EffectBlocked('v2 live execution inventory is malformed')
    if executions:
        raise EffectBlocked('v2 live native execution prevents reconciliation')
    return []

def _canonical_v2_unknown_approval_is_nonimpact(
    item: Mapping[str, Any],
    task_id: str,
) -> bool:
    try:
        _, raw = _read_reference(item['compatibility_review_ref'], 'compatibility_review_ref')
        review = json.loads(raw.decode('utf-8'), object_pairs_hook=_duplicate_free)
        if (
            not isinstance(review, dict)
            or canonical_bytes(review) != raw
            or review.get('schema') != CANONICAL_V2_REVIEW_SCHEMA
            or review.get('workflow_id') != item['workflow_id']
            or review.get('project_id') != item['project_id']
            or review.get('compatibility_snapshot_sha256')
            != canonical_sha256(item['compatibility_snapshot'])
        ):
            return False
        path_key = f'task:{task_id}.pending_decisions'
        for disposition in review.get('dispositions', []):
            if not isinstance(disposition, dict) or disposition.get('path') != path_key:
                continue
            if disposition.get('disposition') != 'non-impact':
                return False
            return bool(_v2_ref_list(disposition.get('evidence'), path_key))
    except (OSError, PlanValidationError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return False

def _canonical_v2_inventory_matches(
    item: Mapping[str, Any],
    live_snapshot: Mapping[str, Any],
    *,
    expected_execution_target: str | None = None,
) -> bool:
    planned = item['compatibility_snapshot']
    policy = planned['execution_target'] if expected_execution_target is None else expected_execution_target
    if live_snapshot.get('execution_target') != policy:
        return False
    if any(
        live_snapshot.get(key) != planned.get(key)
        for key in ('schema', 'workflow_id', 'project_id', 'execution_root')
    ):
        return False
    planned_tasks = {row['id']: row for row in planned['tasks']}
    observed_tasks = {
        row['id']: row
        for row in live_snapshot.get('tasks', [])
        if isinstance(row, dict) and isinstance(row.get('id'), str)
    }
    if set(planned_tasks) != set(observed_tasks):
        return False
    task_keys = ('id', 'status', 'native_state', 'terminal', 'current_nodes')
    for task_id, expected in planned_tasks.items():
        actual = observed_tasks[task_id]
        if any(actual.get(key) != expected.get(key) for key in task_keys):
            return False
        actual_pending = actual.get('pending_decisions')
        if actual_pending is None:
            # Unknown approval state remains explicit. The reader permits it only
            # for terminal or Backlog Tasks, and only with a reviewed non-impact
            # disposition for this exact Task fact.
            if (
                expected['pending_decisions']
                or not _canonical_v2_unknown_approval_is_nonimpact(item, task_id)
            ):
                return False
        elif canonical_sha256(actual_pending) != canonical_sha256(expected['pending_decisions']):
            return False
    planned_sessions = {row['id']: row for row in planned['sessions']}
    observed_sessions = {
        row['id']: row
        for row in live_snapshot.get('sessions', [])
        if isinstance(row, dict) and isinstance(row.get('id'), str)
    }
    if set(planned_sessions) != set(observed_sessions):
        return False
    session_keys = ('id', 'task_id', 'status', 'retained', 'live_owner')
    for session_id, expected in planned_sessions.items():
        actual = observed_sessions[session_id]
        if any(actual.get(key) != expected.get(key) for key in session_keys):
            return False
    if ('native_components' in planned) != ('native_components' in live_snapshot):
        return False
    if 'native_components' in planned and canonical_sha256(
        live_snapshot.get('native_components')
    ) != canonical_sha256(planned['native_components']):
        return False
    return canonical_sha256(live_snapshot.get('live_executions')) == canonical_sha256(
        planned['live_executions']
    )

def _canonical_gate(item: Mapping[str, Any], snapshot: Mapping[str, Any], expected_version: int) -> None:
    if snapshot.get('workflow_id') != item['workflow_id']:
        raise EffectBlocked('canonical Workflow identity drifted')
    if snapshot.get('project_id') != item['project_id']:
        raise EffectBlocked('canonical project identity drifted')
    if snapshot.get('version') != expected_version or snapshot.get('valid') is not True:
        raise EffectBlocked('canonical Workflow revision/validation drifted')
    expected_tasks = [{'id': task['id'], 'status': task['status'], 'terminal': True, 'current_node': None,
            'approval_pending': False} for task in item['terminal_tasks']]
    for key in ('tasks', 'task_details'):
        if canonical_sha256(snapshot.get(key)) != canonical_sha256(expected_tasks):
            raise EffectBlocked(f'canonical {key} inventory drifted')
    for key in ('terminal_anchors', 'links', 'default'):
        if canonical_sha256(snapshot.get(key)) != canonical_sha256(item[key]):
            raise EffectBlocked(f'canonical {key} drifted')
    node_kinds = {node.get('id', node.get('node_id')): node.get('kind') for node in snapshot['graph']['nodes'] if
            isinstance(node, dict)}
    if any((node_kinds.get(anchor['id']) != anchor['kind'] for anchor in item['terminal_anchors'])):
        raise EffectBlocked('canonical graph lost a terminal anchor')

def _graph_document(workflow_id: str, expected_version: int, graph: Mapping[str, Any]) -> dict[str, Any]:
    return {'workflow_id': workflow_id, 'expected_version': expected_version, 'graph': {key: graph[key] for key in
            ('nodes', 'edges', 'node_groups', 'transition_groups')}}

def _canonical_stage(parsed: Mapping[str, Any], item: Mapping[str, Any], name: str, before: Mapping[str, Any], target:
        Mapping[str, Any]) -> dict[str, Any]:
    if name == 'graph':
        document = _graph_document(item['workflow_id'], before['version'], target['graph'])
        command = [str(parsed['kent']), 'workflow', 'graph', 'apply', '-', '--confirm', '--json']
        stdin = canonical_bytes(document)
    else:
        metadata = target['metadata']
        command = [str(parsed['kent']), 'workflow', 'update', item['workflow_id'], '--name', metadata['name'],
                '--description', metadata['description'], '--execution-target', metadata['execution_target'], '--json']
        stdin = None
    return {'name': name, 'status': f'{name}_verified', 'before': dict(before), 'after': dict(target), 'command':
            command, 'stdin': stdin}

def _canonical_progress(parsed: Mapping[str, Any], item: Mapping[str, Any], prepared: Mapping[str, Any]) -> \
        list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    current = dict(prepared)
    if item['intent'] in {'graph-only', 'graph-and-metadata'}:
        target = {**current, 'version': item['graph']['version'], 'graph': item['graph']}
        stages.append(_canonical_stage(parsed, item, 'graph', current, target))
        current = target
    if item['intent'] in {'metadata-only', 'graph-and-metadata'}:
        target = {**current, 'metadata': item['metadata']}
        stages.append(_canonical_stage(parsed, item, 'metadata', current, target))
    return stages

def _status_snapshot(status: str, prepared: Mapping[str, Any], stages: Sequence[Mapping[str, Any]]) -> Mapping[str,
        Any]:
    if status == 'prepared':
        return prepared
    if status == 'verified':
        return stages[-1]['after'] if stages else prepared
    for stage in stages:
        if status == stage['status']:
            return stage['after']
    raise JournalError(f'canonical member status is invalid: {status!r}')

def _canonical_d9_check(dependency: Mapping[str, Any] | None) -> None:
    if dependency is None:
        return
    path = dependency['path']
    if path.is_symlink() or not path.is_file():
        raise EffectBlocked('canonical D9 journal is absent or unsafe')
    raw = path.read_bytes()
    if sha256_bytes(raw) != dependency['sha256']:
        raise EffectBlocked('canonical D9 journal digest drifted')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_duplicate_free)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EffectBlocked('canonical D9 journal is invalid') from error
    if value.get('operation') != dependency['operation'] or value.get('phase') != 'complete':
        raise EffectBlocked('canonical D9 dependency is not complete')
    rows = {row.get('workflow_id'): row.get('status') for row in value.get('members', []) if isinstance(row, dict)}
    if any((rows.get(member) != 'verified' for member in dependency['members'])):
        raise EffectBlocked('canonical D9 member is not verified')

def _canonical_assert_expected(parsed: Mapping[str, Any], item: Mapping[str, Any], expected: Mapping[str, Any]) -> \
        dict[str, Any]:
    live = _canonical_read(parsed, item)
    _canonical_gate(item, live, expected['version'])
    if canonical_sha256(live) != canonical_sha256(expected):
        raise EffectBlocked(f"canonical complete snapshot drifted for {item['workflow_id']}")
    return live

def _canonical_v2_read(parsed: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    native_mode = isinstance(parsed.get('native_evidence'), Mapping)
    live = _canonical_read(parsed, item, include_raw_task_details=True)
    list_facts = [
        {key: task[key] for key in ('id', 'status', 'terminal')}
        for task in live['tasks']
    ]
    detail_facts = [
        {key: task[key] for key in ('id', 'status', 'terminal')}
        for task in live['task_details']
    ]
    if canonical_sha256(list_facts) != canonical_sha256(detail_facts):
        raise EffectBlocked('native Task list/detail inventories disagree')
    kent = parsed['kent']
    root = parsed['project_root']
    raw_details = {
        _row_id(row, 'id', 'task_id'): row for row in live.pop('raw_task_details')
    }
    all_links: list[dict[str, Any]] = []
    for project in _project_rows(kent, root):
        project_id = _native_project_id(project['id'], 'native project identity')
        pages = _kent_pages(kent, ['workflow', 'list', '--project', project_id], cwd=root)
        links, _, _ = _workflow_link_state(
            _page_rows(pages, ('items', 'workflows')),
            project_id,
            item['workflow_id'],
        )
        all_links.extend(links)
    if canonical_sha256(all_links) != canonical_sha256(item['links']):
        raise EffectBlocked('v2 Workflow linked-project ownership changed')
    live['all_links'] = all_links
    live['project_path'] = str(root)
    expected_sessions = {session['id']: session for session in item['compatibility_snapshot']['sessions']}
    tasks: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    live_executions: list[dict[str, str]] = []
    graph_node_keys = {
        row.get('id', row.get('node_id')): row.get('key')
        for row in live['graph']['nodes'] if isinstance(row, dict)
    }
    for task in live['tasks']:
        detail = raw_details[task['id']]
        normalized = _task_state(detail, task['id'])
        base_detail = next(row for row in live['task_details'] if row['id'] == task['id'])
        if canonical_sha256(normalized) != canonical_sha256(base_detail):
            raise EffectBlocked('Task changed during v2 native inventory readback')
        summary = detail.get('summary') if isinstance(detail.get('summary'), dict) else {}
        status_value = detail.get('status', summary.get('status'))
        native_state = status_value.get('native_state') if isinstance(status_value, dict) else None
        pending = [] if native_mode else _canonical_v2_pending_approvals(detail, summary)
        current_nodes = detail.get('current_nodes', summary.get('current_nodes', []))
        if not isinstance(current_nodes, list):
            raise EffectBlocked('v2 Task current-node inventory is malformed')
        normalized_nodes = []
        for row in current_nodes:
            node_id = row if isinstance(row, str) else (
                row.get('node_id', row.get('id')) if isinstance(row, dict) else None
            )
            if not isinstance(node_id, str) or not node_id:
                raise EffectBlocked('v2 Task current-node identity is malformed')
            normalized_nodes.append(node_id)
        normalized_nodes.sort()
        if len(set(normalized_nodes)) != len(normalized_nodes):
            raise EffectBlocked('v2 Task current-node identities are duplicated')
        if not normalized_nodes and not normalized['terminal']:
            current = detail.get('current_node', summary.get('current_node'))
            if isinstance(current, str) and current:
                normalized_nodes.append(current)
        is_backlog = bool(normalized_nodes) and all(
            graph_node_keys.get(node_id) == 'backlog' for node_id in normalized_nodes
        )
        if not native_mode and pending is None and not normalized['terminal'] and not is_backlog:
            raise EffectBlocked('v2 pending approval inventory is unavailable for an active Task')
        _canonical_v2_current_scripts(detail)
        native_executions = _canonical_v2_live_executions(detail, summary)
        tasks.append({
            'id': task['id'],
            'status': normalized['status'],
            'native_state': native_state,
            'terminal': normalized['terminal'],
            'current_nodes': normalized_nodes,
            'pending_decisions': pending,
        })
        live_rows = detail.get('live_sessions', [])
        if not isinstance(live_rows, list) or len(live_rows) > MAX_LIST:
            raise EffectBlocked('v2 live Session inventory is malformed')
        live_ids: set[str] = set()
        for row in live_rows:
            if isinstance(row, str):
                live_ids.add(row)
            elif isinstance(row, dict):
                live_ids.add(_row_id(row, 'id', 'session_id'))
            else:
                raise EffectBlocked('v2 live Session identity is malformed')
        task_sessions = []
        if not (native_mode and normalized['terminal']):
            pages = _kent_pages(
                kent,
                ['task', 'sessions', task['id'], '--project', item['project_id']],
                cwd=root,
            )
            for row in _page_rows(pages, ('items', 'sessions')):
                native = _session_cli_state(row, task['id'], live_ids)
                planned = expected_sessions.get(native['id'])
                if planned is not None and planned['task_id'] != task['id']:
                    raise EffectBlocked('v2 Session Task association drifted')
                task_sessions.append(native)
        retained_count = detail.get('retained_session_count')
        if (
            retained_count is not None
            and not (native_mode and normalized['terminal'])
            and retained_count != len(task_sessions)
        ):
            raise EffectBlocked('v2 retained Session pagination is incomplete')
        sessions.extend(task_sessions)
        live_executions.extend(native_executions)
    tasks.sort(key=lambda row: row['id'])
    sessions.sort(key=lambda row: row['id'])
    live_executions.sort(key=lambda row: row['id'])
    if (
        len({row['id'] for row in sessions}) != len(sessions)
        or len({row['id'] for row in live_executions}) != len(live_executions)
    ):
        raise EffectBlocked('v2 native Session/execution identities are duplicated')
    native_components = None
    if native_mode:
        native_components, sessions = _native_observe(
            parsed,
            item,
            live,
            tasks,
            raw_details,
            sessions,
        )
    live['compatibility_snapshot'] = {
        'schema': CANONICAL_V2_SNAPSHOT_SCHEMA,
        'workflow_id': live['workflow_id'],
        'project_id': live['project_id'],
        'expected_version': live['version'],
        'execution_target': live['metadata']['execution_target'],
        'execution_root': str(root),
        'tasks': tasks,
        'sessions': sessions,
        'live_executions': live_executions,
    }
    if native_components is not None:
        live['compatibility_snapshot']['native_components'] = native_components
    return live

def _canonical_v2_owned(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in (
        'workflow_id', 'project_id', 'version', 'metadata', 'graph', 'links', 'default', 'valid',
    )}


def _canonical_v2_owned_read(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
) -> dict[str, Any]:
    kent = parsed['kent']
    root = parsed['project_root']
    project_id = item['project_id']
    workflow_id = item['workflow_id']
    projects = [
        row for row in _project_rows(kent, root)
        if row['id'] == project_id
    ]
    if len(projects) != 1 or (root / Path(projects[0]['path']).expanduser()).resolve() != root.resolve():
        raise EffectBlocked('canonical project identity/root drifted during owned readback')
    pages = _kent_pages(kent, ['workflow', 'list', '--project', project_id], cwd=root)
    links, default, listed_version = _workflow_link_state(
        _page_rows(pages, ('items', 'workflows')),
        project_id,
        workflow_id,
    )
    if listed_version is None:
        raise EffectBlocked('canonical Workflow is absent during owned readback')
    inspect = _kent_json(kent, ['workflow', 'inspect', workflow_id, '--summary', '--json'], cwd=root)
    summary = _workflow_summary(inspect, workflow_id)
    if summary['revision'] != listed_version:
        raise EffectBlocked('canonical list/inspect revisions disagree during owned readback')
    graph_raw = _kent_json(
        kent,
        ['workflow', 'graph', 'inspect', workflow_id, '--json'],
        cwd=root,
        output_limit=CANONICAL_GRAPH_MAX_OUTPUT,
    )
    graph = _canonical_graph(graph_raw, listed_version)
    validation = _kent_json(kent, ['workflow', 'validate', workflow_id, '--json'], cwd=root)
    if validation.get('valid') is not True:
        raise EffectBlocked('canonical Workflow validation failed during owned readback')
    return {
        'workflow_id': workflow_id,
        'project_id': project_id,
        'version': listed_version,
        'metadata': _canonical_metadata(inspect),
        'graph': graph,
        'links': links,
        'default': default,
        'valid': True,
    }


def _canonical_v2_assert_owned(parsed: Mapping[str, Any], item: Mapping[str, Any],
        expected: Mapping[str, Any]) -> dict[str, Any]:
    native_mode = isinstance(parsed.get('native_evidence'), Mapping)
    live = (
        _canonical_v2_owned_read(parsed, item)
        if native_mode else _canonical_v2_read(parsed, item)
    )
    _canonical_v2_gate(
        item, live, expected['version'], compare_inventory=False,
        expected_execution_target=expected['metadata']['execution_target'],
    )
    if canonical_sha256(_canonical_v2_owned(live)) != canonical_sha256(_canonical_v2_owned(expected)):
        raise EffectBlocked(f"v2 effect-owned Workflow postimage drifted for {item['workflow_id']}")
    return live

def _canonical_v2_gate(item: Mapping[str, Any], snapshot: Mapping[str, Any], expected_version: int,
        *, compare_inventory: bool=True, expected_execution_target: str | None=None) -> None:
    policy = (
        item['compatibility_snapshot']['execution_target']
        if expected_execution_target is None else expected_execution_target
    )
    if (snapshot.get('workflow_id') != item['workflow_id'] or snapshot.get('project_id') != item['project_id'] or
            snapshot.get('version') != expected_version or snapshot.get('valid') is not True):
        raise EffectBlocked('v2 Workflow identity/revision/validation drifted')
    if canonical_sha256(snapshot.get('links')) != canonical_sha256(item['links']) or snapshot.get('default') != \
            item['default']:
        raise EffectBlocked('v2 existing project link/default ownership drifted')
    graph = snapshot.get('graph')
    if not isinstance(graph, dict):
        raise EffectBlocked('v2 Workflow graph readback is malformed')
    node_kinds = {node.get('id', node.get('node_id')): node.get('kind') for node in graph['nodes']
            if isinstance(node, dict)}
    if any(node_kinds.get(anchor['id']) != anchor['kind'] for anchor in item['anchors']):
        raise EffectBlocked('v2 Workflow lost a reviewed anchor')
    if snapshot.get('metadata', {}).get('execution_target') != policy:
        raise EffectBlocked('v2 Workflow execution policy drifted from its owned stage')
    if compare_inventory and not _canonical_v2_inventory_matches(
            item, snapshot.get('compatibility_snapshot', {}), expected_execution_target=policy):
        raise EffectBlocked('v2 Task/Session/approval inventory drifted')

def _canonical_v2_plan_check(parsed: Mapping[str, Any], item: Mapping[str, Any], preimage: Mapping[str, Any],
        *, expected_workflow_id: str | None, expected_spec: Any,
        verify_evidence: bool=True) -> None:
    snapshot = item['compatibility_snapshot']
    if verify_evidence:
        try:
            for task in snapshot['tasks']:
                refs = _v2_ref_list(task['history_refs'], f"task {task['id']} history")
                if canonical_sha256(refs) != canonical_sha256(task['history_refs']):
                    raise EffectBlocked('v2 Task history evidence changed')
            if not isinstance(parsed.get('native_evidence'), Mapping):
                for session in snapshot['sessions']:
                    refs = _v2_ref_list(session['source_refs'], f"Session {session['id']} evidence")
                    if canonical_sha256(refs) != canonical_sha256(session['source_refs']):
                        raise EffectBlocked('v2 Session source evidence changed')
        except PlanValidationError as error:
            raise EffectBlocked(f'v2 history evidence drifted: {error}') from error
    _validate_canonical_v2_target(
        parsed,
        item,
        preimage,
        expected_workflow_id,
        expected_spec,
        verify_evidence=verify_evidence,
    )
    if verify_evidence:
        try:
            current_refs = _v2_runtime_refs(item['runtime_refs'])
        except PlanValidationError as error:
            raise EffectBlocked(f'v2 runtime qualification drifted: {error}') from error
        if canonical_sha256(current_refs) != canonical_sha256(item['runtime_refs']):
            raise EffectBlocked('v2 runtime qualification drifted')


def _admit_v2_source(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
) -> dict[str, Any] | None:
    admission = item['source_admission']
    if admission is None:
        return None
    try:
        verified = admit_source_admission(
            admission,
            project_id=item['project_id'],
            workflow_id=item['workflow_id'],
            project_root=parsed['project_root'],
            kent_path=parsed['kent'],
        )
    except (OperationError, OSError, subprocess.SubprocessError) as error:
        raise EffectBlocked(f'v2 source admission failed: {error}') from error
    expected_admission_sha = canonical_sha256(admission)
    expected_gate = None
    from .delivery import source_admission_gate

    expected_gate = source_admission_gate(
        admission,
        project_id=item['project_id'],
        workflow_id=item['workflow_id'],
        project_root=str(parsed['project_root']),
    )
    if (
        not isinstance(verified, VerifiedSourceAdmission)
        or verified.project_id != item['project_id']
        or verified.workflow_id != item['workflow_id']
        or verified.project_root != str(parsed['project_root'])
        or verified.primary_root != admission['primary_root']
        or verified.primary_workspace_id != admission['primary_workspace_id']
        or verified.kit_root != admission['kit_root']
        or verified.value != admission
        or verified.source_admission_sha256 != expected_admission_sha
        or verified.receipt_sha256 != admission['receipt_ref']['sha256']
        or verified.gate != expected_gate
    ):
        raise EffectBlocked('v2 source admission result does not bind the exact plan inputs')
    inspection = verified.inspection
    if (
        inspection.get('project_id') != item['project_id']
        or inspection.get('workflow_id') != item['workflow_id']
        or inspection.get('workspace_path') != str(parsed['project_root'])
        or inspection.get('status') != 'admitted'
    ):
        raise EffectBlocked('v2 source inspection result identity differs')
    return {
        'source_admission_sha256': verified.source_admission_sha256,
        'receipt_sha256': verified.receipt_sha256,
        'inspection_sha256': canonical_sha256(inspection),
        'inspection': inspection,
    }


def _journal_source_admission(
    state: Mapping[str, Any],
    result: dict[str, Any] | None,
    *,
    effect_key: str | None = None,
) -> dict[str, Any]:
    inventory = dict(state.get('inventory') or {})
    if effect_key is None:
        inventory['source_admission_prepared'] = result
    else:
        records = dict(inventory.get('source_admissions') or {})
        records[effect_key] = result
        inventory['source_admissions'] = records
    return {**state, 'inventory': inventory}

def _canonical_v2_progress(
    parsed: Mapping[str, Any],
    item: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> list[dict[str, Any]]:
    stages = [
        stage for stage in _canonical_progress(parsed, item, prepared)
        if canonical_sha256(_canonical_v2_owned(stage['before']))
        != canonical_sha256(_canonical_v2_owned(stage['after']))
    ]
    if (
        isinstance(parsed.get('native_evidence'), Mapping)
        and item['compatibility_snapshot']['native_components']['schema'] == NATIVE_COMPONENTS_V2_SCHEMA
    ):
        for stage in stages:
            if stage['name'] == 'metadata':
                # Native workflow update saves metadata through the graph
                # store and increments its revision without rewriting Tasks.
                # Legacy v1 journals retain their original effect identities.
                version = stage['before']['version'] + 1
                stage['after'] = {
                    **stage['after'], 'version': version,
                    'graph': {**stage['after']['graph'], 'version': version},
                }
    return stages

def _require_native_execution_bindings(parsed: Mapping[str, Any], item: Mapping[str, Any]) -> None:
    if isinstance(parsed.get('native_evidence'), Mapping) and (
        item['compatibility_snapshot']['native_components']['schema'] != NATIVE_COMPONENTS_V2_SCHEMA
    ):
        raise EffectBlocked('native prepare, dispatch and completion require reviewed native-components-v2 bindings')

def _canonical_v2_stdin_limit(parsed: Mapping[str, Any], item: Mapping[str, Any], stage: Mapping[str, Any]) -> int:
    if (
        stage['name'] == 'graph'
        and isinstance(parsed.get('native_evidence'), Mapping)
        and item['compatibility_snapshot']['native_components']['schema'] == NATIVE_COMPONENTS_V2_SCHEMA
    ):
        return 1024 * 1024
    return MAX_OUTPUT


def _reconcile_canonical_workflows_v2(plan: LoadedPlan, *, mode: str, confirm: bool | str,
        kent: str | Path | None, expected_workflow_id: str | None, expected_spec: Any) -> dict[str, Any]:
    try:
        parsed = _validate_canonical_v2_plan(
            plan,
            verify_evidence=mode == 'prepare',
        )
    except PlanValidationError as error:
        if any(token in str(error) for token in (
            'runtime_refs', 'compatibility_review_ref', 'history_refs', 'source_refs', 'evidence[',
        )):
            raise EffectBlocked(f'v2 compatibility evidence drifted: {error}') from error
        raise
    if kent is not None and str(kent) != str(parsed['kent']):
        raise PlanValidationError('runtime --kent differs from v2 plan executable')
    if mode == 'rollback':
        raise PlanValidationError('v2 rollback is unsupported; apply a freshly reviewed reverse plan')
    if mode not in {'prepare', 'apply'}:
        raise PlanValidationError('canonical v2 mode is unsupported')
    if mode == 'apply' and confirm != plan.sha256:
        raise PlanValidationError('canonical v2 mutation requires exact plan confirmation')
    item = parsed['workflows'][0]
    if 'target_graph' in item:
        item['graph'] = item['target_graph']
    if 'target_metadata' in item:
        item['metadata'] = item['target_metadata']
    if mode == 'prepare':
        _require_native_execution_bindings(parsed, item)
    with OperationJournal(parsed['state_dir'], 'canonical-workflow-reconcile', plan) as journal:
        if mode == 'prepare':
            if journal.state is not None:
                raise JournalError('canonical v2 prepare refuses an existing journal')
            live = _canonical_v2_read(parsed, item)
            _canonical_v2_gate(item, live, item['expected_version'])
            _canonical_v2_plan_check(
                parsed, item, live, expected_workflow_id=expected_workflow_id, expected_spec=expected_spec,
            )
            admission_result = _admit_v2_source(parsed, item)
            progress = _canonical_v2_progress(parsed, item, live)
            final = progress[-1]['after'] if progress else live
            for stage in progress:
                stdin_limit = _canonical_v2_stdin_limit(parsed, item, stage)
                argv, cwd, env, payload, identity = _effect_inputs(
                    stage['command'], parsed['project_root'], None, stage['stdin'],
                    canonical_sha256(_canonical_v2_owned(stage['before'])),
                    canonical_sha256(_canonical_v2_owned(stage['after'])), stdin_limit=stdin_limit,
                )
                _validate_effect_exec_vectors(
                    argv, cwd, env, len(payload), identity['stdin_sha256'], journal._lock_fd, stdin_limit,
                )
            journal.persist({
                'phase': 'prepared',
                'preimage': [live],
                'inventory': {
                    'targets': {item['workflow_id']: {
                        'owned_preimage_sha256': canonical_sha256(_canonical_v2_owned(live)),
                        'owned_target_sha256': canonical_sha256(_canonical_v2_owned(final)),
                        'compatibility_snapshot_sha256': canonical_sha256(item['compatibility_snapshot']),
                    }},
                    'source_admission_prepared': admission_result,
                },
                'members': [{'workflow_id': item['workflow_id'], 'status': 'prepared'}],
                'effects': {},
            })
            return _canonical_report(plan, 'prepared', journal)
        state = journal.require_phase({'prepared', 'in_progress', 'complete'})
        preimages = state.get('preimage', [])
        members = state.get('members', [])
        if len(preimages) != 1 or len(members) != 1 or members[0].get('workflow_id') != item['workflow_id']:
            raise JournalError('canonical v2 journal member inventory drifted')
        preimage = preimages[0]
        progress = _canonical_v2_progress(parsed, item, preimage)
        status = members[0].get('status')
        if status not in {'prepared', *(stage['status'] for stage in progress), 'verified'}:
            raise JournalError('canonical v2 member progress is invalid')
        if state['phase'] == 'complete':
            expected = progress[-1]['after'] if progress else preimage
            _canonical_v2_assert_owned(parsed, item, expected)
            return {**_canonical_report(plan, 'complete', journal), 'resumed': True}
        journal.persist({**state, 'phase': 'in_progress'})
        for stage_index, stage in enumerate(progress):
            status = journal.state['members'][0]['status']
            if status in {stage['status'], 'verified'}:
                continue
            effect_key = f"apply:{item['workflow_id']}:{stage['name']}"
            existing_effect = (journal.state.get('effects') or {}).get(effect_key)
            pending_settlement = isinstance(existing_effect, dict) and existing_effect.get('status') in {
                'attempted', 'unresolved', 'ambiguous', 'verified',
            }
            before_sha = canonical_sha256(_canonical_v2_owned(stage['before']))
            after_sha = canonical_sha256(_canonical_v2_owned(stage['after']))
            if pending_settlement:
                if isinstance(parsed.get('native_evidence'), Mapping):
                    live = _canonical_v2_owned_read(parsed, item)
                else:
                    live = _canonical_v2_read(parsed, item)
                live_owned_sha = canonical_sha256(_canonical_v2_owned(live))
                allowed_owned = {after_sha} if existing_effect.get('status') == 'verified' else {
                    before_sha, after_sha,
                }
                if live_owned_sha not in allowed_owned:
                    raise EffectBlocked('v2 unresolved effect has an unknown Workflow postimage')
                owned = stage['before'] if live_owned_sha == before_sha else stage['after']
                _canonical_v2_gate(
                    item, live, owned['version'], compare_inventory=False,
                    expected_execution_target=owned['metadata']['execution_target'],
                )
                current = lambda: canonical_sha256(_canonical_v2_owned(
                    _canonical_v2_owned_read(parsed, item)
                    if isinstance(parsed.get('native_evidence'), Mapping)
                    else _canonical_v2_read(parsed, item)
                ))
                settled = _settle_or_run(
                    journal, effect_key=effect_key, command=stage['command'], cwd=parsed['project_root'],
                    preimage_sha256=before_sha, postimage_sha256=after_sha,
                    current_sha256=current, stdin_bytes=stage['stdin'],
                    stdin_limit=_canonical_v2_stdin_limit(parsed, item, stage),
                )
                if settled == 'preimage':
                    return {**_canonical_report(plan, 'in_progress', journal), 'settled': 'preimage'}
                after = _canonical_v2_assert_owned(parsed, item, stage['after'])
                journal.persist({**journal.state, 'members': [{
                    'workflow_id': item['workflow_id'], 'status': stage['status'],
                }]})
                if isinstance(parsed.get('native_evidence'), Mapping):
                    return {
                        **_canonical_report(plan, 'in_progress', journal),
                        'settled': 'postimage',
                    }
                if (
                    not _canonical_v2_inventory_matches(
                        item, after['compatibility_snapshot'],
                        expected_execution_target=stage['after']['metadata']['execution_target'],
                    )
                    and stage_index < len(progress) - 1
                ):
                    return {**_canonical_report(plan, 'in_progress', journal), 'settled': settled}
            else:
                _require_native_execution_bindings(parsed, item)
                live = _canonical_v2_read(parsed, item)
                live_owned_sha = canonical_sha256(_canonical_v2_owned(live))
                _canonical_v2_gate(
                    item, live, stage['before']['version'],
                    expected_execution_target=stage['before']['metadata']['execution_target'],
                )
                if live_owned_sha != before_sha:
                    raise EffectBlocked('v2 effect-owned state drifted before the next stage')
                _canonical_v2_plan_check(
                    parsed,
                    item,
                    preimage,
                    expected_workflow_id=expected_workflow_id,
                    expected_spec=expected_spec,
                )
                admission_result = _admit_v2_source(parsed, item)
                journal.persist(_journal_source_admission(
                    journal.state or {},
                    admission_result,
                    effect_key=effect_key,
                ))
                if isinstance(parsed.get('native_evidence'), Mapping):
                    # Re-read all mutable native evidence after static plan/source
                    # checks and immediately before recording the new effect.
                    fresh = _canonical_v2_read(parsed, item)
                    _canonical_v2_gate(
                        item, fresh, stage['before']['version'],
                        expected_execution_target=stage['before']['metadata']['execution_target'],
                    )
                    if canonical_sha256(_canonical_v2_owned(fresh)) != before_sha:
                        raise EffectBlocked('v2 effect-owned state drifted before native dispatch')
                current = lambda: canonical_sha256(_canonical_v2_owned(
                    _canonical_v2_owned_read(parsed, item)
                    if isinstance(parsed.get('native_evidence'), Mapping)
                    else _canonical_v2_read(parsed, item)
                ))
            def process_pre_release() -> None:
                if not isinstance(parsed.get('native_evidence'), Mapping):
                    return
                if current() != before_sha:
                    raise EffectBlocked('native effect-owned Workflow changed before process release')
                _native_process_binding(parsed, parsed['native_evidence'], item['project_id'])

            settled = _settle_or_run(
                journal, effect_key=effect_key, command=stage['command'], cwd=parsed['project_root'],
                preimage_sha256=canonical_sha256(_canonical_v2_owned(stage['before'])),
                postimage_sha256=canonical_sha256(_canonical_v2_owned(stage['after'])),
                current_sha256=current, stdin_bytes=stage['stdin'],
                pre_release_check=process_pre_release,
                stdin_limit=_canonical_v2_stdin_limit(parsed, item, stage),
            )
            if settled == 'preimage':
                return {**_canonical_report(plan, 'in_progress', journal), 'settled': 'preimage'}
            after = _canonical_v2_assert_owned(parsed, item, stage['after'])
            journal.persist({**journal.state, 'members': [{
                'workflow_id': item['workflow_id'], 'status': stage['status'],
            }]})
            if (
                not isinstance(parsed.get('native_evidence'), Mapping)
                and not _canonical_v2_inventory_matches(
                    item, after['compatibility_snapshot'],
                    expected_execution_target=stage['after']['metadata']['execution_target'],
                )
                and stage_index < len(progress) - 1
            ):
                return {**_canonical_report(plan, 'in_progress', journal), 'settled': settled}
            if (
                not isinstance(parsed.get('native_evidence'), Mapping)
                and not _canonical_v2_inventory_matches(
                    item, after['compatibility_snapshot'],
                    expected_execution_target=stage['after']['metadata']['execution_target'],
                )
                and stage_index < len(progress) - 1
            ):
                return {**_canonical_report(plan, 'in_progress', journal), 'settled': settled}
        _require_native_execution_bindings(parsed, item)
        live = _canonical_v2_read(parsed, item)
        expected = progress[-1]['after'] if progress else preimage
        _canonical_v2_gate(
            item, live, expected['version'],
            expected_execution_target=expected['metadata']['execution_target'],
        )
        if canonical_sha256(_canonical_v2_owned(live)) != canonical_sha256(_canonical_v2_owned(expected)):
            raise EffectBlocked('v2 final effect-owned Workflow state drifted')
        journal.persist({**journal.state, 'phase': 'complete', 'members': [{
            'workflow_id': item['workflow_id'], 'status': 'verified',
        }]})
        return _canonical_report(plan, 'complete', journal)

def _canonical_restore_progress(parsed: Mapping[str, Any], item: Mapping[str, Any], prepared: Mapping[str, Any], start:
        Mapping[str, Any]) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    current = dict(start)
    source_body = {key: prepared['graph'][key] for key in prepared['graph'] if key != 'version'}
    current_body = {key: current['graph'][key] for key in current['graph'] if key != 'version'}
    if source_body != current_body:
        graph = {**prepared['graph'], 'version': current['version'] + 1}
        target = {**current, 'version': graph['version'], 'graph': graph}
        stages.append(_canonical_stage(parsed, item, 'graph', current, target))
        current = target
    if current['metadata'] != prepared['metadata']:
        target = {**current, 'metadata': prepared['metadata']}
        stages.append(_canonical_stage(parsed, item, 'metadata', current, target))
    return stages

def _canonical_report(plan: LoadedPlan, phase: str, journal: OperationJournal) -> dict[str, Any]:
    return {'schema': 'canonical-workflow-report-v2', 'plan_sha256': plan.sha256, 'phase': phase, 'effects_released':
            _effect_attempts(journal.state or {})}

def reconcile_canonical_workflows(plan: LoadedPlan, *, mode: str, confirm: bool | str=False, kent: str | Path |
        None=None, expected_workflow_id: str | None=None, expected_spec: Any=None) -> dict[str, Any]:
    if plan.schema == CANONICAL_V2_SCHEMA or plan.value.get('schema') == CANONICAL_V2_SCHEMA:
        return _reconcile_canonical_workflows_v2(
            plan,
            mode=mode,
            confirm=confirm,
            kent=kent,
            expected_workflow_id=expected_workflow_id,
            expected_spec=expected_spec,
        )
    if expected_workflow_id is not None or expected_spec is not None:
        raise PlanValidationError('expected Workflow/spec binding is supported only for canonical v2')
    parsed = _validate_canonical_plan(plan)
    if kent is not None and str(kent) != str(parsed['kent']):
        raise PlanValidationError('runtime --kent differs from plan executable')
    if mode not in {'prepare', 'apply', 'rollback'}:
        raise PlanValidationError('canonical mode is unsupported')
    if mode in {'apply', 'rollback'} and confirm not in (True, plan.sha256):
        raise PlanValidationError('canonical mutation requires confirmation')
    _canonical_d9_check(parsed['d9'])
    with OperationJournal(parsed['state_dir'], 'canonical-workflow-reconcile', plan) as journal:
        if mode == 'prepare':
            if journal.state is not None:
                raise JournalError('canonical prepare refuses an existing journal')
            preimages = []
            targets: dict[str, Any] = {}
            for item in parsed['workflows']:
                live = _canonical_read(parsed, item)
                _canonical_gate(item, live, item['expected_version'])
                stages = _canonical_progress(parsed, item, live)
                preimages.append(live)
                targets[item['workflow_id']] = {'preimage_sha256': canonical_sha256(live), 'target_sha256':
                        canonical_sha256(stages[-1]['after'] if stages else live)}
            journal.persist({'phase': 'prepared', 'preimage': preimages, 'inventory': {'targets': targets}, 'members':
                    [{'workflow_id': item['workflow_id'], 'status': 'prepared'} for item in parsed['workflows']],
                    'effects': {}})
            return _canonical_report(plan, 'prepared', journal)
        state = journal.require_phase({'prepared', 'in_progress', 'complete'})
        if mode == 'rollback' and state['phase'] == 'prepared':
            journal.persist({**state, 'phase': 'rolled_back'})
            return _canonical_report(plan, 'rolled_back', journal)
        if mode == 'rollback' and state['phase'] == 'complete':
            raise JournalError('canonical completion requires a new restore plan')
        preimages = {row['workflow_id']: row for row in state.get('preimage', [])}
        statuses = {row['workflow_id']: row['status'] for row in state.get('members', [])}
        ids = {item['workflow_id'] for item in parsed['workflows']}
        if set(preimages) != ids or set(statuses) != ids:
            raise JournalError('canonical journal member inventory drifted')
        progress = {item['workflow_id']: _canonical_progress(parsed, item, preimages[item['workflow_id']]) for item in
                parsed['workflows']}
        if mode == 'rollback':
            inventory = dict(state.get('inventory') or {})
            restore = inventory.get('restore')
            if restore is None:
                restore = {}
                for item in parsed['workflows']:
                    live = _canonical_read(parsed, item)
                    _canonical_gate(item, live, live['version'])
                    candidates = [preimages[item['workflow_id']]] + [stage['after'] for stage in
                            progress[item['workflow_id']]]
                    if all((canonical_sha256(live) != canonical_sha256(candidate) for candidate in candidates)):
                        raise EffectBlocked('canonical rollback source state is ambiguous')
                    restore[item['workflow_id']] = {'start': live, 'status': 'prepared'}
                inventory['restore'] = restore
                journal.persist({**state, 'phase': 'in_progress', 'inventory': inventory})
            for item in parsed['workflows']:
                wid = item['workflow_id']
                record = restore[wid]
                stages = _canonical_restore_progress(parsed, item, preimages[wid], record['start'])
                for stage in stages:
                    if record['status'] not in {'prepared', stage['status']}:
                        continue
                    if record['status'] == stage['status']:
                        continue
                    _canonical_d9_check(parsed['d9'])
                    _canonical_assert_expected(parsed, item, stage['before'])
                    current = lambda item=item: canonical_sha256(_canonical_read(parsed, item))
                    settled = _settle_or_run(journal, effect_key=f"restore:{wid}:{stage['name']}",
                            command=stage['command'], cwd=parsed['project_root'],
                            preimage_sha256=canonical_sha256(stage['before']),
                            postimage_sha256=canonical_sha256(stage['after']), current_sha256=current,
                            stdin_bytes=stage['stdin'])
                    if settled == 'preimage':
                        return {**_canonical_report(plan, 'in_progress', journal), 'settled': 'preimage'}
                    _canonical_assert_expected(parsed, item, stage['after'])
                    record['status'] = stage['status']
                    journal.persist({**journal.state, 'inventory': inventory})
                record['status'] = 'verified'
                journal.persist({**journal.state, 'inventory': inventory})
            for item in parsed['workflows']:
                stages = _canonical_restore_progress(parsed, item, preimages[item['workflow_id']],
                        restore[item['workflow_id']]['start'])
                expected = stages[-1]['after'] if stages else restore[item['workflow_id']]['start']
                _canonical_assert_expected(parsed, item, expected)
            journal.persist({**journal.state, 'phase': 'rolled_back'})
            return _canonical_report(plan, 'rolled_back', journal)
        if (state.get('inventory') or {}).get('restore') is not None:
            raise JournalError('canonical forward restore is already in progress')
        if state['phase'] == 'complete':
            for item in parsed['workflows']:
                expected = _status_snapshot('verified', preimages[item['workflow_id']], progress[item['workflow_id']])
                _canonical_assert_expected(parsed, item, expected)
            return {**_canonical_report(plan, 'complete', journal), 'resumed': True}
        journal.persist({**state, 'phase': 'in_progress'})
        for item in parsed['workflows']:
            wid = item['workflow_id']
            stages = progress[wid]
            if statuses[wid] == 'verified':
                continue
            for stage in stages:
                if statuses[wid] == stage['status']:
                    continue
                prior = _status_snapshot(statuses[wid], preimages[wid], stages)
                if canonical_sha256(prior) != canonical_sha256(stage['before']):
                    continue
                _canonical_d9_check(parsed['d9'])
                existing = (journal.state.get('effects') or {}).get(f"apply:{wid}:{stage['name']}")
                for other in parsed['workflows']:
                    other_id = other['workflow_id']
                    live = _canonical_read(parsed, other)
                    expected = _status_snapshot(statuses[other_id], preimages[other_id], progress[other_id])
                    _canonical_gate(other, live, live['version'])
                    allowed = [expected]
                    if other_id == wid and isinstance(existing, dict):
                        allowed.append(stage['after'])
                    if all((canonical_sha256(live) != canonical_sha256(value) for value in allowed)):
                        raise EffectBlocked('canonical pre-effect allocation/state drifted')
                current = lambda item=item: canonical_sha256(_canonical_read(parsed, item))
                settled = _settle_or_run(journal, effect_key=f"apply:{wid}:{stage['name']}", command=stage['command'],
                        cwd=parsed['project_root'], preimage_sha256=canonical_sha256(stage['before']),
                        postimage_sha256=canonical_sha256(stage['after']), current_sha256=current,
                        stdin_bytes=stage['stdin'])
                if settled == 'preimage':
                    return {**_canonical_report(plan, 'in_progress', journal), 'settled': 'preimage'}
                _canonical_assert_expected(parsed, item, stage['after'])
                statuses[wid] = stage['status']
                journal.persist({**journal.state, 'members': [{'workflow_id': member_id, 'status': statuses[member_id]}
                        for member_id in sorted(statuses)]})
            statuses[wid] = 'verified'
            journal.persist({**journal.state, 'members': [{'workflow_id': member_id, 'status': statuses[member_id]} for
                    member_id in sorted(statuses)]})
        for item in parsed['workflows']:
            expected = _status_snapshot('verified', preimages[item['workflow_id']], progress[item['workflow_id']])
            _canonical_assert_expected(parsed, item, expected)
        journal.persist({**journal.state, 'phase': 'complete'})
        return _canonical_report(plan, 'complete', journal)



def _portfolio_sequence(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PlanValidationError(f'{label} must be a non-negative integer')
    return value


def _portfolio_root(value: Any, label: str) -> Path:
    root = _absolute_path(value, label)
    if not root.exists() or not root.is_dir():
        raise PlanValidationError(f'{label} must be an existing directory')
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise PlanValidationError(f'{label} must resolve to a directory')
    return resolved


def _portfolio_member(value: Any, index: int, project_id: str) -> dict[str, Any]:
    label = f'retirement_members[{index}]'
    item = _closed(value, {
        'sequence', 'project_id', 'project_root', 'workflow_id', 'revision', 'links', 'default', 'tasks',
        'sessions', 'worktrees', 'retained', 'absent', 'delete_preview',
    }, label)
    if _uuid(_required(item, 'project_id', label), f'{label}.project_id') != project_id:
        raise PlanValidationError('retirement member project identity is inconsistent')
    root = _portfolio_root(_required(item, 'project_root', label), f'{label}.project_root')
    member = dict(_typed_member({key: item[key] for key in item if key not in {
        'sequence', 'project_id', 'project_root',
    }}, index, project_id))
    member.update({
        'sequence': _portfolio_sequence(_required(item, 'sequence', label), f'{label}.sequence'),
        'project_id': project_id,
        'project_root': root,
    })
    return member


def _portfolio_canonical(value: Any, index: int) -> dict[str, Any]:
    label = f'canonical_workflows[{index}]'
    item = _closed(value, {
        'sequence', 'project_id', 'project_root', 'workflow_id', 'expected_version', 'intent', 'graph', 'metadata',
        'terminal_tasks', 'terminal_anchors', 'links', 'default',
    }, label)
    project_id = _uuid(_required(item, 'project_id', label), f'{label}.project_id')
    root = _portfolio_root(_required(item, 'project_root', label), f'{label}.project_root')
    workflow_id = _uuid(_required(item, 'workflow_id', label), f'{label}.workflow_id')
    version = _required(item, 'expected_version', label)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise PlanValidationError(f'{label}.expected_version is invalid')
    intent = _string(_required(item, 'intent', label), f'{label}.intent')
    if intent not in {'graph-only', 'metadata-only', 'graph-and-metadata'}:
        raise PlanValidationError('canonical intent is unsupported')
    tasks = []
    raw_tasks = _bounded_list(_required(item, 'terminal_tasks', label), f'{label}.terminal_tasks')
    for task_index, raw in enumerate(raw_tasks):
        task_label = f'{label}.terminal_tasks[{task_index}]'
        task = _closed(raw, {'id', 'status'}, task_label)
        tasks.append({
            'id': _string(_required(task, 'id', task_label), f'{task_label}.id'),
            'status': _string(_required(task, 'status', task_label), f'{task_label}.status'),
        })
    tasks.sort(key=lambda row: row['id'])
    _unique([row['id'] for row in tasks], f'{label}.terminal_tasks')
    anchors = []
    raw_anchors = _bounded_list(_required(item, 'terminal_anchors', label), f'{label}.terminal_anchors')
    for anchor_index, raw in enumerate(raw_anchors):
        anchor_label = f'{label}.terminal_anchors[{anchor_index}]'
        anchor = _closed(raw, {'id', 'kind'}, anchor_label)
        anchors.append({
            'id': _string(_required(anchor, 'id', anchor_label), f'{anchor_label}.id'),
            'kind': _string(_required(anchor, 'kind', anchor_label), f'{anchor_label}.kind'),
        })
    anchors.sort(key=lambda row: row['id'])
    _unique([row['id'] for row in anchors], f'{label}.terminal_anchors')
    links = []
    for link_index, raw in enumerate(_bounded_list(_required(item, 'links', label), f'{label}.links')):
        link_label = f'{label}.links[{link_index}]'
        link = _closed(raw, {'project_id', 'workflow_id', 'is_default'}, link_label)
        parsed = {
            'project_id': _uuid(_required(link, 'project_id', link_label), f'{link_label}.project_id'),
            'workflow_id': _uuid(_required(link, 'workflow_id', link_label), f'{link_label}.workflow_id'),
            'is_default': _required(link, 'is_default', link_label),
        }
        if parsed['project_id'] != project_id or parsed['workflow_id'] != workflow_id:
            raise PlanValidationError('canonical link identity drifted')
        if not isinstance(parsed['is_default'], bool):
            raise PlanValidationError('canonical link default flag is invalid')
        links.append(parsed)
    links.sort(key=lambda row: (row['project_id'], row['workflow_id']))
    default_raw = _required(item, 'default', label)
    default = _uuid(default_raw, f'{label}.default') if default_raw is not None else None
    expected_default = workflow_id if any(row['is_default'] for row in links) else None
    if default != expected_default:
        raise PlanValidationError('canonical default does not match the project link')
    result: dict[str, Any] = {
        'sequence': _portfolio_sequence(_required(item, 'sequence', label), f'{label}.sequence'),
        'project_id': project_id,
        'project_root': root,
        'workflow_id': workflow_id,
        'expected_version': version,
        'intent': intent,
        'terminal_tasks': tasks,
        'terminal_anchors': anchors,
        'links': links,
        'default': default,
    }
    if intent in {'graph-only', 'graph-and-metadata'}:
        graph = dict(_closed(_required(item, 'graph', label), {
            'version', 'nodes', 'edges', 'node_groups', 'transition_groups',
        }, f'{label}.graph'))
        if graph.get('version') != version + 1:
            raise PlanValidationError('canonical target graph must advance exactly one version')
        for key in ('nodes', 'edges', 'node_groups', 'transition_groups'):
            _bounded_list(_required(graph, key, f'{label}.graph'), f'{label}.graph.{key}')
        kinds = {
            node.get('id', node.get('node_id')): node.get('kind')
            for node in graph['nodes'] if isinstance(node, dict)
        }
        if any(kinds.get(anchor['id']) != anchor['kind'] for anchor in anchors):
            raise PlanValidationError('canonical target graph does not preserve terminal anchors')
        result['graph'] = graph
    elif 'graph' in item:
        raise PlanValidationError('metadata-only workflow cannot carry graph')
    if intent in {'metadata-only', 'graph-and-metadata'}:
        metadata = _closed(
            _required(item, 'metadata', label),
            {'name', 'description', 'execution_target'},
            f'{label}.metadata',
        )
        result['metadata'] = {
            'name': _string(_required(metadata, 'name', f'{label}.metadata'), 'metadata.name'),
            'description': _string(
                _required(metadata, 'description', f'{label}.metadata'),
                'metadata.description',
                nonempty=False,
            ),
            'execution_target': _string(
                _required(metadata, 'execution_target', f'{label}.metadata'),
                'metadata.execution_target',
            ),
        }
        result['metadata']['execution_target'] = _plan_execution_target(
            result['metadata']['execution_target'],
            f'{label}.metadata.execution_target',
        )
    elif 'metadata' in item:
        raise PlanValidationError('graph-only workflow cannot carry metadata')
    return result


def _validate_release_live_portfolio_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(plan.value, {
        'schema', 'state_dir', 'kent', 'database', 'retirement_members', 'canonical_workflows',
    }, 'release-live-portfolio plan')
    if data.get('schema') != RELEASE_LIVE_PORTFOLIO_SCHEMA:
        raise PlanValidationError('release-live-portfolio schema is invalid')
    state_dir = _absolute_path(_required(data, 'state_dir', 'release-live-portfolio plan'), 'state_dir')
    if state_dir.exists() and state_dir.is_symlink():
        raise PlanValidationError('state_dir must not be a symlink')
    kent_data = _closed(_required(data, 'kent', 'release-live-portfolio plan'), {'path', 'sha256'}, 'kent')
    kent = _kent_path(_required(kent_data, 'path', 'kent'), 'kent.path')
    kent_sha256 = _sha256(_required(kent_data, 'sha256', 'kent'), 'kent.sha256')
    _verify_executable(kent, kent_sha256)
    database_data = _closed(_required(data, 'database', 'release-live-portfolio plan'), {
        'path', 'schema', 'session_roots',
    }, 'database')
    database = _absolute_path(_required(database_data, 'path', 'database'), 'database.path')
    schema = _string(_required(database_data, 'schema', 'database'), 'database.schema')
    if schema != KENT_SCHEMA_IDENTITY:
        raise PlanValidationError('unsupported Kent persistence schema')
    session_roots = sorted({
        _absolute_path(root, 'database.session_root')
        for root in _bounded_list(_required(database_data, 'session_roots', 'database'), 'database.session_roots')
    }, key=str)
    raw_retirement = _bounded_list(
        _required(data, 'retirement_members', 'release-live-portfolio plan'), 'retirement_members'
    )
    raw_canonical = _bounded_list(
        _required(data, 'canonical_workflows', 'release-live-portfolio plan'), 'canonical_workflows'
    )
    if len(raw_retirement) != 6 or len(raw_canonical) != 4:
        raise PlanValidationError('release-live-portfolio requires six retirement and four canonical Workflows')
    project_ids: set[str] = set()
    project_roots: dict[str, Path] = {}
    physical_roots: dict[Path, str] = {}
    retirement: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_retirement):
        project_id = _uuid(_required(raw, 'project_id', f'retirement_members[{index}]'), 'project_id')
        member = _portfolio_member(raw, index, project_id)
        project_ids.add(project_id)
        root = member['project_root']
        if project_id in project_roots and project_roots[project_id] != root:
            raise PlanValidationError('project ID maps to multiple roots')
        if root in physical_roots and physical_roots[root] != project_id:
            raise PlanValidationError('project roots resolve to multiple project IDs')
        project_roots[project_id] = root
        physical_roots[root] = project_id
        retirement.append(member)
    canonical: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_canonical):
        item = _portfolio_canonical(raw, index)
        project_ids.add(item['project_id'])
        if item['project_id'] in project_roots and project_roots[item['project_id']] != item['project_root']:
            raise PlanValidationError('project ID maps to multiple roots')
        if item['project_root'] in physical_roots and physical_roots[item['project_root']] != item['project_id']:
            raise PlanValidationError('project roots resolve to multiple project IDs')
        project_roots[item['project_id']] = item['project_root']
        physical_roots[item['project_root']] = item['project_id']
        canonical.append(item)
    for label, rows in (('retirement_members', retirement), ('canonical_workflows', canonical)):
        sequences = [row['sequence'] for row in rows]
        _unique([str(value) for value in sequences], f'{label}.sequence')
        if sequences != sorted(sequences):
            raise PlanValidationError(f'{label} must be ordered by sequence')
    retirement_ids = [row['workflow_id'] for row in retirement]
    canonical_ids = [row['workflow_id'] for row in canonical]
    _unique(retirement_ids, 'retirement Workflow identities')
    _unique(canonical_ids, 'canonical Workflow identities')
    if set(retirement_ids) & set(canonical_ids):
        raise PlanValidationError('canonical and retirement Workflow identities overlap')
    task_ids = [task['id'] for row in retirement for task in row['tasks']]
    task_ids.extend(task['id'] for row in canonical for task in row['terminal_tasks'])
    _unique(task_ids, 'Task identities')
    session_ids = [session['id'] for row in retirement for session in row['sessions']]
    _unique(session_ids, 'Session identities')
    worktree_paths = [row['path'] for member in retirement for row in member['worktrees']]
    _unique(worktree_paths, 'worktree identities')
    resource_ids = [resource['id'] for member in retirement for resource in [*member['retained'], *member['absent']]]
    _unique(resource_ids, 'resource identities')
    if len({row['project_id'] for row in retirement}) != 2:
        raise PlanValidationError('retirement set must cover exactly two projects')
    if len({str(root) for root in project_roots.values()}) != len(project_roots):
        raise PlanValidationError('project root maps to multiple project IDs')
    if len({row['project_id'] for row in canonical}) != 4:
        raise PlanValidationError('canonical set must cover exactly four projects')
    if len(task_ids) != 12 + sum(len(row['terminal_tasks']) for row in canonical):
        raise PlanValidationError('retirement Task count is not representable')
    allowed_roots = {str(root) for root in session_roots}
    for member in retirement:
        for session in member['sessions']:
            if session['root'] not in allowed_roots:
                raise PlanValidationError('Session root is not in the portfolio allowlist')
    return {
        'state_dir': state_dir, 'kent': kent, 'kent_sha256': kent_sha256, 'database': database, 'schema': schema,
        'session_roots': session_roots, 'project_ids': sorted(project_ids), 'project_roots': project_roots,
        'retirement_members': retirement, 'canonical_workflows': canonical,
    }


def _portfolio_project_rows(parsed: Mapping[str, Any]) -> list[dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    roots = {Path(row['project_root']) for row in [*parsed['retirement_members'], *parsed['canonical_workflows']]}
    for root in sorted(roots, key=str):
        for row in _project_rows(parsed['kent'], root):
            old = rows.get(row['id'])
            if old is not None and old != row:
                raise EffectBlocked('portfolio project identity/root mapping is ambiguous')
            rows[row['id']] = row
    expected = {project_id: str(parsed['project_roots'][project_id]) for project_id in parsed['project_ids']}
    actual: dict[str, str] = {}
    for project_id, row in rows.items():
        if project_id not in expected:
            continue
        try:
            actual[project_id] = str(_portfolio_root(row['path'], f'project {project_id}.path'))
        except (PlanValidationError, OSError) as error:
            raise EffectBlocked('portfolio project root is not a canonical directory') from error
    if actual != expected:
        raise EffectBlocked('portfolio project identity/root mapping drifted')
    normalized = {project_id: {**rows[project_id], 'path': actual[project_id]} for project_id in expected}
    return sorted((normalized[project_id] for project_id in expected), key=lambda row: row['id'])


def _portfolio_d9_parsed(parsed: Mapping[str, Any], member: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'project_id': member['project_id'], 'state_dir': parsed['state_dir'], 'kent': parsed['kent'],
        'kent_sha256': parsed['kent_sha256'], 'database': parsed['database'], 'schema': parsed['schema'],
        'project_root': member['project_root'], 'session_roots': parsed['session_roots'], 'members': [member],
    }


def _portfolio_read_state(parsed: Mapping[str, Any]) -> dict[str, Any]:
    projects = _portfolio_project_rows(parsed)
    retirement: dict[str, Any] = {}
    for member in parsed['retirement_members']:
        inventory = _d9_read_inventory(_portfolio_d9_parsed(parsed, member))
        retirement[member['workflow_id']] = inventory['members'][member['workflow_id']]
    canonical: dict[str, Any] = {}
    for item in parsed['canonical_workflows']:
        canonical_parsed = {
            'project_root': item['project_root'], 'kent': parsed['kent'], 'd9': None,
        }
        canonical[item['workflow_id']] = _canonical_read(canonical_parsed, item)
    all_task_ids = [task['id'] for member in parsed['retirement_members'] for task in member['tasks']]
    all_session_ids = [session['id'] for member in parsed['retirement_members'] for session in member['sessions']]
    sqlite_state = _sqlite_snapshot(parsed['database'], parsed['schema'], all_session_ids, all_task_ids)
    return {
        'projects': projects, 'database_schema': parsed['schema'], 'retirement': retirement,
        'canonical': canonical, 'sqlite': sqlite_state,
    }


def _portfolio_preimage_gate(parsed: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    expected_projects = {row['id']: row['path'] for row in _portfolio_project_rows(parsed)}
    actual_projects = {row['id']: row['path'] for row in state.get('projects', [])}
    if actual_projects != expected_projects or state.get('database_schema') != parsed['schema']:
        raise EffectBlocked('portfolio project or database preimage drifted')
    for member in parsed['retirement_members']:
        live = state['retirement'].get(member['workflow_id'])
        if not isinstance(live, dict):
            raise EffectBlocked('retirement preimage member is missing')
        _d9_member_live_gate(member, live)
    for item in parsed['canonical_workflows']:
        live = state['canonical'].get(item['workflow_id'])
        if not isinstance(live, dict):
            raise EffectBlocked('canonical preimage member is missing')
        _canonical_gate(item, live, item['expected_version'])


def _portfolio_expected_post(parsed: Mapping[str, Any], preimage: Mapping[str, Any]) -> dict[str, Any]:
    retirement = {}
    for member in parsed['retirement_members']:
        wid = member['workflow_id']
        retirement[wid] = _d9_expected_post(member, preimage['retirement'][wid])
    sqlite = preimage['sqlite']
    task_ids = {task['id'] for member in parsed['retirement_members'] for task in member['tasks']}
    sessions = []
    for value in sqlite['sessions']:
        row = dict(value['row']) if value['row'] is not None else None
        if row is not None:
            row['task_id'] = None
        sessions.append({**value, 'row': row})
    associations = [row for row in sqlite['associations'] if row.get('task_id') not in task_ids]
    return {
        'projects': preimage['projects'], 'database_schema': preimage['database_schema'], 'retirement': retirement,
        'canonical': preimage['canonical'], 'sqlite': {
            'schema_fingerprints': sqlite['schema_fingerprints'], 'sessions': sessions, 'associations': associations,
        },
    }


def _portfolio_state_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return canonical_sha256(expected) == canonical_sha256(actual)


def _portfolio_retirement_progress(
    parsed: Mapping[str, Any],
    preimage: Mapping[str, Any],
    statuses: Mapping[str, str],
    settling: str | None = None,
) -> dict[str, Any]:
    verified = {
        member['workflow_id'] for member in parsed['retirement_members']
        if statuses.get(member['workflow_id']) == 'verified'
    }
    if settling is not None:
        verified.add(settling)
    retirement = dict(preimage['retirement'])
    task_ids = set()
    session_ids = set()
    for member in parsed['retirement_members']:
        wid = member['workflow_id']
        if wid in verified:
            retirement[wid] = _d9_expected_post(member, preimage['retirement'][wid])
            task_ids.update(task['id'] for task in member['tasks'])
            session_ids.update(session['id'] for session in member['sessions'])
    sqlite = preimage['sqlite']
    sessions = []
    for value in sqlite['sessions']:
        row = dict(value['row']) if value['row'] is not None else None
        if row is not None and value['session_id'] in session_ids:
            row['task_id'] = None
        sessions.append({**value, 'row': row})
    associations = [row for row in sqlite['associations'] if row.get('task_id') not in task_ids]
    return {
        'projects': preimage['projects'], 'database_schema': preimage['database_schema'],
        'retirement': retirement, 'canonical': preimage['canonical'],
        'sqlite': {
            'schema_fingerprints': sqlite['schema_fingerprints'],
            'sessions': sessions, 'associations': associations,
        },
    }


def _portfolio_retirement_gate(
    parsed: Mapping[str, Any],
    live: Mapping[str, Any],
    preimage: Mapping[str, Any],
    statuses: Mapping[str, str],
    settling: str | None = None,
) -> None:
    expected = _portfolio_retirement_progress(parsed, preimage, statuses, settling)
    if _portfolio_state_matches(expected, live):
        return
    if settling is not None:
        expected_pre = _portfolio_retirement_progress(parsed, preimage, statuses)
        if _portfolio_state_matches(expected_pre, live):
            return
    raise EffectBlocked('retirement portfolio state drifted before the next delete')


def _portfolio_d9_post_gate(
    parsed: Mapping[str, Any],
    live: Mapping[str, Any],
    preimage: Mapping[str, Any],
) -> None:
    expected = _portfolio_expected_post(parsed, preimage)
    expected['canonical'] = live.get('canonical')
    if not _portfolio_state_matches(expected, live):
        raise EffectBlocked('complete D9 poststate is absent or drifted')


def _portfolio_canonical_state_gate(
    parsed: Mapping[str, Any],
    live: Mapping[str, Any],
    journal: OperationJournal,
    in_flight: tuple[str, str] | None = None,
) -> None:
    effects = journal.state.get('effects') or {}
    for item in parsed['canonical_workflows']:
        wid = item['workflow_id']
        current = live['canonical'].get(wid)
        if not isinstance(current, dict):
            raise EffectBlocked('canonical Workflow is missing before the next effect')
        before = journal.state['canonical_preimage']['members'][wid]
        stages = _canonical_progress(
            {'kent': parsed['kent'], 'project_root': item['project_root']}, item, before
        )
        status = _portfolio_member_status(journal.state, wid)
        stage_statuses = [stage['status'] for stage in stages]
        if status == 'pending':
            status_index = -1
        elif status == 'verified':
            status_index = len(stages)
        elif status in stage_statuses:
            status_index = stage_statuses.index(status)
        else:
            raise JournalError(f'canonical member status is invalid: {status!r}')
        if status_index < 0:
            expected = before
        elif status_index == len(stages):
            expected = stages[-1]['after'] if stages else before
        else:
            expected = stages[status_index]['after']
        allowed = {canonical_sha256(expected)}
        if in_flight is not None and in_flight[0] == wid:
            requested_name = in_flight[1]
            requested = next((stage for stage in stages if stage['name'] == requested_name), None)
            if requested is None:
                raise EffectBlocked('canonical requested stage is not in the plan')
            requested_index = stages.index(requested)
            if requested_index > status_index + 1:
                raise EffectBlocked('canonical requested stage is ahead of the journal')
            if requested_index == status_index + 1:
                effect = effects.get(f'apply:{wid}:{requested_name}')
                if isinstance(effect, dict) and effect.get('status') in {
                    'attempted', 'unresolved', 'ambiguous', 'verified',
                }:
                    allowed.update({
                        canonical_sha256(requested['before']), canonical_sha256(requested['after'])
                    })
                elif effect is not None and isinstance(effect, dict) and effect.get('status') == 'settled_preimage':
                    allowed.add(canonical_sha256(requested['before']))
        if canonical_sha256(current) not in allowed:
            raise EffectBlocked('canonical Workflow state drifted before the next effect')


def _portfolio_forward_progress(
    parsed: Mapping[str, Any], item: Mapping[str, Any], preimage: Mapping[str, Any], journal: OperationJournal,
) -> tuple[list[Mapping[str, Any]], int]:
    stages = _canonical_progress(
        {'kent': parsed['kent'], 'project_root': item['project_root']}, item, preimage
    )
    effects = journal.state.get('effects') or {}
    count = 0
    for stage in stages:
        effect = effects.get(f'apply:{item["workflow_id"]}:{stage["name"]}')
        if not isinstance(effect, dict) or effect.get('status') != 'verified':
            break
        count += 1
    if any(
        isinstance(effects.get(f'apply:{item["workflow_id"]}:{stage["name"]}'), dict)
        and effects[f'apply:{item["workflow_id"]}:{stage["name"]}'].get('status') == 'verified'
        for stage in stages[count:]
    ):
        raise JournalError('canonical apply effects are not an exact prefix')
    return stages, count


def _portfolio_forward_state(
    parsed: Mapping[str, Any], item: Mapping[str, Any], preimage: Mapping[str, Any], journal: OperationJournal
) -> Mapping[str, Any]:
    stages, count = _portfolio_forward_progress(parsed, item, preimage, journal)
    return preimage if count == 0 else stages[count - 1]['after']


def _portfolio_restore_stages(
    parsed: Mapping[str, Any], item: Mapping[str, Any], preimage: Mapping[str, Any],
    start: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if start is None:
        forward = _canonical_progress(
            {'kent': parsed['kent'], 'project_root': item['project_root']}, item, preimage
        )
        start = forward[-1]['after'] if forward else preimage
    return _canonical_restore_progress(
        {'kent': parsed['kent'], 'project_root': item['project_root']}, item, preimage, start
    )


def _portfolio_restore_state_gate(
    parsed: Mapping[str, Any],
    live: Mapping[str, Any],
    journal: OperationJournal,
    in_flight: tuple[str, str] | None = None,
) -> None:
    effects = journal.state.get('effects') or {}
    for item in parsed['canonical_workflows']:
        wid = item['workflow_id']
        current = live['canonical'].get(wid)
        if not isinstance(current, dict):
            raise EffectBlocked('canonical Workflow is missing during rollback')
        preimage = journal.state['canonical_preimage']['members'][wid]
        expected_forward = _portfolio_forward_state(parsed, item, preimage, journal)
        restore_stages = _portfolio_restore_stages(parsed, item, preimage, expected_forward)
        expected = expected_forward
        allowed = {canonical_sha256(expected)}
        for stage in restore_stages:
            effect_key = f'restore:{wid}:{stage["name"]}'
            effect = effects.get(effect_key)
            if isinstance(effect, dict) and effect.get('status') == 'verified':
                expected = stage['after']
                allowed = {canonical_sha256(expected)}
                continue
            if isinstance(effect, dict) and effect.get('status') == 'settled_preimage':
                allowed = {canonical_sha256(stage['before'])}
                break
            if isinstance(effect, dict) and effect.get('status') in {'attempted', 'unresolved', 'ambiguous'}:
                allowed.update({canonical_sha256(stage['before']), canonical_sha256(stage['after'])})
                break
            if in_flight == (wid, stage['name']):
                allowed.update({canonical_sha256(stage['before']), canonical_sha256(stage['after'])})
                break
            break
        if canonical_sha256(current) not in allowed:
            raise EffectBlocked('canonical rollback state drifted')


def _portfolio_member_status(state: Mapping[str, Any], key: str) -> str:
    rows = state.get('members')
    if not isinstance(rows, list):
        raise JournalError('release-live-portfolio journal members are invalid')
    matches = [row for row in rows if isinstance(row, dict) and row.get('workflow_id') == key]
    if len(matches) != 1 or not isinstance(matches[0].get('status'), str):
        raise JournalError('release-live-portfolio journal member identity is invalid')
    return matches[0]['status']


def _portfolio_set_status(journal: OperationJournal, key: str, status: str) -> None:
    state = journal.state or {}
    rows = []
    for row in state.get('members', []):
        if not isinstance(row, dict):
            raise JournalError('release-live-portfolio journal members are invalid')
        rows.append({**row, 'status': status if row.get('workflow_id') == key else row.get('status')})
    journal.persist({**state, 'members': rows})


def _release_live_report(plan: LoadedPlan, phase: str, journal: OperationJournal | None = None) -> dict[str, Any]:
    effects = _effect_attempts(journal.state) if journal is not None else 0
    return {
        'schema': RELEASE_LIVE_PORTFOLIO_REPORT, 'operation': RELEASE_LIVE_PORTFOLIO_OPERATION,
        'plan_sha256': plan.sha256, 'phase': phase, 'effects_released': effects,
    }


def _portfolio_journal_preimage(parsed: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'projects': state['projects'], 'database_schema': state['database_schema'],
        'sqlite': state['sqlite'], 'members': state['retirement'],
    }


def _portfolio_canonical_preimage(state: Mapping[str, Any]) -> dict[str, Any]:
    return {'projects': state['projects'], 'members': state['canonical']}


def _portfolio_canonical_targets(parsed: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    targets = {}
    for item in parsed['canonical_workflows']:
        before = state['canonical'][item['workflow_id']]
        stages = _canonical_progress({
            'kent': parsed['kent'], 'project_root': item['project_root'],
        }, item, before)
        targets[item['workflow_id']] = {
            'preimage_sha256': canonical_sha256(before),
            'target_sha256': canonical_sha256(stages[-1]['after'] if stages else before),
            'stages': [
                {
                    'name': stage['name'],
                    'before_sha256': canonical_sha256(stage['before']),
                    'after_sha256': canonical_sha256(stage['after']),
                }
                for stage in stages
            ],
        }
    return targets


def _portfolio_validate_projects(value: Any, label: str) -> None:
    rows = _bounded_list(value, label)
    seen: set[str] = set()
    roots: dict[Path, str] = {}
    for index, raw in enumerate(rows):
        item_label = f'{label}[{index}]'
        item = _closed(raw, {'id', 'name', 'path'}, item_label)
        project_id = _uuid(_required(item, 'id', item_label), f'{item_label}.id')
        _string(_required(item, 'name', item_label), f'{item_label}.name')
        try:
            root = _portfolio_root(_required(item, 'path', item_label), f'{item_label}.path')
        except PlanValidationError as error:
            raise JournalError(str(error)) from error
        if project_id in seen:
            raise JournalError(f'{label} contains duplicate project identities')
        if root in roots and roots[root] != project_id:
            raise JournalError(f'{label} contains aliased project roots')
        roots[root] = project_id
        seen.add(project_id)


def _portfolio_validate_sqlite(value: Any, label: str) -> None:
    snapshot = _closed(value, {'schema_fingerprints', 'sessions', 'associations'}, label)
    fingerprints = snapshot['schema_fingerprints']
    if not isinstance(fingerprints, dict) or set(fingerprints) != set(SQLITE_SCHEMA_FINGERPRINTS):
        raise JournalError(f'{label}.schema_fingerprints is not closed')
    for table, fingerprint in fingerprints.items():
        _sha256(fingerprint, f'{label}.schema_fingerprints[{table}]')
        if fingerprint != SQLITE_SCHEMA_FINGERPRINTS[table]:
            raise JournalError(f'{label}.schema_fingerprints drifted')
    session_columns = {
        'id', 'project_id', 'workspace_id', 'worktree_id', 'artifact_relpath', 'name', 'first_prompt_preview',
        'input_draft', 'category', 'created_at_unix_ms', 'updated_at_unix_ms', 'last_sequence',
        'model_request_count', 'launch_visible', 'cwd_relpath', 'continuation_json', 'locked_json',
        'usage_state_json', 'metadata_json', 'previous_session_id', 'parent_agent_session_id', 'task_id',
        'completed_compaction_count', 'manual_compact_eligible',
    }
    association_columns = {
        'task_id', 'session_id', 'node_id', 'transition_branch_key', 'association_status', 'source_session_id',
        'associated_at_unix_ms',
    }
    for index, raw in enumerate(_bounded_list(snapshot['sessions'], f'{label}.sessions')):
        row_label = f'{label}.sessions[{index}]'
        row = _closed(raw, {'session_id', 'present', 'row'}, row_label)
        _string(_required(row, 'session_id', row_label), f'{row_label}.session_id')
        if not isinstance(row['present'], bool):
            raise JournalError(f'{row_label}.present is invalid')
        if row['row'] is not None:
            value_row = _closed(row['row'], session_columns, f'{row_label}.row')
            if set(value_row) != session_columns:
                raise JournalError(f'{row_label}.row is incomplete')
    for index, raw in enumerate(_bounded_list(snapshot['associations'], f'{label}.associations')):
        row_label = f'{label}.associations[{index}]'
        row = _closed(raw, association_columns, row_label)
        if set(row) != association_columns:
            raise JournalError(f'{row_label} is incomplete')
        node_id = row['node_id']
        if not isinstance(node_id, dict) or set(node_id) != {'bytes_hex'}:
            raise JournalError(f'{row_label}.node_id is invalid')
        if not isinstance(node_id['bytes_hex'], str) or len(node_id['bytes_hex']) % 2:
            raise JournalError(f'{row_label}.node_id is invalid')


def _portfolio_validate_d9_snapshot(value: Any, label: str) -> None:
    snapshot = _closed(value, {
        'workflow', 'links', 'default', 'graph_sha256', 'validation_sha256', 'tasks', 'task_details', 'sessions',
        'session_manifests', 'sqlite', 'worktrees', 'retained', 'absent', 'preview_sha256',
    }, label)
    workflow = _closed(snapshot['workflow'], {'present', 'workflow_id', 'revision'}, f'{label}.workflow')
    if not isinstance(workflow['present'], bool):
        raise JournalError(f'{label}.workflow.present is invalid')
    _uuid(workflow['workflow_id'], f'{label}.workflow.workflow_id')
    if workflow['revision'] is not None and (not isinstance(workflow['revision'], int) or isinstance(
            workflow['revision'], bool)):
        raise JournalError(f'{label}.workflow.revision is invalid')
    for key in ('graph_sha256', 'validation_sha256', 'preview_sha256'):
        if snapshot[key] is not None:
            _sha256(snapshot[key], f'{label}.{key}')
    if not isinstance(snapshot['default'], (str, type(None))):
        raise JournalError(f'{label}.default is invalid')
    for index, raw in enumerate(_bounded_list(snapshot['links'], f'{label}.links')):
        row_label = f'{label}.links[{index}]'
        row = _closed(raw, {'project_id', 'workflow_id', 'is_default'}, row_label)
        _uuid(row['project_id'], f'{row_label}.project_id')
        _uuid(row['workflow_id'], f'{row_label}.workflow_id')
        if not isinstance(row['is_default'], bool):
            raise JournalError(f'{row_label}.is_default is invalid')
    for key in ('tasks', 'task_details'):
        for index, raw in enumerate(_bounded_list(snapshot[key], f'{label}.{key}')):
            row_label = f'{label}.{key}[{index}]'
            row = _closed(raw, {'id', 'status', 'terminal', 'current_node', 'approval_pending'}, row_label)
            _string(row['id'], f'{row_label}.id')
            _string(row['status'], f'{row_label}.status')
            if not isinstance(row['terminal'], bool) or not isinstance(row['approval_pending'], bool):
                raise JournalError(f'{row_label} terminal state is invalid')
    for index, raw in enumerate(_bounded_list(snapshot['sessions'], f'{label}.sessions')):
        row_label = f'{label}.sessions[{index}]'
        row = _closed(raw, {'id', 'status', 'task_id', 'retained', 'live_owner'}, row_label)
        _string(row['id'], f'{row_label}.id')
        _string(row['status'], f'{row_label}.status')
        if not isinstance(row['retained'], bool):
            raise JournalError(f'{row_label}.retained is invalid')
    for index, raw in enumerate(_bounded_list(snapshot['session_manifests'], f'{label}.session_manifests')):
        row_label = f'{label}.session_manifests[{index}]'
        row = _closed(raw, {'id', 'manifest'}, row_label)
        _string(row['id'], f'{row_label}.id')
        for manifest_index, manifest_raw in enumerate(_bounded_list(row['manifest'], f'{row_label}.manifest')):
            manifest_label = f'{row_label}.manifest[{manifest_index}]'
            if not isinstance(manifest_raw, dict):
                raise JournalError(f'{manifest_label} is invalid')
            manifest_fields = {'path', 'type', 'mode', 'bytes'}
            if manifest_raw.get('type') == 'file':
                manifest_fields.add('sha256')
            manifest = _closed(manifest_raw, manifest_fields, manifest_label)
            _string(manifest['path'], f'{manifest_label}.path')
            if manifest['type'] not in {'file', 'directory'}:
                raise JournalError(f'{manifest_label}.type is invalid')
            if not isinstance(manifest['mode'], int) or isinstance(manifest['mode'], bool):
                raise JournalError(f'{manifest_label}.mode is invalid')
            if not isinstance(manifest['bytes'], int) or isinstance(manifest['bytes'], bool):
                raise JournalError(f'{manifest_label}.bytes is invalid')
            if manifest['type'] == 'file':
                _sha256(manifest['sha256'], f'{manifest_label}.sha256')
    _portfolio_validate_sqlite(snapshot['sqlite'], f'{label}.sqlite')
    for key in ('worktrees', 'retained', 'absent'):
        for index, raw in enumerate(_bounded_list(snapshot[key], f'{label}.{key}')):
            row_label = f'{label}.{key}[{index}]'
            row = _closed(raw, {'path', 'branch', 'head', 'dirty', 'owner_session', 'registered', 'retained'}
                          if key == 'worktrees' else {'kind', 'id', 'path', 'sha256', 'present'}, row_label)
            if key == 'worktrees':
                _absolute_path(row['path'], f'{row_label}.path')
                if not isinstance(row['dirty'], bool) or not isinstance(row['registered'], bool) or not isinstance(
                        row['retained'], bool):
                    raise JournalError(f'{row_label} state is invalid')
            else:
                if row['kind'] not in {'file', 'directory'}:
                    raise JournalError(f'{row_label}.kind is invalid')
                _string(row['id'], f'{row_label}.id')
                _absolute_path(row['path'], f'{row_label}.path')
                if not isinstance(row['present'], bool):
                    raise JournalError(f'{row_label}.present is invalid')
                if row['sha256'] is not None:
                    _sha256(row['sha256'], f'{row_label}.sha256')


def _portfolio_validate_canonical_snapshot(value: Any, label: str) -> None:
    snapshot = _closed(value, {
        'workflow_id', 'project_id', 'version', 'metadata', 'graph', 'tasks', 'task_details', 'terminal_anchors',
        'links', 'default', 'valid',
    }, label)
    _uuid(snapshot['workflow_id'], f'{label}.workflow_id')
    _uuid(snapshot['project_id'], f'{label}.project_id')
    if not isinstance(snapshot['version'], int) or isinstance(snapshot['version'], bool):
        raise JournalError(f'{label}.version is invalid')
    metadata = _closed(snapshot['metadata'], {'name', 'description', 'execution_target'}, f'{label}.metadata')
    _string(metadata['name'], f'{label}.metadata.name')
    _string(metadata['description'], f'{label}.metadata.description', nonempty=False)
    _string(metadata['execution_target'], f'{label}.metadata.execution_target')
    graph = _closed(
        snapshot['graph'], {'version', 'nodes', 'edges', 'node_groups', 'transition_groups'}, f'{label}.graph'
    )
    if not isinstance(graph['version'], int) or isinstance(graph['version'], bool):
        raise JournalError(f'{label}.graph.version is invalid')
    for key in ('nodes', 'edges', 'node_groups', 'transition_groups'):
        _bounded_list(graph[key], f'{label}.graph.{key}')
    for key in ('tasks', 'task_details'):
        for index, raw in enumerate(_bounded_list(snapshot[key], f'{label}.{key}')):
            row_label = f'{label}.{key}[{index}]'
            row = _closed(raw, {'id', 'status', 'terminal', 'current_node', 'approval_pending'}, row_label)
            _string(row['id'], f'{row_label}.id')
            _string(row['status'], f'{row_label}.status')
            if not isinstance(row['terminal'], bool) or not isinstance(row['approval_pending'], bool):
                raise JournalError(f'{row_label} terminal state is invalid')
    for index, raw in enumerate(_bounded_list(snapshot['terminal_anchors'], f'{label}.terminal_anchors')):
        row_label = f'{label}.terminal_anchors[{index}]'
        row = _closed(raw, {'id', 'kind'}, row_label)
        _string(row['id'], f'{row_label}.id')
        _string(row['kind'], f'{row_label}.kind')
    for index, raw in enumerate(_bounded_list(snapshot['links'], f'{label}.links')):
        row_label = f'{label}.links[{index}]'
        row = _closed(raw, {'project_id', 'workflow_id', 'is_default'}, row_label)
        _uuid(row['project_id'], f'{row_label}.project_id')
        _uuid(row['workflow_id'], f'{row_label}.workflow_id')
        if not isinstance(row['is_default'], bool):
            raise JournalError(f'{row_label}.is_default is invalid')
    if not isinstance(snapshot['valid'], bool):
        raise JournalError(f'{label}.valid is invalid')


def _portfolio_effect_prefix(
    effects: Mapping[str, Any], order: Sequence[str], label: str,
) -> tuple[int, str | None]:
    pending = {'attempted', 'unresolved', 'ambiguous', 'settled_preimage'}
    prefix = 0
    current: str | None = None
    for key in order:
        if key not in effects:
            break
        raw = effects[key]
        status = raw.get('status') if isinstance(raw, dict) else None
        if status == 'verified':
            prefix += 1
            continue
        if status in pending:
            current = key
            break
        raise JournalError(f'{label} has an invalid effect status')
    tail = prefix + (1 if current is not None else 0)
    if any(key in effects for key in order[tail:]):
        raise JournalError(f'{label} is not an exact ordered prefix')
    return prefix, current


def _portfolio_effect_identity(
    parsed: Mapping[str, Any], key: str, retirement_preimage: Mapping[str, Any],
    canonical_preimage: Mapping[str, Any], stages_by_workflow: Mapping[str, Sequence[Mapping[str, Any]]],
    restore_stages_by_workflow: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    if key.startswith('delete:'):
        wid = key.removeprefix('delete:')
        member = next(row for row in parsed['retirement_members'] if row['workflow_id'] == wid)
        before = retirement_preimage['members'][wid]
        command = _kent_delete_command(parsed['kent'], wid, True)
        cwd = member['project_root']
        stdin = None
        after = _d9_expected_post(member, before)
    else:
        prefix, wid, stage_name = key.split(':', 2)
        if prefix == 'apply':
            stages = stages_by_workflow[wid]
        elif prefix == 'restore':
            stages = restore_stages_by_workflow[wid]
        else:
            raise JournalError(f'unknown portfolio effect key: {key}')
        stage = next((row for row in stages if row['name'] == stage_name), None)
        if stage is None:
            raise JournalError(f'portfolio effect {key} is not in the exact stage plan')
        command = stage['command']
        cwd = next(row['project_root'] for row in parsed['canonical_workflows'] if row['workflow_id'] == wid)
        stdin = stage['stdin']
        before = stage['before']
        after = stage['after']
    return _effect_inputs(
        command, cwd, None, stdin, canonical_sha256(before), canonical_sha256(after),
    )[4]


def _portfolio_forward_status_count(status: str, stages: Sequence[Mapping[str, Any]]) -> int:
    if status == 'pending':
        return 0
    if status == 'verified':
        return len(stages)
    for index, stage in enumerate(stages):
        if status == stage['status']:
            return index + 1
    raise JournalError(f'canonical member status is invalid: {status!r}')


def _portfolio_validate_effects(
    parsed: Mapping[str, Any], value: Any, *, phase: str,
    retirement_statuses: Mapping[str, str], canonical_statuses: Mapping[str, str],
    retirement_preimage: Mapping[str, Any], canonical_preimage: Mapping[str, Any],
    stages_by_workflow: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    if not isinstance(value, dict):
        raise JournalError('release-live-portfolio journal effects are invalid')
    delete_order = [f'delete:{row["workflow_id"]}' for row in parsed['retirement_members']]
    apply_order = [
        f'apply:{wid}:{stage["name"]}'
        for item in parsed['canonical_workflows']
        for wid in [item['workflow_id']]
        for stage in stages_by_workflow[wid]
    ]
    apply_prefix, apply_current = _portfolio_effect_prefix(value, apply_order, 'canonical apply effects')
    apply_counts = {wid: 0 for wid in stages_by_workflow}
    for key in apply_order[:apply_prefix]:
        _, wid, _ = key.split(':', 2)
        apply_counts[wid] += 1
    restore_stages_by_workflow: dict[str, list[dict[str, Any]]] = {}
    for item in parsed['canonical_workflows']:
        wid = item['workflow_id']
        before = canonical_preimage['members'][wid]
        if apply_counts[wid]:
            start = stages_by_workflow[wid][apply_counts[wid] - 1]['after']
            restore_stages_by_workflow[wid] = _canonical_restore_progress(
                {'kent': parsed['kent'], 'project_root': item['project_root']}, item, before, start,
            )
        else:
            restore_stages_by_workflow[wid] = []
    restore_order = [
        f'restore:{wid}:{stage["name"]}'
        for item in parsed['canonical_workflows']
        for wid in [item['workflow_id']]
        for stage in restore_stages_by_workflow[wid]
    ]
    apply_segment = apply_order
    if restore_order:
        apply_segment = apply_order[:apply_prefix] + ([apply_current] if apply_current is not None else [])
    order = delete_order + apply_segment + restore_order
    delete_prefix, delete_current = _portfolio_effect_prefix(value, delete_order, 'retirement effects')
    restore_prefix, restore_current = _portfolio_effect_prefix(value, restore_order, 'restore effects')
    if set(value) - set(order):
        raise JournalError('release-live-portfolio journal effect identity is invalid')
    active_current = []
    if delete_current is not None:
        active_current.append(delete_current)
    if apply_current is not None and value[apply_current].get('status') != 'settled_preimage':
        active_current.append(apply_current)
    if restore_current is not None:
        active_current.append(restore_current)
    if len(active_current) > 1:
        raise JournalError('portfolio effect prefix has multiple active receipts')
    if delete_current is not None and (apply_prefix or restore_prefix or apply_current or restore_current):
        raise JournalError('retirement current receipt cannot precede canonical effects')
    if apply_current is not None and (restore_prefix or restore_current):
        if value[apply_current].get('status') != 'settled_preimage':
            raise JournalError('canonical restore cannot follow an unresolved apply effect')
    in_flight = [
        key for key, effect in value.items()
        if isinstance(effect, dict) and effect.get('status') in {'attempted', 'unresolved', 'ambiguous'}
    ]
    if len(in_flight) > 1:
        raise JournalError('release-live-portfolio has multiple in-flight effects')
    fields = {
        'command_digest', 'cwd', 'environment_sha256', 'stdin_sha256', 'preimage_sha256', 'postimage_sha256',
        'status', 'attempt', 'child', 'result', 'settled_invocation',
    }
    required = {
        'command_digest', 'cwd', 'environment_sha256', 'stdin_sha256', 'preimage_sha256', 'postimage_sha256',
        'status', 'attempt', 'child',
    }
    for key, raw in value.items():
        label = f'journal.effects[{key}]'
        effect = _closed(raw, fields, label)
        missing = sorted(required - set(effect))
        if missing:
            raise JournalError(f'{label} is missing required fields: {missing}')
        expected = _portfolio_effect_identity(
            parsed, key, retirement_preimage, canonical_preimage, stages_by_workflow,
            restore_stages_by_workflow,
        )
        _verify_effect_identity(effect, expected)
        if effect['status'] not in {'attempted', 'unresolved', 'ambiguous', 'verified', 'settled_preimage'}:
            raise JournalError(f'{label}.status is invalid')
        if not isinstance(effect['attempt'], int) or isinstance(effect['attempt'], bool) or effect['attempt'] < 1:
            raise JournalError(f'{label}.attempt is invalid')
        child = effect['child']
        if child is not None:
            child = _closed(child, {'guardian_pid', 'child_pid'}, f'{label}.child')
            for pid_key in ('guardian_pid', 'child_pid'):
                if not isinstance(child[pid_key], int) or isinstance(child[pid_key], bool) or child[pid_key] <= 0:
                    raise JournalError(f'{label}.child.{pid_key} is invalid')
        result = effect.get('result')
        if result is not None:
            if not isinstance(result, dict):
                raise JournalError(f'{label}.result is invalid')
            if set(result) == {'timed_out'}:
                if result['timed_out'] is not True:
                    raise JournalError(f'{label}.result.timed_out is invalid')
            elif set(result) == {'returncode', 'stdout_sha256', 'stderr_sha256'}:
                if not isinstance(result['returncode'], int) or isinstance(result['returncode'], bool):
                    raise JournalError(f'{label}.result.returncode is invalid')
                _sha256(result['stdout_sha256'], f'{label}.result.stdout_sha256')
                _sha256(result['stderr_sha256'], f'{label}.result.stderr_sha256')
            else:
                raise JournalError(f'{label}.result is not closed')
        if effect.get('settled_invocation') is not None:
            _sha256(effect['settled_invocation'], f'{label}.settled_invocation')
    if restore_prefix:
        lag_key = restore_order[restore_prefix - 1]
    elif apply_prefix:
        lag_key = apply_order[apply_prefix - 1]
    elif delete_prefix:
        lag_key = delete_order[delete_prefix - 1]
    else:
        lag_key = None
    apply_lag_key = apply_order[apply_prefix - 1] if apply_prefix else None
    retirement_values = [retirement_statuses[row['workflow_id']] for row in parsed['retirement_members']]
    if any(status not in {'pending', 'verified'} for status in retirement_values):
        raise JournalError('retirement member status is invalid')
    retirement_count = sum(status == 'verified' for status in retirement_values)
    if retirement_values != ['verified'] * retirement_count + ['pending'] * (len(retirement_values) - retirement_count):
        raise JournalError('retirement member status order drifted')
    if delete_current is not None:
        if retirement_count != delete_prefix:
            raise JournalError('retirement status/effect bijection is invalid')
    elif retirement_count not in {delete_prefix, delete_prefix - 1}:
        raise JournalError('retirement status/effect bijection is invalid')
    elif retirement_count == delete_prefix - 1 and lag_key != delete_order[delete_prefix - 1]:
        raise JournalError('retirement verified effect acknowledgement is out of order')
    if phase == 'prepared':
        if value or retirement_count or any(status != 'pending' for status in canonical_statuses.values()):
            raise JournalError('prepared release-live-portfolio member state is impossible')
        return
    if phase == 'retirement_in_progress':
        if apply_prefix or restore_prefix or apply_current or restore_current:
            raise JournalError('retirement_in_progress contains canonical effects')
        if any(status != 'pending' for status in canonical_statuses.values()):
            raise JournalError('retirement_in_progress requires pending canonical members')
        return
    if phase == 'rolled_back' and not value and not retirement_count and all(
        status == 'pending' for status in canonical_statuses.values()
    ):
        return
    if delete_prefix != len(delete_order) or delete_current is not None or retirement_count != len(delete_order):
        raise JournalError(f'{phase} requires a settled retirement prefix')
    if phase == 'd9_complete':
        if apply_prefix or restore_prefix or apply_current or restore_current:
            raise JournalError('d9_complete contains canonical effects')
        if any(status != 'pending' for status in canonical_statuses.values()):
            raise JournalError('d9_complete requires pending canonical members')
        return
    if phase not in {'canonical_in_progress', 'complete', 'rolled_back'}:
        raise JournalError(f'unsupported portfolio phase: {phase}')
    if phase == 'complete' and any(key.startswith('restore:') for key in value):
        raise JournalError('complete cannot contain restore effects')
    if apply_current is not None and (restore_prefix or restore_current is not None):
        if value[apply_current].get('status') != 'settled_preimage':
            raise JournalError('canonical restore cannot follow an unresolved apply effect')
    for item in parsed['canonical_workflows']:
        wid = item['workflow_id']
        stages = stages_by_workflow[wid]
        count = apply_counts[wid]
        status = canonical_statuses[wid]
        status_count = None if status == 'rolled_back' else _portfolio_forward_status_count(status, stages)
        last_key = apply_lag_key
        lag_allowed = bool(
            status_count is not None and count and status_count == count - 1
            and last_key == f'apply:{wid}:{stages[count - 1]["name"]}'
        )
        if status_count is not None and status_count != count and not lag_allowed:
            raise JournalError('canonical status/effect bijection is invalid')
        restore_stages = restore_stages_by_workflow[wid]
        restore_keys = [f'restore:{wid}:{stage["name"]}' for stage in restore_stages]
        restore_count = sum(value[key].get('status') == 'verified' for key in restore_keys if key in value)
        restore_current_for_member = restore_current is not None and restore_current.startswith(f'restore:{wid}:')
        if count == 0 and (restore_keys or status != 'pending'):
            raise JournalError('canonical pending member has restore effects')
        if restore_count or restore_current_for_member:
            if count == 0:
                raise JournalError('canonical restore has no applied stage prefix')
            if restore_count < len(restore_stages) and status == 'rolled_back':
                raise JournalError('canonical member is rolled_back before restore completion')
            if restore_count == len(restore_stages) and status not in {
                'rolled_back', 'pending', 'verified', *[stage['status'] for stage in stages],
            }:
                raise JournalError('canonical restore acknowledgement state is invalid')
        if phase == 'complete':
            if status != 'verified' or count != len(stages) or restore_keys:
                raise JournalError('complete requires all canonical apply effects and no restore effects')
        elif phase == 'rolled_back':
            if count == 0:
                if status != 'pending' or restore_keys:
                    raise JournalError('rolled_back contains an untouched canonical member effect')
            elif status != 'rolled_back' or restore_count != len(restore_stages) or restore_current_for_member:
                raise JournalError('rolled_back restore/status bijection is invalid')
    if phase == 'complete':
        if apply_prefix != len(apply_order) or restore_prefix or restore_current is not None:
            raise JournalError('complete requires all planned apply effects and no restore effects')
        return
    if phase == 'rolled_back':
        if apply_current is not None and value[apply_current].get('status') != 'settled_preimage':
            raise JournalError('rolled_back cannot contain an unresolved apply effect')
        if restore_current is not None:
            raise JournalError('rolled_back cannot contain an unresolved restore effect')
        if not value:
            if any(status != 'pending' for status in retirement_values + list(canonical_statuses.values())):
                raise JournalError('empty rolled_back journal has impossible member state')
        return
    if any(status == 'rolled_back' for status in canonical_statuses.values()):
        if restore_prefix == 0 and restore_current is None:
            raise JournalError('rolled_back canonical status requires restore effects')


def _portfolio_validate_journal_inner(parsed: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    required = {
        'schema', 'operation', 'plan_sha256', 'phase', 'retirement_preimage', 'canonical_preimage',
        'retirement_targets', 'canonical_targets', 'members', 'effects',
    }
    if set(state) != required:
        raise JournalError('release-live-portfolio journal fields are not closed')
    if state['operation'] != RELEASE_LIVE_PORTFOLIO_OPERATION:
        raise JournalError('release-live-portfolio journal operation is invalid')
    if not isinstance(state['members'], list) or len(state['members']) != 10:
        raise JournalError('release-live-portfolio journal members are incomplete')
    expected = [row['workflow_id'] for row in [*parsed['retirement_members'], *parsed['canonical_workflows']]]
    ids = []
    for index, raw in enumerate(state['members']):
        member = _closed(raw, {'workflow_id', 'status'}, f'journal.members[{index}]')
        label = f'journal.members[{index}]'
        ids.append(_uuid(_required(member, 'workflow_id', label), f'{label}.workflow_id'))
        status = _string(_required(member, 'status', label), f'{label}.status')
        if status not in PORTFOLIO_MEMBER_STATUSES:
            raise JournalError('release-live-portfolio journal member status is invalid')
    if ids != expected:
        raise JournalError('release-live-portfolio journal member order drifted')
    status_by_workflow = {row['workflow_id']: row['status'] for row in state['members']}
    retirement_ids = {row['workflow_id'] for row in parsed['retirement_members']}
    retirement_statuses = {wid: status_by_workflow[wid] for wid in retirement_ids}
    canonical_statuses = {wid: status_by_workflow[wid] for wid in {
        row['workflow_id'] for row in parsed['canonical_workflows']
    }}
    if any(status not in {'pending', 'verified'} for status in retirement_statuses.values()):
        raise JournalError('retirement member status is invalid')
    canonical_ids = {row['workflow_id'] for row in parsed['canonical_workflows']}
    retirement_targets = state['retirement_targets']
    if not isinstance(retirement_targets, dict) or set(retirement_targets) != retirement_ids:
        raise JournalError('retirement target identities drifted')
    canonical_targets = state['canonical_targets']
    if not isinstance(canonical_targets, dict) or set(canonical_targets) != canonical_ids:
        raise JournalError('canonical target identities drifted')
    retirement_preimage = _closed(
        state['retirement_preimage'], {'projects', 'database_schema', 'sqlite', 'members'},
        'journal.retirement_preimage',
    )
    canonical_preimage = _closed(
        state['canonical_preimage'], {'projects', 'members'}, 'journal.canonical_preimage'
    )
    if canonical_preimage['projects'] != retirement_preimage['projects']:
        raise JournalError('canonical and retirement project preimages differ')
    _portfolio_validate_projects(retirement_preimage['projects'], 'journal.retirement_preimage.projects')
    _portfolio_validate_projects(canonical_preimage['projects'], 'journal.canonical_preimage.projects')
    expected_project_paths = {project_id: str(root) for project_id, root in parsed['project_roots'].items()}
    actual_project_paths = {row['id']: row['path'] for row in retirement_preimage['projects']}
    if actual_project_paths != expected_project_paths:
        raise JournalError('journal projects do not match the plan mapping')
    if retirement_preimage['database_schema'] != parsed['schema']:
        raise JournalError('retirement preimage database schema drifted')
    _portfolio_validate_sqlite(retirement_preimage['sqlite'], 'journal.retirement_preimage.sqlite')
    retirement_members = retirement_preimage.get('members')
    canonical_members = canonical_preimage.get('members')
    if not isinstance(retirement_members, dict) or set(retirement_members) != retirement_ids:
        raise JournalError('retirement preimage identities drifted')
    if not isinstance(canonical_members, dict) or set(canonical_members) != canonical_ids:
        raise JournalError('canonical preimage identities drifted')
    stages_by_workflow: dict[str, list[dict[str, Any]]] = {}
    for member in parsed['retirement_members']:
        wid = member['workflow_id']
        _portfolio_validate_d9_snapshot(retirement_members[wid], f'journal.retirement_preimage.members[{wid}]')
        target = retirement_targets.get(wid)
        _sha256(target, f'retirement_targets[{wid}]')
        expected_target = _d9_expected_post(member, retirement_members[wid])
        if target != canonical_sha256(expected_target):
            raise JournalError('retirement target preimage drifted')
    for item in parsed['canonical_workflows']:
        wid = item['workflow_id']
        _portfolio_validate_canonical_snapshot(
            canonical_members[wid], f'journal.canonical_preimage.members[{wid}]'
        )
        target = _closed(
            canonical_targets.get(wid),
            {'preimage_sha256', 'target_sha256', 'stages'},
            f'canonical_targets[{wid}]',
        )
        _sha256(target['preimage_sha256'], f'canonical_targets[{wid}].preimage_sha256')
        _sha256(target['target_sha256'], f'canonical_targets[{wid}].target_sha256')
        if target['preimage_sha256'] != canonical_sha256(canonical_members[wid]):
            raise JournalError('canonical target preimage drifted')
        stages = _canonical_progress(
            {'kent': parsed['kent'], 'project_root': item['project_root']}, item, canonical_members[wid]
        )
        expected_stages = [
            {
                'name': stage['name'],
                'before_sha256': canonical_sha256(stage['before']),
                'after_sha256': canonical_sha256(stage['after']),
            }
            for stage in stages
        ]
        if target['stages'] != expected_stages:
            raise JournalError('canonical stage target digests drifted')
        if target['target_sha256'] != canonical_sha256(stages[-1]['after'] if stages else canonical_members[wid]):
            raise JournalError('canonical final target digest drifted')
        stages_by_workflow[wid] = stages
    _portfolio_validate_effects(
        parsed,
        state['effects'],
        phase=state['phase'],
        retirement_statuses=retirement_statuses,
        canonical_statuses=canonical_statuses,
        retirement_preimage=retirement_preimage,
        canonical_preimage=canonical_preimage,
        stages_by_workflow=stages_by_workflow,
    )


def _portfolio_validate_journal(parsed: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    try:
        _portfolio_validate_journal_inner(parsed, state)
    except PlanValidationError as error:
        raise JournalError(str(error)) from error


def _portfolio_apply_direction_guard(parsed: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    effects = state.get('effects') or {}
    if any(key.startswith('restore:') for key in effects):
        raise JournalError('apply is forbidden after canonical rollback has started')
    canonical_ids = {row['workflow_id'] for row in parsed['canonical_workflows']}
    if any(
        isinstance(row, dict) and row.get('workflow_id') in canonical_ids and row.get('status') == 'rolled_back'
        for row in state.get('members', [])
    ):
        raise JournalError('apply is forbidden for a rolled_back canonical member')


def reconcile_release_live_portfolio(
    plan: LoadedPlan,
    *,
    mode: str,
    kent: str | Path | None = None,
    confirm: str | None = None,
) -> dict[str, Any]:
    if mode not in RELEASE_LIVE_PORTFOLIO_MODES:
        raise PlanValidationError('release-live-portfolio mode is unsupported')
    if mode in RELEASE_LIVE_PORTFOLIO_MUTATION_MODES and confirm != plan.sha256:
        raise PlanValidationError('release-live-portfolio mutation requires confirmation')
    parsed = _validate_release_live_portfolio_plan(plan)
    if kent is None:
        kent = parsed['kent']
    if str(kent) != str(parsed['kent']):
        raise PlanValidationError('runtime --kent differs from the plan-bound executable')
    if mode == 'preview':
        state = _portfolio_read_state(parsed)
        _portfolio_preimage_gate(parsed, state)
        return _release_live_report(plan, 'preview')
    if mode == 'prepare':
        state = _portfolio_read_state(parsed)
        _portfolio_preimage_gate(parsed, state)
        with OperationJournal(parsed['state_dir'], RELEASE_LIVE_PORTFOLIO_OPERATION, plan) as journal:
            if journal.state is not None:
                raise JournalError('release-live-portfolio prepare refuses an existing journal')
            members = [
                {'workflow_id': member['workflow_id'], 'status': 'pending'}
                for member in [*parsed['retirement_members'], *parsed['canonical_workflows']]
            ]
            journal.persist({
                'phase': 'prepared', 'retirement_preimage': _portfolio_journal_preimage(parsed, state),
                'canonical_preimage': _portfolio_canonical_preimage(state),
                'retirement_targets': {
                    member['workflow_id']: canonical_sha256(
                        _d9_expected_post(member, state['retirement'][member['workflow_id']])
                    )
                    for member in parsed['retirement_members']
                },
                'canonical_targets': _portfolio_canonical_targets(parsed, state),
                'members': members, 'effects': {},
            })
            return _release_live_report(plan, 'prepared', journal)
    with OperationJournal(parsed['state_dir'], RELEASE_LIVE_PORTFOLIO_OPERATION, plan) as journal:
        state = journal.require_phase({
            'prepared', 'retirement_in_progress', 'd9_complete', 'canonical_in_progress', 'complete', 'rolled_back',
        })
        _portfolio_validate_journal(parsed, state)
        if mode == 'rollback':
            if state['phase'] == 'prepared':
                current = _portfolio_read_state(parsed)
                _portfolio_preimage_gate(parsed, current)
                journal.persist({**state, 'phase': 'rolled_back'})
                return _release_live_report(plan, 'rolled_back', journal)
            if state['phase'] in {'retirement_in_progress', 'd9_complete'}:
                raise JournalError('D9 is irreversible after the first retirement effect')
            if state['phase'] == 'complete':
                raise JournalError('complete requires a new separately approved restore plan')
            d9_preimage = {
                'projects': state['retirement_preimage']['projects'],
                'database_schema': state['retirement_preimage']['database_schema'],
                'sqlite': state['retirement_preimage']['sqlite'],
                'retirement': state['retirement_preimage']['members'],
                'canonical': state['canonical_preimage']['members'],
            }
            if state['phase'] == 'rolled_back':
                current = _portfolio_read_state(parsed)
                if not journal.state.get('effects'):
                    _portfolio_preimage_gate(parsed, current)
                else:
                    _portfolio_d9_post_gate(parsed, current, d9_preimage)
                    _portfolio_restore_state_gate(parsed, current, journal)
                    for item in parsed['canonical_workflows']:
                        wid = item['workflow_id']
                        preimage = state['canonical_preimage']['members'][wid]
                        start = _portfolio_forward_state(parsed, item, preimage, journal)
                        restore_stages = _portfolio_restore_stages(parsed, item, preimage, start)
                        expected = restore_stages[-1]['after'] if restore_stages else start
                        if canonical_sha256(current['canonical'][wid]) != canonical_sha256(expected):
                            raise EffectBlocked('rolled_back canonical restore state drifted')
                return _release_live_report(plan, 'rolled_back', journal)
            current = _portfolio_read_state(parsed)
            _portfolio_d9_post_gate(parsed, current, d9_preimage)
            for item in parsed['canonical_workflows']:
                wid = item['workflow_id']
                before = state['canonical_preimage']['members'][wid]
                forward, applied_count = _portfolio_forward_progress(parsed, item, before, journal)
                if applied_count == 0:
                    continue
                start = forward[applied_count - 1]['after']
                stages = _portfolio_restore_stages(parsed, item, before, start)
                for stage in stages:
                    effect_key = f'restore:{wid}:{stage["name"]}'
                    effect = (journal.state.get('effects') or {}).get(effect_key)
                    if isinstance(effect, dict) and effect.get('status') == 'verified':
                        continue
                    current_live = _portfolio_read_state(parsed)
                    _portfolio_d9_post_gate(parsed, current_live, d9_preimage)
                    _portfolio_restore_state_gate(
                        parsed, current_live, journal, (wid, stage['name'])
                    )
                    current_hash = canonical_sha256(current_live['canonical'][wid])
                    before_hash = canonical_sha256(stage['before'])
                    after_hash = canonical_sha256(stage['after'])
                    if current_hash == after_hash:
                        if not isinstance(effect, dict) or effect.get('status') == 'settled_preimage':
                            raise EffectBlocked('canonical rollback postimage lacks a journaled restore effect')
                        if effect.get('status') == 'verified':
                            continue
                    elif current_hash != before_hash:
                        raise EffectBlocked('canonical rollback source state drifted')
                    current_fn = lambda item=item: canonical_sha256(
                        _portfolio_read_state(parsed)['canonical'][item['workflow_id']]
                    )
                    settled = _settle_or_run(
                        journal, effect_key=effect_key,
                        command=stage['command'], cwd=item['project_root'],
                        preimage_sha256=before_hash, postimage_sha256=after_hash,
                        current_sha256=current_fn, stdin_bytes=stage['stdin'],
                    )
                    if settled == 'preimage':
                        return {
                            **_release_live_report(plan, 'canonical_in_progress', journal),
                            'settled': 'preimage',
                        }
                    if canonical_sha256(_portfolio_read_state(parsed)['canonical'][wid]) != after_hash:
                        raise EffectBlocked('canonical rollback postimage drifted')
                _portfolio_set_status(journal, wid, 'rolled_back')
            final = _portfolio_read_state(parsed)
            _portfolio_d9_post_gate(parsed, final, d9_preimage)
            _portfolio_restore_state_gate(parsed, final, journal)
            for item in parsed['canonical_workflows']:
                wid = item['workflow_id']
                preimage = state['canonical_preimage']['members'][wid]
                start = _portfolio_forward_state(parsed, item, preimage, journal)
                restore_stages = _portfolio_restore_stages(parsed, item, preimage, start)
                expected = restore_stages[-1]['after'] if restore_stages else start
                if canonical_sha256(final['canonical'][wid]) != canonical_sha256(expected):
                    raise EffectBlocked('canonical rollback did not reach the forward-restore target')
            journal.persist({**journal.state, 'phase': 'rolled_back'})
            return _release_live_report(plan, 'rolled_back', journal)
        if mode == 'retire':
            if state['phase'] not in {'prepared', 'retirement_in_progress'}:
                raise JournalError('retire requires a prepared or retirement-in-progress journal')
            if state['phase'] == 'prepared':
                live = _portfolio_read_state(parsed)
                _portfolio_preimage_gate(parsed, live)
                journal.persist({**state, 'phase': 'retirement_in_progress'})
            preimage = {
                'projects': state['retirement_preimage']['projects'],
                'database_schema': state['retirement_preimage']['database_schema'],
                'sqlite': state['retirement_preimage']['sqlite'],
                'retirement': state['retirement_preimage']['members'],
                'canonical': state['canonical_preimage']['members'],
            }
            for member in parsed['retirement_members']:
                wid = member['workflow_id']
                if _portfolio_member_status(journal.state, wid) == 'verified':
                    continue
                live = _portfolio_read_state(parsed)
                statuses = {
                    row['workflow_id']: _portfolio_member_status(journal.state, row['workflow_id'])
                    for row in parsed['retirement_members']
                }
                existing = (journal.state.get('effects') or {}).get(f'delete:{wid}')
                settling = wid if isinstance(existing, dict) else None
                _portfolio_retirement_gate(parsed, live, preimage, statuses, settling)
                before = preimage['retirement'][wid]
                after = _d9_expected_post(member, before)
                command = _kent_delete_command(parsed['kent'], wid, True)
                current = lambda member=member: _portfolio_read_state(parsed)['retirement'][member['workflow_id']]
                settled = _settle_or_run(
                    journal, effect_key=f'delete:{wid}', command=command, cwd=member['project_root'],
                    preimage_sha256=canonical_sha256(before), postimage_sha256=canonical_sha256(after),
                    current_sha256=lambda: canonical_sha256(current()),
                )
                if settled == 'preimage':
                    return {**_release_live_report(plan, 'retirement_in_progress', journal), 'settled': 'preimage'}
                if canonical_sha256(current()) != canonical_sha256(after):
                    raise EffectBlocked('retirement member postimage drifted')
                _portfolio_set_status(journal, wid, 'verified')
            final = _portfolio_read_state(parsed)
            expected = _portfolio_expected_post(parsed, preimage)
            if not _portfolio_state_matches(final, expected):
                raise EffectBlocked('D9 complete poststate is not exact')
            journal.persist({**journal.state, 'phase': 'd9_complete'})
            return _release_live_report(plan, 'd9_complete', journal)
        if mode == 'apply':
            if state['phase'] not in {'d9_complete', 'canonical_in_progress'}:
                raise JournalError('apply requires d9_complete or canonical_in_progress')
            _portfolio_apply_direction_guard(parsed, state)
            d9_preimage = {
                'projects': state['retirement_preimage']['projects'],
                'database_schema': state['retirement_preimage']['database_schema'],
                'sqlite': state['retirement_preimage']['sqlite'], 'retirement': state['retirement_preimage']['members'],
                'canonical': state['canonical_preimage']['members'],
            }
            _portfolio_d9_post_gate(parsed, _portfolio_read_state(parsed), d9_preimage)
            journal.persist({**state, 'phase': 'canonical_in_progress'})
            for item in parsed['canonical_workflows']:
                wid = item['workflow_id']
                before = state['canonical_preimage']['members'][wid]
                stages = _canonical_progress(
                    {'kent': parsed['kent'], 'project_root': item['project_root']}, item, before
                )
                status = _portfolio_member_status(journal.state, wid)
                stage_statuses = [stage['status'] for stage in stages]
                status_index = stage_statuses.index(status) if status in stage_statuses else -1
                for stage_index, stage in enumerate(stages):
                    if status == 'verified' or stage_index <= status_index:
                        continue
                    current_state = _portfolio_read_state(parsed)
                    _portfolio_d9_post_gate(parsed, current_state, d9_preimage)
                    _portfolio_canonical_state_gate(parsed, current_state, journal, (wid, stage['name']))
                    live = current_state['canonical'][wid]
                    expected_before = stage['before']
                    existing = (journal.state.get('effects') or {}).get(f'apply:{wid}:{stage["name"]}')
                    if canonical_sha256(live) not in {
                        canonical_sha256(expected_before),
                        canonical_sha256(stage['after']) if isinstance(existing, dict) else '',
                    }:
                        raise EffectBlocked('canonical pre-effect state drifted')
                    current_fn = lambda item=item: canonical_sha256(
                        _portfolio_read_state(parsed)['canonical'][item['workflow_id']]
                    )
                    settled = _settle_or_run(
                        journal, effect_key=f'apply:{wid}:{stage["name"]}',
                        command=stage['command'], cwd=item['project_root'],
                        preimage_sha256=canonical_sha256(stage['before']),
                        postimage_sha256=canonical_sha256(stage['after']),
                        current_sha256=current_fn, stdin_bytes=stage['stdin'],
                    )
                    if settled == 'preimage':
                        return {**_release_live_report(plan, 'canonical_in_progress', journal), 'settled': 'preimage'}
                    if not _portfolio_state_matches(_portfolio_read_state(parsed)['canonical'][wid], stage['after']):
                        raise EffectBlocked('canonical stage postimage drifted')
                    _portfolio_set_status(journal, wid, stage['status'])
                    status = stage['status']
                _portfolio_set_status(journal, wid, 'verified')
            final = _portfolio_read_state(parsed)
            _portfolio_d9_post_gate(parsed, final, d9_preimage)
            for item in parsed['canonical_workflows']:
                live = final['canonical'][item['workflow_id']]
                target = state['canonical_targets'][item['workflow_id']]['target_sha256']
                if canonical_sha256(live) != target:
                    raise EffectBlocked('canonical target postimage is not exact')
            journal.persist({**journal.state, 'phase': 'complete'})
            return _release_live_report(plan, 'complete', journal)
        raise OperationError('unreachable release-live-portfolio mode')

def _activation_lstat(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        target = os.readlink(path)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return {'kind': 'symlink', 'target': target, 'sha256': None}
        digest = sha256_bytes(resolved.read_bytes()) if resolved.is_file() else None
        return {'kind': 'symlink', 'target': target, 'sha256': digest}
    if path.is_file():
        return {'kind': 'file', 'target': None, 'sha256': sha256_bytes(path.read_bytes())}
    if path.exists():
        return {'kind': 'directory', 'target': None, 'sha256': None}
    return {'kind': 'absent', 'target': None, 'sha256': None}

def _validate_activation_plan(plan: LoadedPlan) -> dict[str, Any]:
    data = _closed(plan.value, {'schema', 'state_dir', 'primary_root', 'branch', 'baseline_commit', 'target_commit',
            'role', 'git_config_allowlist', 'tracking', 'installed_links', 'prompt_prestate', 'backups',
            'source_prompt_sha256'}, 'activation plan')
    branch = _string(_required(data, 'branch', 'activation plan'), 'branch')
    if branch != 'main':
        raise PlanValidationError('primary activation is restricted to main')
    baseline = _sha1(_required(data, 'baseline_commit', 'activation plan'), 'baseline_commit')
    target = _sha1(_required(data, 'target_commit', 'activation plan'), 'target_commit')
    if baseline == target:
        raise PlanValidationError('primary activation target must advance main')
    state_dir = _absolute_path(_required(data, 'state_dir', 'activation plan'), 'state_dir')
    primary_root = _absolute_path(_required(data, 'primary_root', 'activation plan'), 'primary_root')
    role_raw = _closed(_required(data, 'role', 'activation plan'), {'prompt_path', 'config_path', 'kit_prompt_path',
            'expected_prompt_sha256'}, 'role')
    role = {key: str(_absolute_path(_required(role_raw, key, 'role'), f'role.{key}')) for key in ('prompt_path',
            'config_path', 'kit_prompt_path')}
    role['expected_prompt_sha256'] = _sha256(_required(role_raw, 'expected_prompt_sha256', 'role'),
            'role.expected_prompt_sha256')
    source_sha = _sha256(_required(data, 'source_prompt_sha256', 'activation plan'), 'source_prompt_sha256')
    if source_sha != role['expected_prompt_sha256']:
        raise PlanValidationError('source and expected prompt digests must match')
    config_rows: list[dict[str, str]] = []
    for index, raw in enumerate(_bounded_list(_required(data, 'git_config_allowlist', 'activation plan'),
            'git_config_allowlist')):
        row = _closed(raw, {'scope', 'key', 'value'}, f'git_config_allowlist[{index}]')
        scope = _string(_required(row, 'scope', 'Git config'), 'Git config scope')
        if scope not in {'local', 'worktree'}:
            raise PlanValidationError('Git config scope is unsupported')
        config_rows.append({'scope': scope, 'key': _string(_required(row, 'key', 'Git config'), 'Git config key'),
                'value': _string(_required(row, 'value', 'Git config'), 'Git config value', nonempty=False)})
    config_rows.sort(key=lambda row: (row['scope'], row['key'], row['value']))
    tracking_raw = _closed(_required(data, 'tracking', 'activation plan'), {'remote', 'merge'}, 'tracking')
    tracking = {'remote': _string(_required(tracking_raw, 'remote', 'tracking'), 'tracking.remote'), 'merge':
            _string(_required(tracking_raw, 'merge', 'tracking'), 'tracking.merge')}
    links: list[dict[str, str]] = []
    for index, raw in enumerate(_bounded_list(_required(data, 'installed_links', 'activation plan'),
            'installed_links')):
        link = _closed(raw, {'path', 'target'}, f'installed_links[{index}]')
        links.append({'path': str(_absolute_path(_required(link, 'path', 'link'), 'link.path')), 'target':
                str(_absolute_path(_required(link, 'target', 'link'), 'link.target'))})
    links.sort(key=lambda link: link['path'])
    _unique([link['path'] for link in links], 'installed link paths')
    prestate_raw = _closed(_required(data, 'prompt_prestate', 'activation plan'), {'kind', 'target', 'sha256'},
            'prompt_prestate')
    prestate = {'kind': _string(_required(prestate_raw, 'kind', 'prompt_prestate'), 'prompt kind'), 'target':
            _required(prestate_raw, 'target', 'prompt_prestate'), 'sha256': _required(prestate_raw, 'sha256',
            'prompt_prestate')}
    if prestate['kind'] not in {'absent', 'file', 'symlink'}:
        raise PlanValidationError('prompt_prestate.kind is unsupported')
    if prestate['target'] is not None:
        prestate['target'] = _string(prestate['target'], 'prompt_prestate.target')
    if prestate['sha256'] is not None:
        prestate['sha256'] = _sha256(prestate['sha256'], 'prompt_prestate.sha256')
    if prestate['kind'] == 'file' and prestate != {'kind': 'file', 'target': None, 'sha256':
            role['expected_prompt_sha256']}:
        raise PlanValidationError('regular prompt prestate is not byte-identical')
    if prestate['kind'] == 'absent' and any((prestate[key] is not None for key in ('target', 'sha256'))):
        raise PlanValidationError('absent prompt prestate carries identity')
    if prestate['kind'] == 'symlink' and (prestate['target'] != role['kit_prompt_path'] or prestate['sha256'] !=
            source_sha):
        raise PlanValidationError('symlink prompt prestate is not the exact Kit source')
    backup_raw = _closed(_required(data, 'backups', 'activation plan'), {'path', 'kind', 'sha256'}, 'backups')
    backup = {'path': str(_absolute_path(_required(backup_raw, 'path', 'backups'), 'backups.path')), 'kind':
            _string(_required(backup_raw, 'kind', 'backups'), 'backups.kind'), 'sha256': _required(backup_raw, 'sha256',
            'backups')}
    if backup['kind'] != 'absent' or backup['sha256'] is not None:
        raise PlanValidationError('activation backup must be initially absent')
    return {'state_dir': state_dir, 'primary_root': primary_root, 'branch': branch, 'baseline_commit': baseline,
            'target_commit': target, 'role': role, 'git_config_allowlist': config_rows, 'tracking': tracking,
            'installed_links': links, 'prompt_prestate': prestate, 'backups': backup, 'source_prompt_sha256':
            source_sha}
_GIT_EFFECT_ENV = {'GIT_TERMINAL_PROMPT': '0', 'GIT_PAGER': 'cat', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL':
        os.devnull}

def _git_config_scope(root: Path, scope: str) -> list[dict[str, str]]:
    command = ['/usr/bin/git', '-C', str(root), 'config', f'--{scope}', '--null', '--list']
    code, out, err = _run(command, cwd=root, extra_env=_GIT_EFFECT_ENV)
    if code:
        text = err.decode(errors='replace')
        if scope == 'worktree' and 'worktreeConfig is enabled' in text:
            return []
        raise OperationError(text or f'Git {scope} config read failed')
    rows: list[dict[str, str]] = []
    for raw in (record for record in out.split(b'\x00') if record):
        key, separator, value = raw.partition(b'\n')
        if not separator:
            raise OperationError('Git config inventory is malformed')
        try:
            rows.append({'scope': scope, 'key': key.decode('utf-8').lower(), 'value': value.decode('utf-8')})
        except UnicodeDecodeError as error:
            raise OperationError('Git config inventory is not UTF-8') from error
    return rows

def _git_config_inventory(root: Path) -> list[dict[str, str]]:
    rows = _git_config_scope(root, 'local') + _git_config_scope(root, 'worktree')
    rows.sort(key=lambda row: (row['scope'], row['key'], row['value']))
    dangerous = ('core.hookspath', 'core.askpass', 'core.fsmonitor', 'core.attributesfile', 'core.sparsecheckout',
            'credential.', 'filter.', 'pager.', 'maintenance.', 'include.', 'includeif.', 'diff.external', 'merge.',
            'checkout.')
    for row in rows:
        if row['value'] and any((row['key'] == prefix or row['key'].startswith(prefix) for prefix in dangerous)):
            raise OperationError(f"unsafe Git configuration: {row['key']}")
    return rows

def _tracking_state(root: Path) -> dict[str, str]:
    values = {}
    for key in ('remote', 'merge'):
        value = _git(root, 'config', '--local', '--get-all', f'branch.main.{key}')
        rows = value.splitlines()
        if len(rows) != 1 or not rows[0]:
            raise OperationError('main tracking configuration is absent or duplicated')
        values[key] = rows[0]
    return values

def _activation_git_snapshot(data: Mapping[str, Any]) -> dict[str, Any]:
    root = data['primary_root']
    if not root.is_dir() or root.is_symlink():
        raise OperationError('primary checkout root is unsafe')
    if Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() != root.resolve():
        raise OperationError('primary checkout root mismatch')
    if _git(root, 'branch', '--show-current') != data['branch']:
        raise OperationError('primary checkout branch drifted')
    if _git(root, 'status', '--porcelain=v1', '--untracked-files=all'):
        raise OperationError('primary checkout is dirty')
    _git(root, 'cat-file', '-e', f"{data['target_commit']}^{{commit}}")
    _git(root, 'merge-base', '--is-ancestor', data['baseline_commit'], data['target_commit'])
    tracking = _tracking_state(root)
    if tracking != data['tracking']:
        raise OperationError('main tracking configuration drifted')
    config = _git_config_inventory(root)
    if canonical_sha256(config) != canonical_sha256(data['git_config_allowlist']):
        raise OperationError('complete Git configuration inventory drifted')
    return {'root': str(root), 'branch': data['branch'], 'head': _git(root, 'rev-parse', 'HEAD'), 'main_ref': _git(root,
            'rev-parse', f"refs/heads/{data['branch']}"), 'tracking': tracking, 'config': config}

def _role_config_state(data: Mapping[str, Any]) -> dict[str, Any]:
    import tomllib
    role = data['role']
    config = Path(role['config_path'])
    if config.is_symlink() or not config.is_file():
        raise OperationError('role configuration is absent or unsafe')
    try:
        parsed = tomllib.loads(config.read_text())
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise OperationError('role configuration is invalid') from error
    subagents = parsed.get('subagents') if isinstance(parsed, dict) else None
    registration = subagents.get('release-decision') if isinstance(subagents, dict) else None
    if not isinstance(registration, dict):
        raise OperationError('release-decision role is not registered')
    prompt_file = registration.get('system_prompt_file')
    if not isinstance(prompt_file, str):
        raise OperationError('release-decision prompt registration is absent')
    registered_prompt = (config.parent / prompt_file).resolve(strict=False)
    if registered_prompt != Path(role['prompt_path']).resolve(strict=False):
        raise OperationError('release-decision prompt registration drifted')
    for key in ('agent_callable', 'workflow_subagent'):
        if registration.get(key) is not False:
            raise OperationError(f'release-decision {key} must be disabled')
    tools = registration.get('tools')
    if not isinstance(tools, dict) or any((tools.get(key) is not False for key in ('shell', 'patch', 'edit'))):
        raise OperationError('release-decision mutation tools must be disabled')
    return {'sha256': sha256_bytes(config.read_bytes()), 'prompt_file': prompt_file, 'agent_callable': False,
            'workflow_subagent': False, 'tools': {key: False for key in ('shell', 'patch', 'edit')}}

def _installed_link_state(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for link in data['installed_links']:
        path = Path(link['path'])
        target = Path(link['target'])
        if not path.is_symlink() or not target.exists():
            raise OperationError('installed Kit link is absent or dangling')
        try:
            actual = path.resolve(strict=True)
            expected = target.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise OperationError('installed Kit link is absent or dangling') from error
        if actual != expected:
            raise OperationError('installed Kit link target drifted')
        target_state = _activation_lstat(target)
        if target_state['kind'] not in {'file', 'directory'}:
            raise OperationError('installed Kit link target is unsafe')
        result.append({**link, 'target_state': target_state})
    return result

def _activation_file_snapshot(data: Mapping[str, Any], phase: str) -> dict[str, Any]:
    role = data['role']
    source = Path(role['kit_prompt_path'])
    if source.is_symlink() or not source.is_file():
        raise OperationError('Kit source prompt is absent or unsafe')
    if sha256_bytes(source.read_bytes()) != data['source_prompt_sha256']:
        raise OperationError('Kit source prompt digest drifted')
    prompt = _activation_lstat(Path(role['prompt_path']))
    backup = _activation_lstat(Path(data['backups']['path']))
    initial_prompt = data['prompt_prestate']
    initial_backup = {'kind': data['backups']['kind'], 'target': None, 'sha256': data['backups']['sha256']}
    final_prompt = {'kind': 'symlink', 'target': role['kit_prompt_path'], 'sha256': data['source_prompt_sha256']}
    final_backup = {'kind': 'file', 'target': None, 'sha256': role['expected_prompt_sha256']} if \
            initial_prompt['kind'] == 'file' else initial_backup
    if phase == 'baseline':
        allowed = {(canonical_sha256(initial_prompt), canonical_sha256(initial_backup))}
    elif phase == 'promoted':
        states = [(initial_prompt, initial_backup), (final_prompt, final_backup)]
        if initial_prompt['kind'] == 'file':
            states.extend([(initial_prompt, final_backup), ({'kind': 'absent', 'target': None, 'sha256': None},
                    final_backup)])
        allowed = {(canonical_sha256(left), canonical_sha256(right)) for left, right in states}
    elif phase == 'final':
        allowed = {(canonical_sha256(final_prompt), canonical_sha256(final_backup))}
    else:
        raise OperationError('activation file phase is invalid')
    if (canonical_sha256(prompt), canonical_sha256(backup)) not in allowed:
        raise OperationError(f'activation prompt/backup state drifted in {phase}')
    return {'source_prompt_sha256': data['source_prompt_sha256'], 'prompt': prompt, 'backup': backup, 'links':
            _installed_link_state(data), 'role_config': _role_config_state(data)}

def _activation_bound_files(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise JournalError('activation prepared file authority is invalid')
    return {key: _required(value, key, 'activation prepared files') for key in ('source_prompt_sha256', 'links',
            'role_config')}

def _activation_preflight(data: Mapping[str, Any], expected_head: str, file_phase: str, prepared_files: Mapping[str,
        Any] | None=None) -> dict[str, Any]:
    git = _activation_git_snapshot(data)
    if git['head'] != expected_head or git['main_ref'] != expected_head:
        raise OperationError('primary checkout identity does not match the phase')
    files = _activation_file_snapshot(data, file_phase)
    if prepared_files is not None and canonical_sha256(_activation_bound_files(files)) != \
            canonical_sha256(_activation_bound_files(prepared_files)):
        raise EffectBlocked('activation bound file authority drifted')
    return {'git': git, 'files': files}

def _activation_merge_command(data: Mapping[str, Any]) -> list[str]:
    return ['/usr/bin/git', '-C', str(data['primary_root']), '-c', 'core.hooksPath=/dev/null', '-c',
            'credential.helper=', '-c', 'maintenance.auto=false', 'merge', '--ff-only', data['target_commit']]

def _adopt_release_prompt(data: Mapping[str, Any]) -> None:
    _activation_file_snapshot(data, 'promoted')
    role = data['role']
    prompt = Path(role['prompt_path'])
    source = Path(role['kit_prompt_path'])
    backup = Path(data['backups']['path'])
    prompt_state = _activation_lstat(prompt)
    backup_state = _activation_lstat(backup)
    if data['prompt_prestate']['kind'] == 'file':
        expected_file = {'kind': 'file', 'target': None, 'sha256': role['expected_prompt_sha256']}
        if backup_state['kind'] == 'absent' and prompt_state == expected_file:
            os.link(prompt, backup, follow_symlinks=False)
            _fsync_directory(prompt.parent)
            backup_state = _activation_lstat(backup)
        if backup_state != expected_file:
            raise OperationError('release-decision backup is not exact')
        if prompt_state == expected_file:
            os.unlink(prompt)
            _fsync_directory(prompt.parent)
            prompt_state = _activation_lstat(prompt)
    if prompt_state['kind'] == 'absent':
        if not prompt.parent.is_dir() or prompt.parent.is_symlink():
            raise OperationError('release-decision prompt parent is unsafe')
        os.symlink(str(source), prompt)
        _fsync_directory(prompt.parent)
    _activation_file_snapshot(data, 'final')

def _activation_report(plan: LoadedPlan, journal: OperationJournal, phase: str) -> dict[str, Any]:
    return {'schema': 'kit-primary-activation-report-v2', 'plan_sha256': plan.sha256, 'phase': phase,
            'effects_released': _effect_attempts(journal.state or {})}

def activate_primary_checkout(plan: LoadedPlan, *, mode: str, confirm: bool | str=False) -> dict[str, Any]:
    data = _validate_activation_plan(plan)
    if mode not in {'preview', 'apply', 'rollback'}:
        raise PlanValidationError('activation mode is unsupported')
    if mode == 'preview':
        return {'schema': 'kit-primary-activation-report-v2', 'plan_sha256': plan.sha256, 'phase': 'preview',
                'preflight': _activation_preflight(data, data['baseline_commit'], 'baseline'), 'effects_released': 0}
    if confirm not in (True, plan.sha256):
        raise PlanValidationError('activation mutation requires confirmation')
    with OperationJournal(data['state_dir'], 'kit-primary-activation', plan) as journal:
        if journal.state is None:
            preflight = _activation_preflight(data, data['baseline_commit'], 'baseline')
            journal.persist({'phase': 'prepared', 'preflight': preflight, 'effects': {}})
        if mode == 'rollback':
            state = journal.require_phase({'prepared'})
            current = _activation_preflight(data, data['baseline_commit'], 'baseline')
            if canonical_sha256(current) != canonical_sha256(state['preflight']):
                raise EffectBlocked('activation prepared preimage drifted')
            journal.persist({**state, 'phase': 'rolled_back'})
            return _activation_report(plan, journal, 'rolled_back')
        state = journal.require_phase({'prepared', 'activation_committed', 'primary_promoted', 'role_adopted',
                'verified'})
        if state['phase'] == 'prepared':
            current = _activation_preflight(data, data['baseline_commit'], 'baseline')
            if canonical_sha256(current) != canonical_sha256(state['preflight']):
                raise EffectBlocked('activation prepared preimage drifted')
            journal.persist({**state, 'phase': 'activation_committed'})
        if journal.state['phase'] == 'activation_committed':
            pre_git = journal.state['preflight']['git']
            post_git = {**pre_git, 'head': data['target_commit'], 'main_ref': data['target_commit']}
            preimage = canonical_sha256(pre_git)
            postimage = canonical_sha256(post_git)
            command = _activation_merge_command(data)

            def current() -> str:
                return canonical_sha256(_activation_git_snapshot(data))
            entry = (journal.state.get('effects') or {}).get('primary-merge')
            current_digest = current()
            if not isinstance(entry, dict) or entry.get('status') == 'settled_preimage':
                if current_digest != preimage:
                    raise EffectBlocked('new activation attempt is not at exact baseline')
                retry = _activation_preflight(data, data['baseline_commit'], 'baseline')
                if canonical_sha256(retry) != canonical_sha256(journal.state['preflight']):
                    raise EffectBlocked('activation prepared preimage drifted')
            elif current_digest not in {preimage, postimage}:
                raise EffectBlocked('primary activation state is ambiguous')
            settled = _settle_or_run(journal, effect_key='primary-merge', command=command, cwd=data['primary_root'],
                    preimage_sha256=preimage, postimage_sha256=postimage, current_sha256=current,
                    extra_env=_GIT_EFFECT_ENV)
            if settled == 'preimage':
                return {**_activation_report(plan, journal, 'activation_committed'), 'settled': 'preimage'}
            prepared_files = journal.state['preflight']['files']
            _activation_preflight(data, data['target_commit'], 'promoted', prepared_files)
            journal.persist({**journal.state, 'phase': 'primary_promoted'})
        if journal.state['phase'] == 'primary_promoted':
            prepared_files = journal.state['preflight']['files']
            _activation_preflight(data, data['target_commit'], 'promoted', prepared_files)
            _adopt_release_prompt(data)
            _activation_preflight(data, data['target_commit'], 'final', prepared_files)
            journal.persist({**journal.state, 'phase': 'role_adopted'})
        if journal.state['phase'] in {'role_adopted', 'verified'}:
            _activation_preflight(data, data['target_commit'], 'final', journal.state['preflight']['files'])
            journal.persist({**journal.state, 'phase': 'verified'})
        return _activation_report(plan, journal, 'verified')

def main_guardian(argv: Sequence[str]) -> int:
    del argv
    return 2
