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
SHA1_RE = '^[0-9a-f]{40}$'
SHA256_RE = '^[0-9a-f]{64}$'
UUID_RE = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'
MAX_LIST = 256
MAX_COMMAND = 64
MAX_OUTPUT = 256 * 1024
JOURNAL_SCHEMA = 'kit-operation-journal-v1'
PHASES = {'prepared', 'in_progress', 'complete', 'verified', 'activation_committed', 'primary_promoted', 'role_adopted',
        'rolled_back'}
JOURNAL_FIELDS = {'schema', 'operation', 'plan_sha256', 'phase', 'effects', 'members', 'preimage', 'inventory',
        'inventory_sha256', 'preflight'}

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

def load_plan(path: Path, *, schema: str, expected_sha256: str, mutation: bool=False, confirm: str | None=None) -> \
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
    if not isinstance(value, dict) or value.get('schema') != schema:
        raise PlanValidationError(f'expected schema {schema!r}')
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
        if state.get('phase') not in PHASES:
            raise JournalError('journal phase is invalid')
        if set(state) - JOURNAL_FIELDS:
            raise JournalError('journal contains unknown fields')
        if canonical_bytes(state) + b'\n' != raw:
            raise JournalError('journal readback is not canonical')
        return state

    def persist(self, state: Mapping[str, Any]) -> None:
        if self._lock_fd is None:
            raise JournalError('journal lock is not held')
        data = dict(state)
        data.update({'schema': JOURNAL_SCHEMA, 'operation': self.operation, 'plan_sha256': self.plan.sha256})
        if set(data) - JOURNAL_FIELDS:
            raise JournalError('journal contains unknown fields')
        if data.get('phase') not in PHASES:
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
import base64, json, os, selectors, signal, subprocess, sys
lock_fd = int(sys.argv[1])
report_fd = int(sys.argv[2])
control_fd = int(sys.argv[3])
command = json.loads(base64.b64decode(sys.argv[4]).decode())
cwd = sys.argv[5]
env = json.loads(base64.b64decode(sys.argv[6]).decode())
limit = int(sys.argv[7])
stdin_view = memoryview(base64.b64decode(sys.argv[8]))
gate_read, gate_write = os.pipe()
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        sys.argv[9],
        str(gate_read),
        base64.b64encode(json.dumps(command, separators=(",", ":")).encode()).decode(),
        cwd,
        base64.b64encode(json.dumps(env, separators=(",", ":")).encode()).decode(),
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
os.close(gate_read)
os.write(report_fd, (json.dumps({"pid": child.pid}) + "\n").encode())
os.close(report_fd)

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
if not os.read(control_fd, 1):
    stop(signal.SIGTERM, None)
