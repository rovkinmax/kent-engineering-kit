"""Public borrowed-device custody API; no lease ownership or authentication policy.

Import this module from a byte-qualified android-tools bundle. Call check() before
device boundaries and during waits. begin_remote_execution() persists an exclusive
uncertainty marker before dispatch; only positive caller-owned remote termination
verification authorizes confirm_remote_stopped(). Context/host exit never does.
The marker blocks consumers, not the native owner's release: it is NOT quarantine.
Remote BEGIN requires protocol 1 in both immutable owner binding and fresh
runtime status. Legacy ordinary checks remain valid, but source installation
cannot upgrade a retained owner; start a qualified new owner and new run.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time
from typing import Any, Iterator, Mapping
import uuid

BUNDLE_PROTOCOL = 1
CUSTODY_API_VERSION = 1
CUSTODY_MARKER_PROTOCOL = 1
MARKER_LOCK_TIMEOUT_SECONDS = 10.0
LIMIT = 262144
MARKER = "command-stop-uncertain.json"
REASON_CODES = frozenset({
    "native_runner_drain_unconfirmed", "remote_stop_unconfirmed",
    "host_interrupted", "remote_execution_uncertain",
})


class Error(ValueError):
    """Categorical, nonsecret custody rejection."""


def private(path: Path, directory: bool = False) -> None:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise Error("private_binding_required")


def read_private(path: Path) -> dict[str, Any]:
    private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise Error("private_binding_required")
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise Error("binding_too_large")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise Error("invalid_binding") from None
    if not isinstance(value, dict):
        raise Error("invalid_binding")
    return value


@dataclass(frozen=True)
class LeaseConfig:
    """Public, validated schema-1 resource-lease configuration."""

    executable: str
    pool: str
    runs_directory: str
    profiles: tuple[tuple[str, str], ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LeaseConfig:
        if (not isinstance(value, Mapping) or set(value) != {
                "schema", "executable", "pool", "runs_directory", "profiles",
        } or type(value["schema"]) is not int or value["schema"] != 1):
            raise Error("invalid_lease_configuration")
        for key in ("executable", "pool", "runs_directory"):
            if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
                raise Error("invalid_lease_configuration")
        profiles = value["profiles"]
        if (not isinstance(profiles, dict) or not profiles
                or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v
                       for k, v in profiles.items())):
            raise Error("invalid_lease_configuration")
        return cls(value["executable"], value["pool"], value["runs_directory"],
                   tuple(sorted(profiles.items())))

    def as_mapping(self) -> dict[str, Any]:
        return {"schema": 1, "executable": self.executable, "pool": self.pool,
                "runs_directory": self.runs_directory, "profiles": dict(self.profiles)}


def load_lease_config(path: Path) -> LeaseConfig:
    """Read the lease section of an owner-private android-tools JSON config."""
    return LeaseConfig.from_mapping(read_private(path).get("lease"))


def inspect_lease(command: list[str]) -> dict[str, Any]:
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = {child.stdout: bytearray(), child.stderr: bytearray()}
    deadline = time.monotonic() + 10
    try:
        with selectors.DefaultSelector() as selector:
            for stream in output:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise Error("lease_inspection_timeout")
                for key, _ in selector.select(0.1):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.fileobj].extend(data)
                        if sum(map(len, output.values())) > LIMIT:
                            raise Error("lease_inspection_output_limit")
        if child.wait() != 0:
            raise Error("lease_inspection_failed")
        try:
            value = json.loads(output[child.stdout])
        except (ValueError, UnicodeError):
            raise Error("lease_inspection_failed") from None
        if not isinstance(value, dict):
            raise Error("lease_inspection_failed")
        return value
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        for stream in output:
            stream.close()


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _run_lock(run: Path) -> Iterator[int]:
    """Serialize cooperative marker mutations without adding owner-run files."""
    private(run, directory=True)
    fd = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        deadline = time.monotonic() + MARKER_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Error("marker_lock_timeout") from None
                time.sleep(min(0.05, remaining))
        info = run.lstat()
        if (info.st_dev, info.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
            raise Error("lease_binding_invalid")
        yield fd
    finally:
        os.close(fd)


def _marker_snapshot(path: Path) -> tuple[tuple[int, ...], bytes]:
    private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(LIMIT + 1)
        after = os.fstat(stream.fileno())
    if (len(data) > LIMIT or _identity(before) != _identity(after)
            or _identity(after) != _identity(path.lstat())):
        raise Error("uncertainty_marker_changed")
    return _identity(after), data


def _create_marker(run: Path, directory_fd: int, reason: str) -> tuple[tuple[int, ...], bytes]:
    if not isinstance(reason, str) or reason not in REASON_CODES:
        raise Error("invalid_uncertainty_reason")
    payload = (json.dumps({"schema": 1, "reason": reason, "operation": uuid.uuid4().hex},
                          sort_keys=True) + "\n").encode()
    path = run / MARKER
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Error("lease_binding_invalid") from None
    with os.fdopen(fd, "w+b") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        if stream.read(LIMIT + 1) != payload:
            raise Error("uncertainty_marker_unconfirmed")
        created = _identity(os.fstat(stream.fileno()))
    os.fsync(directory_fd)
    snapshot = _marker_snapshot(path)
    if snapshot != (created, payload):
        raise Error("uncertainty_marker_changed")
    return snapshot


class DeviceCustody:
    """A borrow of the existing owner, with no acquire/renew/release effects."""

    def __init__(self, lease_config: LeaseConfig | Mapping[str, Any], serial: str):
        config = lease_config.as_mapping() if isinstance(lease_config, LeaseConfig) else lease_config
        self.config = LeaseConfig.from_mapping(config)
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", serial):
            raise Error("device_serial_required")
        self.serial = serial
        self.run = Path(os.environ.get("RESOURCE_LEASE_RUN_DIRECTORY", ""))
        if not self.run.is_absolute():
            raise Error("owned_run_required")
        self._marker: tuple[tuple[int, ...], bytes] | None = None
        self._binding: dict[str, Any] | None = None
        self._lease_identity: tuple[Any, ...] | None = None
        self._directory_identity: tuple[int, int] | None = None
        self._closed = False

    def __enter__(self) -> DeviceCustody:
        self.check()
        return self

    def __exit__(self, *exc: object) -> None:
        # Host/context exit is not evidence that remote execution stopped.
        self._closed = True

    def _check_marker(self) -> None:
        path = self.run / MARKER
        if self._marker is None:
            if path.exists() or path.is_symlink():
                raise Error("lease_binding_invalid")
        else:
            try:
                if _marker_snapshot(path) != self._marker:
                    raise Error("uncertainty_marker_changed")
            except (OSError, ValueError):
                raise Error("uncertainty_marker_changed") from None

    def _check(self, *, require_protocol: bool = False) -> dict[str, Any]:
        if self._closed:
            raise Error("custody_closed")
        private(self.run, directory=True)
        info = self.run.lstat()
        directory_identity = (info.st_dev, info.st_ino)
        binding = read_private(self.run / "binding.json")
        state = read_private(self.run / "status.json")
        private(self.run / "receipt.json")
        if require_protocol or self._marker is not None:
            for record in (binding, state):
                protocol = record.get("custody_marker_protocol")
                if type(protocol) is not int or protocol != CUSTODY_MARKER_PROTOCOL:
                    raise Error("owner_marker_protocol_required")
        updated_at = state.get("updated_at")
        if (type(updated_at) not in (int, float) or not math.isfinite(updated_at)
                or not 0 <= time.monotonic() - updated_at <= 30):
            raise Error("lease_binding_invalid")
        self._check_marker()
        profile = binding.get("profile")
        if (type(binding.get("schema")) is not int or binding.get("schema") != 1
                or binding.get("config") != self.config.as_mapping()
                or self.run.parent != Path(self.config.runs_directory)
                or not isinstance(profile, str)
                or dict(self.config.profiles).get(profile) != binding.get("capability")
                or not isinstance(binding.get("owner"), str) or not binding["owner"]
                or not binding.get("capability")
                or state.get("phase") != "active"
                or any((self.run / name).exists() or (self.run / name).is_symlink()
                       for name in ("finish.json", "stop-renewal.json"))
                or self._binding is not None and binding != self._binding
                or self._directory_identity is not None
                and directory_identity != self._directory_identity):
            raise Error("lease_binding_invalid")
        lock_path = self.run / "helper.lock"
        private(lock_path)
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise Error("lease_owner_not_running")
        finally:
            os.close(fd)
        current = inspect_lease([
            self.config.executable, "--root", self.config.pool, "inspect", "--owner",
            binding["owner"], "--lease", str(self.run / "receipt.json"),
        ])
        prior = state.get("lease") or {}
        if not isinstance(prior, dict):
            raise Error("lease_binding_invalid")
        endpoint = current.get("endpoint")
        remaining = current.get("expires_in")
        identity = tuple(current.get(key) for key in ("id", "generation", "slot"))
        if (current.get("state") != "active" or current.get("quarantined") is not False
                or current.get("service_available") is not True
                or current.get("owner") != binding["owner"]
                or current.get("capability") != binding["capability"]
                or not isinstance(endpoint, dict) or endpoint.get("adb_serial") != self.serial
                or type(remaining) not in (int, float) or not math.isfinite(remaining)
                or remaining <= 0
                or any(current.get(key) != prior.get(key) for key in ("id", "generation", "slot"))
                or any(current.get(key) is None for key in ("id", "generation", "slot"))
                or self._lease_identity is not None and identity != self._lease_identity):
            raise Error("lease_lost_or_foreign")
        # Native-owner stop markers may appear during the bounded CLI inspection.
        self._check_marker()
        if any((self.run / name).exists() or (self.run / name).is_symlink()
               for name in ("finish.json", "stop-renewal.json")):
            raise Error("lease_binding_invalid")
        self._binding = binding
        self._lease_identity = identity
        self._directory_identity = directory_identity
        return current

    def check(self) -> dict[str, Any]:
        with _run_lock(self.run):
            return self._check()

    def begin_remote_execution(self) -> None:
        with _run_lock(self.run) as fd:
            self._check(require_protocol=True)
            if self._marker is not None:
                raise Error("remote_execution_already_started")
            self._marker = _create_marker(self.run, fd, "remote_stop_unconfirmed")
            self._check()

    def confirm_remote_stopped(self) -> None:
        """Caller asserts positive remote-stop proof; never call for host exit alone."""
        with _run_lock(self.run) as fd:
            if self._marker is None:
                raise Error("remote_execution_not_started")
            self._check()
            if _marker_snapshot(self.run / MARKER) != self._marker:
                raise Error("uncertainty_marker_changed")
            (self.run / MARKER).unlink()
            os.fsync(fd)
            self._marker = None

    def handoff_uncertain(self, reason_code: str) -> None:
        """Retain this context's unchanged marker; never overwrite a foreign one."""
        if not isinstance(reason_code, str) or reason_code not in REASON_CODES:
            raise Error("invalid_uncertainty_reason")
        with _run_lock(self.run) as fd:
            if self._closed:
                raise Error("custody_closed")
            if self._marker is None:
                self._check(require_protocol=True)
                self._marker = _create_marker(self.run, fd, reason_code)
            elif _marker_snapshot(self.run / MARKER) != self._marker:
                raise Error("uncertainty_marker_changed")


def borrow_device(lease_config: LeaseConfig | Mapping[str, Any], serial: str) -> DeviceCustody:
    return DeviceCustody(lease_config, serial)


def handoff_uncertain(reason_code: str) -> None:
    """Legacy facade handoff after dispatch; preserve any existing private marker."""
    if not isinstance(reason_code, str) or reason_code not in REASON_CODES:
        raise Error("invalid_uncertainty_reason")
    run = Path(os.environ.get("RESOURCE_LEASE_RUN_DIRECTORY", ""))
    if not run.is_absolute():
        raise Error("owned_run_required")
    with _run_lock(run) as fd:
        path = run / MARKER
        if path.exists() or path.is_symlink():
            _marker_snapshot(path)
            return
        _create_marker(run, fd, reason_code)
