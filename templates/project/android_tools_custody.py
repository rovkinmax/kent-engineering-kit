"""Public borrowed-device custody API; no lease ownership or authentication policy.

Import this module from a byte-qualified android-tools bundle. Call check() before
device boundaries and during waits. begin_remote_execution() persists one exclusive
uncertainty marker before dispatch; only positive caller-owned remote termination
verification authorizes confirm_remote_stopped(). Context/host exit never does.
The marker blocks consumers, not owner release; it is not quarantine. Existing
owner bindings use custody marker protocol 1. New stop provenance is recorded in
the existing status file; legacy or malformed status is not upgraded.
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
MARKER_REASONS = REASON_CODES | frozenset({"wait_timeout", "signal_error"})
PARENT_STATES = frozenset({"not_started", "pending", "confirmed", "unconfirmed"})
STATUS_PHASES = frozenset({"preparing", "acquiring", "active", "stopped", "failed", "finished"})


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


def _owner_binding(run: Path, config: LeaseConfig | None = None) -> dict[str, Any]:
    private(run, directory=True)
    binding = read_private(run / "binding.json")
    lease_config = binding.get("config")
    profile = binding.get("profile")
    owner_binding = binding.get("binding")
    try:
        parsed = LeaseConfig.from_mapping(lease_config)
    except (Error, TypeError):
        raise Error("lease_binding_invalid") from None
    if (set(binding) != {
                "schema", "config", "profile", "custody_marker_protocol",
                "capability", "owner", "binding"}
            or type(binding.get("schema")) is not int or binding.get("schema") != 1
            or type(binding.get("custody_marker_protocol")) is not int
            or binding["custody_marker_protocol"] != CUSTODY_MARKER_PROTOCOL
            or not isinstance(profile, str)
            or dict(parsed.profiles).get(profile) != binding.get("capability")
            or not isinstance(binding.get("owner"), str) or not binding["owner"]
            or owner_binding is not None and (
                not isinstance(owner_binding, dict)
                or set(owner_binding) != {"project", "task", "session"}
                or any(not isinstance(owner_binding[key], str) or not owner_binding[key]
                       for key in ("project", "task", "session")))
            or run.parent != Path(parsed.runs_directory)
            or config is not None and parsed != config):
        raise Error("lease_binding_invalid")
    return binding


def _owner_status(run: Path) -> dict[str, Any]:
    status = read_private(run / "status.json")
    updated_at = status.get("updated_at")
    required = {"phase", "updated_at", "custody_marker_protocol",
                "parent_command_stop", "external_stop_unconfirmed",
                "command_stop", "run_directory", "lease", "error"}
    if (set(status) != required
            or status.get("phase") not in STATUS_PHASES
            or type(status.get("custody_marker_protocol")) is not int
            or status["custody_marker_protocol"] != CUSTODY_MARKER_PROTOCOL
            or status.get("parent_command_stop") not in PARENT_STATES
            or type(status.get("external_stop_unconfirmed")) is not bool
            or status.get("command_stop") not in {"not_started", "confirmed", "unconfirmed"}
            or status.get("run_directory") != str(run)
            or status.get("lease") is not None and not isinstance(status["lease"], dict)
            or status.get("error") is not None and not isinstance(status["error"], str)):
        raise Error("owner_stop_status_required")
    if (type(updated_at) not in (int, float) or not math.isfinite(updated_at)
            or updated_at < 0):
        raise Error("lease_binding_invalid")
    return status


def _marker_value(run: Path) -> tuple[tuple[int, ...], bytes, dict[str, Any]] | None:
    path = run / MARKER
    if not (path.exists() or path.is_symlink()):
        return None
    identity, data = _marker_snapshot(path)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise Error("uncertainty_marker_changed") from None
    if (not isinstance(value, dict) or set(value) != {"schema", "reason", "operation"}
            or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("reason") not in MARKER_REASONS
            or not isinstance(value.get("operation"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["operation"])):
        raise Error("uncertainty_marker_changed")
    return identity, data, value


def _aggregate(parent_stop: str, external: bool, marker_present: bool) -> str:
    if parent_stop in {"pending", "unconfirmed"} or external or marker_present:
        return "unconfirmed"
    return "not_started" if parent_stop == "not_started" else "confirmed"


def _write_owner_status(run: Path, directory_fd: int, *, external_uncertain=False) -> dict[str, Any]:
    status = _owner_status(run)
    marker = _marker_value(run)
    external = status["external_stop_unconfirmed"] or external_uncertain
    phase = status["phase"]
    error = status.get("error")
    if external_uncertain:
        phase = "failed"
        message = "external command outcome is uncertain"
        if error is None:
            error = message
        elif message not in error:
            error = f"{message}; {error}"
    command_stop = _aggregate(status["parent_command_stop"], external, marker is not None)
    if command_stop == "unconfirmed" and phase == "finished":
        phase = "failed"
        message = "external command outcome is uncertain"
        if error is None:
            error = message
        elif message not in error:
            error = f"{message}; {error}"
    value = {**status, "phase": phase,
             "external_stop_unconfirmed": external,
             "command_stop": command_stop, "error": error}
    data = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(data) > LIMIT:
        raise Error("owner_stop_status_too_large")
    path = run / "status.json"
    temporary = run / f".status.json-{uuid.uuid4().hex}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.fsync(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def _record_external_uncertainty(run: Path, reason_code: str,
                                 expected_marker: tuple[tuple[int, ...], bytes] | None = None) -> None:
    if not isinstance(reason_code, str) or reason_code not in REASON_CODES:
        raise Error("invalid_uncertainty_reason")
    with _run_lock(run) as directory_fd:
        _owner_binding(run)
        marker = _marker_value(run)
        if expected_marker is not None and (
                marker is None or marker[:2] != expected_marker):
            raise Error("uncertainty_marker_changed")
        _write_owner_status(run, directory_fd, external_uncertain=True)


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

    def _check(self, *, allow_external_uncertainty: bool = False) -> dict[str, Any]:
        if self._closed:
            raise Error("custody_closed")
        private(self.run, directory=True)
        info = self.run.lstat()
        directory_identity = (info.st_dev, info.st_ino)
        binding = _owner_binding(self.run, self.config)
        state = _owner_status(self.run)
        private(self.run / "receipt.json")
        updated_at = state.get("updated_at")
        if (type(updated_at) not in (int, float) or not math.isfinite(updated_at)
                or not 0 <= time.monotonic() - updated_at <= 30):
            raise Error("lease_binding_invalid")
        if (state.get("parent_command_stop") == "unconfirmed"
                or state.get("external_stop_unconfirmed") and not allow_external_uncertainty
                or state.get("parent_command_stop") == "pending"
                and state.get("phase") != "active"
                and not (allow_external_uncertainty
                         and state.get("external_stop_unconfirmed")
                         and state.get("phase") == "failed")):
            raise Error("parent_command_stop_unconfirmed")
        marker = _marker_value(self.run)
        if ((marker is None and self._marker is not None)
                or (marker is not None and (
                    self._marker is None or marker[:2] != self._marker))):
            raise Error("uncertainty_marker_changed" if self._marker else "lease_binding_invalid")
        if ((state.get("phase") != "active"
             and not (allow_external_uncertainty
                      and state.get("external_stop_unconfirmed")
                      and state.get("phase") == "failed"))
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
        prior = state.get("lease")
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
        # Pool inspection can block while the owner publishes a finish/stop signal.
        # Re-read every local admission barrier after that external call.
        latest_binding = _owner_binding(self.run, self.config)
        latest_state = _owner_status(self.run)
        if latest_binding != binding or latest_state != state:
            raise Error("lease_binding_invalid")
        latest_marker = _marker_value(self.run)
        if (latest_state.get("parent_command_stop") == "unconfirmed"
                or latest_state.get("external_stop_unconfirmed") and not allow_external_uncertainty
                or latest_state.get("parent_command_stop") == "pending"
                and latest_state.get("phase") != "active"
                and not (allow_external_uncertainty
                         and latest_state.get("external_stop_unconfirmed")
                         and latest_state.get("phase") == "failed")
                or latest_marker != marker
                or any((self.run / name).exists() or (self.run / name).is_symlink()
                       for name in ("finish.json", "stop-renewal.json"))):
            raise Error("parent_command_stop_unconfirmed")
        self._binding = binding
        self._lease_identity = identity
        self._directory_identity = directory_identity
        return current

    def check(self) -> dict[str, Any]:
        with _run_lock(self.run):
            return self._check()

    def _begin_remote_execution(self, reason: str) -> None:
        with _run_lock(self.run) as fd:
            self._check()
            if self._marker is not None:
                raise Error("remote_execution_already_started")
            marker = self.run / MARKER
            if marker.exists() or marker.is_symlink():
                raise Error("uncertainty_marker_changed")
            self._marker = _create_marker(self.run, fd, reason)
            _write_owner_status(self.run, fd)
            self._check()

    def begin_remote_execution(self) -> None:
        """Durably publish the uncertainty marker before caller dispatch."""
        self._begin_remote_execution("remote_stop_unconfirmed")

    def confirm_remote_stopped(self) -> None:
        """Remove only this live context's unchanged marker after positive proof."""
        with _run_lock(self.run) as fd:
            if self._closed:
                raise Error("custody_closed")
            if self._marker is None:
                raise Error("remote_execution_not_started")
            self._check(allow_external_uncertainty=True)
            if _marker_snapshot(self.run / MARKER) != self._marker:
                raise Error("uncertainty_marker_changed")
            (self.run / MARKER).unlink()
            os.fsync(fd)
            self._marker = None
            _write_owner_status(self.run, fd)

    def handoff_uncertain(self, reason_code: str) -> None:
        """Persist a sticky negative independently of owner lease liveness."""
        if not isinstance(reason_code, str) or reason_code not in REASON_CODES:
            raise Error("invalid_uncertainty_reason")
        _record_external_uncertainty(self.run, reason_code, self._marker)


def borrow_device(lease_config: LeaseConfig | Mapping[str, Any], serial: str) -> DeviceCustody:
    return DeviceCustody(lease_config, serial)


def handoff_uncertain(reason_code: str) -> None:
    """Persist an external negative without requiring a live lease or helper."""
    if not isinstance(reason_code, str) or reason_code not in REASON_CODES:
        raise Error("invalid_uncertainty_reason")
    run = Path(os.environ.get("RESOURCE_LEASE_RUN_DIRECTORY", ""))
    if not run.is_absolute():
        raise Error("owned_run_required")
    _record_external_uncertainty(run, reason_code)