os.close(control_fd)
os.write(gate_write, b"1")
os.close(gate_write)
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
        preimage_sha256: str | None, postimage_sha256: str | None) -> tuple[list[str], Path, dict[str, str], bytes,
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
    if len(stdin) > MAX_OUTPUT:
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
        stdin_bytes: bytes | None=None, current_sha256: Callable[[], str] | None=None) -> EffectResult:
    if timeout <= 0 or timeout > 300:
        raise PlanValidationError('effect timeout must be between 0 and 300 seconds')
    if not callable(current_sha256):
        raise PlanValidationError('effect requires an exact current-state readback')
    command, cwd, env, stdin_bytes, identity = _effect_inputs(command, cwd, extra_env, stdin_bytes, preimage_sha256,
            postimage_sha256)
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
    lock_fd = journal._lock_fd
    if lock_fd is None:
        raise JournalError('effect requires the held operation lock')
    read_fd, write_fd = os.pipe()
    control_read, control_write = os.pipe()
    guardian = subprocess.Popen([sys.executable, '-c', _GUARDIAN, str(lock_fd), str(write_fd), str(control_read),
            base64.b64encode(canonical_bytes(command)).decode(), str(cwd),
            base64.b64encode(canonical_bytes(env)).decode(), str(MAX_OUTPUT), base64.b64encode(stdin_bytes).decode(),
            _CHILD_GATE], cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(lock_fd,
            write_fd, control_read), start_new_session=True, close_fds=True)
    os.close(write_fd)
    os.close(control_read)
    child_pid: int | None = None
    acknowledgement_error: EffectBlocked | None = None
    acknowledgement = selectors.DefaultSelector()
    try:
        acknowledgement.register(read_fd, selectors.EVENT_READ)
        if not acknowledgement.select(timeout):
            acknowledgement_error = EffectBlocked('effect guardian acknowledgement timed out')
        else:
            data = os.read(read_fd, 4096)
            if not data:
                acknowledgement_error = EffectBlocked('effect guardian acknowledgement was lost')
            else:
                child_pid = int(json.loads(data.splitlines()[0].decode())['pid'])
                if not _pid_alive(child_pid):
                    acknowledgement_error = EffectBlocked('effect child is not alive after acknowledgement')
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        acknowledgement_error = EffectBlocked('effect guardian acknowledgement was invalid')
        acknowledgement_error.__cause__ = error
    finally:
        acknowledgement.close()
        os.close(read_fd)
    if acknowledgement_error is not None or child_pid is None:
        os.close(control_write)
        _terminate_owned(guardian)
        effects[effect_key]['status'] = 'unresolved'
        journal.persist({**journal.state, 'effects': effects})
        raise acknowledgement_error or EffectBlocked('effect guardian acknowledgement was not durable')
    effects[effect_key]['child'] = {'guardian_pid': guardian.pid, 'child_pid': child_pid}
    journal.persist({**journal.state, 'effects': effects})
    try:
        os.write(control_write, b'1')
    finally:
        os.close(control_write)
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
        None=None, stdin_bytes: bytes | None=None) -> str:
    if not callable(current_sha256):
        raise PlanValidationError('effect recovery requires an exact readback')
    _, _, _, _, identity = _effect_inputs(command, cwd, extra_env, stdin_bytes, preimage_sha256, postimage_sha256)
    state = journal.require_phase({'prepared', 'in_progress', 'activation_committed'})
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
        stdin_bytes: bytes | None=None, timeout: float=30.0) -> str:
    entry = (journal.state.get('effects') or {}).get(effect_key)
    if isinstance(entry, dict) and entry.get('status') == 'verified':
        if current_sha256() != postimage_sha256:
            raise EffectBlocked(f'verified effect {effect_key!r} postimage drifted')
        return 'postimage'
    if isinstance(entry, dict) and entry.get('status') in {'attempted', 'unresolved', 'ambiguous'}:
        return recover_effect(journal, effect_key=effect_key, command=command, cwd=cwd, preimage_sha256=preimage_sha256,
                postimage_sha256=postimage_sha256, current_sha256=current_sha256, extra_env=extra_env,
                stdin_bytes=stdin_bytes)
    return run_effect(journal, effect_key=effect_key, command=command, cwd=cwd, timeout=timeout,
            preimage_sha256=preimage_sha256, postimage_sha256=postimage_sha256, current_sha256=current_sha256,
            extra_env=extra_env, stdin_bytes=stdin_bytes).settlement or ''

def _run(command: Sequence[str], *, cwd: Path, timeout: float=30.0, extra_env: Mapping[str, str] | None=None) -> \
        tuple[int, bytes, bytes]:
    argv = _argv(list(command), 'internal command')
    if not Path(argv[0]).is_absolute():
        raise OperationError('internal command executable must be absolute')
    process = subprocess.Popen(argv, cwd=str(cwd), env=_safe_env(extra_env), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True)
    try:
        out, err = _bounded_communicate(process, timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait(timeout=2)
        raise OperationError('internal command timed out') from error
    return (process.returncode or 0, out, err)

def _json_command(command: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    code, out, err = _run(command, cwd=cwd)
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

def _kent_json(kent: Path, args: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    return _json_command([str(kent), *args], cwd=cwd)

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

def _session_manifest(path: Path) -> list[dict[str, Any]]:
    root = path
    if not root.is_dir() or root.is_symlink():
        raise OperationError('retained Session directory is absent or unsafe')
    result: list[dict[str, Any]] = []
    total_bytes = 0
    for entry in sorted(root.rglob('*')):
        relative = entry.relative_to(root)
        if any((part in {'.', '..'} for part in relative.parts)):
            raise OperationError('invalid Session manifest path')
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise OperationError('Session manifest refuses symlinks')
        kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
        record: dict[str, Any] = {'path': relative.as_posix(), 'type': kind, 'mode': stat.S_IMODE(info.st_mode),
                'bytes': info.st_size if kind == 'file' else 0}
        if kind == 'file':
            if info.st_size > MAX_OUTPUT:
                raise OperationError('Session manifest file exceeds the bound')
            total_bytes += info.st_size
            if total_bytes > MAX_OUTPUT:
                raise OperationError('Session manifest exceeds the byte bound')
            record['sha256'] = sha256_bytes(entry.read_bytes())
        result.append(record)
    if len(result) > MAX_LIST:
        raise OperationError('Session manifest is too large')
    return result

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

def _workflow_summary(value: Mapping[str, Any], workflow_id: str) -> dict[str, Any]:
    source = value.get('workflow') if isinstance(value.get('workflow'), dict) else value
    actual_id = source.get('id', source.get('workflow_id', workflow_id))
    version = source.get('version', source.get('revision'))
    if actual_id != workflow_id or not isinstance(version, int) or isinstance(version, bool):
        raise EffectBlocked('Workflow inspect identity/revision is malformed')
    return {'present': True, 'workflow_id': workflow_id, 'revision': version}

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
        digest = canonical_sha256(_session_manifest(path))
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
        inspect = _kent_optional(kent, ['workflow', 'inspect', wid, '--json'], cwd=cwd)
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
            if validation.get('valid') is False:
                raise EffectBlocked('Workflow validation failed')
            preview = _kent_json(kent, ['workflow', 'delete', wid, '--json'], cwd=cwd)
            preview_sha = preview.get('sha256') or canonical_sha256(preview)
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
        item['project_id'] = _uuid(_required(item, 'project_id', label), f'{label}.project_id')
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
            parsed_link = {'project_id': _uuid(_required(link, 'project_id', 'link'), 'link.project_id'), 'workflow_id':
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

def _canonical_metadata(value: Mapping[str, Any]) -> dict[str, str]:
    source = value.get('workflow') if isinstance(value.get('workflow'), dict) else value
    target = source.get('execution_target')
    policy = source.get('execution_target_policy')
    if target is None and isinstance(policy, dict):
        target = policy.get('mode')
    return {'name': _string(source.get('name'), 'Workflow name'), 'description': _string(source.get('description', ''),
            'Workflow description', nonempty=False), 'execution_target': _string(target, 'Workflow execution target')}

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

def _canonical_read(parsed: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    kent = parsed['kent']
    root = parsed['project_root']
    wid = item['workflow_id']
    project_id = item['project_id']
    project_rows = _project_rows(kent, root)
    projects = [row for row in project_rows if row['id'] == project_id]
    if len(projects) != 1 or Path(projects[0]['path']).resolve() != root.resolve():
        raise EffectBlocked('canonical project identity/root drifted')
    pages = _kent_pages(kent, ['workflow', 'list', '--project', project_id], cwd=root)
    links, default, listed_version = _workflow_link_state(_page_rows(pages, ('items', 'workflows')), project_id, wid)
    if listed_version is None:
        raise EffectBlocked('canonical Workflow is absent from the project')
    inspect = _kent_json(kent, ['workflow', 'inspect', wid, '--json'], cwd=root)
    summary = _workflow_summary(inspect, wid)
    if summary['revision'] != listed_version:
        raise EffectBlocked('canonical list/inspect revisions disagree')
    graph_raw = _kent_json(kent, ['workflow', 'graph', 'inspect', wid, '--json'], cwd=root)
    graph = _canonical_graph(graph_raw, listed_version)
    validation = _kent_json(kent, ['workflow', 'validate', wid, '--json'], cwd=root)
    if validation.get('valid') is False or validation.get('errors'):
        raise EffectBlocked('canonical Workflow validation failed')
    task_pages = _kent_pages(kent, ['task', 'list', '--project', project_id, '--workflow', wid], cwd=root)
    task_rows = _page_rows(task_pages, ('items', 'tasks'))
    tasks = sorted((_task_state(row) for row in task_rows), key=lambda row: row['id'])
    details: list[dict[str, Any]] = []
    anchor_ids: set[str] = set()
    for task in tasks:
        detail = _kent_json(kent, ['task', 'show', task['id'], '--project', project_id, '--json'], cwd=root)
        details.append(_task_state(detail, task['id']))
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
    return {'workflow_id': wid, 'project_id': project_id, 'version': listed_version, 'metadata':
            _canonical_metadata(inspect), 'graph': graph, 'tasks': tasks, 'task_details': details, 'terminal_anchors':
            anchors, 'links': links, 'default': default, 'valid': True}

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
        None=None) -> dict[str, Any]:
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
