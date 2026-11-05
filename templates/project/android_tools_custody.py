"""Public borrowed-device custody API; no lease ownership or authentication policy.

Import this module from a byte-qualified android-tools bundle. Call check() before
device boundaries and during waits. begin_remote_execution() durably records one
operation and its exclusive uncertainty marker before dispatch; only positive
caller-owned remote termination verification authorizes confirm_remote_stopped().
Context/host exit never does. The marker blocks consumers, not owner release; it is
not quarantine. New operations require protocol 2 in binding and fresh status.
Legacy records are not upgraded by installation and require qualified recovery.
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
CUSTODY_API_VERSION = 2
CUSTODY_MARKER_PROTOCOL = 2
STOP_PROTOCOL = 2
MARKER_LOCK_TIMEOUT_SECONDS = 10.0
LIMIT = 262144
MARKER = "command-stop-uncertain.json"
REMOTE_STOP_RECORD = "remote-stop.json"
PARENT_STOP_MARKER = "parent-command-stop-uncertain.json"
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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


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
        value = json.loads(data, object_pairs_hook=_unique_object)
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


def _binding_digest(binding: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(_canonical_json(binding)).hexdigest()


def _read_remote_record(run: Path, binding: Mapping[str, Any]) -> dict[str, Any] | None:
    path = run / REMOTE_STOP_RECORD
    if not (path.exists() or path.is_symlink()):
        return None
    value = read_private(path)
    required = {"schema", "stop_protocol", "operation_id", "state", "run_directory",
                "binding_sha256", "lease_identity", "marker_identity"}
    operation_id = value.get("operation_id")
    try:
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError
    except ValueError:
        raise Error("remote_operation_identity_invalid") from None
    identity = value.get("lease_identity")
    if (set(value) != required or type(value.get("schema")) is not int or value["schema"] != 1
            or type(value.get("stop_protocol")) is not int
            or value["stop_protocol"] != STOP_PROTOCOL
            or value.get("state") not in {"pending", "confirmed"}
            or value.get("run_directory") != str(run.resolve())
            or value.get("binding_sha256") != _binding_digest(binding)
            or not isinstance(identity, dict) or set(identity) != {
                "id", "generation", "slot", "owner", "capability"}
            or not isinstance(identity.get("id"), str) or not identity["id"]
            or type(identity.get("generation")) is not int or identity["generation"] < 0
            or not isinstance(identity.get("slot"), str) or not identity["slot"]
            or identity.get("owner") != binding.get("owner")
            or identity.get("capability") != binding.get("capability")
            or value.get("marker_identity") is not None and (
                not isinstance(value["marker_identity"], dict)
                or set(value["marker_identity"]) != {
                    "device", "inode", "size", "mtime_ns", "ctime_ns", "sha256"}
                or any(type(value["marker_identity"].get(key)) is not int
                       or value["marker_identity"][key] < 0 for key in (
                           "device", "inode", "size", "mtime_ns", "ctime_ns"))
                or not isinstance(value["marker_identity"].get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["marker_identity"]["sha256"])
            )
            or value.get("state") == "confirmed" and value.get("marker_identity") is None):
        raise Error("remote_operation_record_invalid")
    return value


def _write_remote_record(run: Path, directory_fd: int, value: Mapping[str, Any]) -> None:
    data = _canonical_json(dict(value)) + b"\n"
    if len(data) > LIMIT:
        raise Error("remote_operation_record_too_large")
    path = run / REMOTE_STOP_RECORD
    temporary = run / f".{REMOTE_STOP_RECORD}-{uuid.uuid4().hex}"
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


def _marker_operation(path: Path) -> tuple[tuple[int, ...], bytes, dict[str, Any]]:
    identity, data = _marker_snapshot(path)
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        raise Error("uncertainty_marker_changed") from None
    if not isinstance(value, dict) or value.get("schema") != 2:
        raise Error("uncertainty_marker_changed")
    return identity, data, value


def _marker_identity(identity: tuple[int, ...], data: bytes) -> dict[str, Any]:
    return {
        "device": identity[0], "inode": identity[1], "size": identity[2],
        "mtime_ns": identity[3], "ctime_ns": identity[4],
        "sha256": __import__("hashlib").sha256(data).hexdigest(),
    }


def _remove_confirmed_marker(run: Path, directory_fd: int,
                             record: Mapping[str, Any]) -> None:
    path = run / MARKER
    if not (path.exists() or path.is_symlink()):
        return
    identity, data, marker = _marker_operation(path)
    durable_identity = record.get("marker_identity")
    if (marker.get("operation_id") != record.get("operation_id")
            or marker.get("reason") not in REASON_CODES
            or durable_identity is None
            or _marker_identity(identity, data) != durable_identity):
        raise Error("uncertainty_marker_changed")
    os.fsync(directory_fd)
    path.unlink()
    os.fsync(directory_fd)


def _create_marker(run: Path, directory_fd: int, reason: str,
                   operation_id: str) -> tuple[tuple[int, ...], bytes]:
    if not isinstance(reason, str) or reason not in REASON_CODES:
        raise Error("invalid_uncertainty_reason")
    payload = (_canonical_json({"schema": 2, "reason": reason,
                                "operation_id": operation_id}) + b"\n")
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
        self._operation_id: str | None = None
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

    def _check(self, *, require_protocol: bool = False,
               directory_fd: int | None = None) -> dict[str, Any]:
        if self._closed:
            raise Error("custody_closed")
        private(self.run, directory=True)
        info = self.run.lstat()
        directory_identity = (info.st_dev, info.st_ino)
        binding = read_private(self.run / "binding.json")
        state = read_private(self.run / "status.json")
        private(self.run / "receipt.json")
        for record in (binding, state):
            protocol = record.get("custody_marker_protocol")
            stop_protocol = record.get("stop_protocol")
            if (type(protocol) is not int or protocol != CUSTODY_MARKER_PROTOCOL
                    or type(stop_protocol) is not int or stop_protocol != STOP_PROTOCOL):
                raise Error("owner_stop_protocol_required")
        updated_at = state.get("updated_at")
        if (type(updated_at) not in (int, float) or not math.isfinite(updated_at)
                or not 0 <= time.monotonic() - updated_at <= 30):
            raise Error("lease_binding_invalid")
        parent_marker = self.run / PARENT_STOP_MARKER
        if (parent_marker.exists() or parent_marker.is_symlink()
                or state.get("parent_command_stop") == "unconfirmed"
                or (state.get("parent_command_stop") == "pending"
                    and state.get("phase") != "active")):
            raise Error("parent_command_stop_unconfirmed")
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
        remote = _read_remote_record(self.run, binding)
        marker_path = self.run / MARKER
        marker_exists = marker_path.exists() or marker_path.is_symlink()
        if remote is None:
            if marker_exists or self._marker is not None:
                raise Error("lease_binding_invalid")
        else:
            expected_identity = {
                "id": current["id"], "generation": current["generation"],
                "slot": current["slot"], "owner": binding["owner"],
                "capability": binding["capability"],
            }
            if remote["lease_identity"] != expected_identity:
                raise Error("lease_lost_or_foreign")
            if remote["state"] == "pending":
                if self._operation_id != remote["operation_id"] or not marker_exists:
                    raise Error("remote_execution_unconfirmed")
                marker = _marker_operation(marker_path)
                if (marker[2].get("operation_id") != remote["operation_id"]
                        or remote.get("marker_identity") is None
                        or _marker_identity(*marker[:2]) != remote["marker_identity"]
                        or self._marker is not None and marker[:2] != self._marker):
                    raise Error("uncertainty_marker_changed")
            else:
                if self._operation_id not in (None, remote["operation_id"]):
                    raise Error("remote_execution_not_started")
                if marker_exists:
                    if directory_fd is None:
                        raise Error("marker_lock_required")
                    _remove_confirmed_marker(self.run, directory_fd, remote)
                self._marker = None
                self._operation_id = remote["operation_id"] if self._operation_id else None
        # Pool inspection can block while the owner publishes a finish/stop signal.
        # Re-read every local admission barrier after that external call.
        latest_binding = read_private(self.run / "binding.json")
        latest_state = read_private(self.run / "status.json")
        if latest_binding != binding or latest_state != state:
            raise Error("lease_binding_invalid")
        if (parent_marker.exists() or parent_marker.is_symlink()
                or latest_state.get("parent_command_stop") == "unconfirmed"
                or latest_state.get("parent_command_stop") == "pending"
                and latest_state.get("phase") != "active"
                or any((self.run / name).exists() or (self.run / name).is_symlink()
                       for name in ("finish.json", "stop-renewal.json"))):
            raise Error("parent_command_stop_unconfirmed")
        latest_remote = _read_remote_record(self.run, binding)
        if latest_remote != remote:
            raise Error("remote_operation_record_invalid")
        latest_marker_exists = marker_path.exists() or marker_path.is_symlink()
        if remote is None and latest_marker_exists:
            raise Error("lease_binding_invalid")
        if remote is not None and remote["state"] == "confirmed" and latest_marker_exists:
            raise Error("uncertainty_marker_changed")
        if remote is not None and remote["state"] == "pending":
            if not latest_marker_exists:
                raise Error("remote_execution_unconfirmed")
            latest_marker = _marker_operation(marker_path)
            if (_marker_identity(*latest_marker[:2]) != remote.get("marker_identity")
                    or latest_marker[2].get("operation_id") != remote["operation_id"]):
                raise Error("uncertainty_marker_changed")
        self._binding = binding
        self._lease_identity = identity
        self._directory_identity = directory_identity
        return current

    def check(self) -> dict[str, Any]:
        with _run_lock(self.run) as directory_fd:
            return self._check(directory_fd=directory_fd)

    def _begin_remote_execution(self, reason: str) -> None:
        with _run_lock(self.run) as fd:
            current = self._check(require_protocol=True, directory_fd=fd)
            if self._marker is not None:
                raise Error("remote_execution_already_started")
            binding = read_private(self.run / "binding.json")
            previous = _read_remote_record(self.run, binding)
            if previous is not None and previous["state"] == "pending":
                raise Error("remote_execution_already_started")
            marker = self.run / MARKER
            if marker.exists() or marker.is_symlink():
                raise Error("uncertainty_marker_changed")
            operation_id = str(uuid.uuid4())
            record = {
                "schema": 1, "stop_protocol": STOP_PROTOCOL,
                "operation_id": operation_id, "state": "pending",
                "run_directory": str(self.run.resolve()),
                "binding_sha256": _binding_digest(binding),
                "lease_identity": {
                    "id": current["id"], "generation": current["generation"],
                    "slot": current["slot"], "owner": binding["owner"],
                    "capability": binding["capability"],
                },
                "marker_identity": None,
            }
            _write_remote_record(self.run, fd, record)
            self._operation_id = operation_id
            self._marker = _create_marker(self.run, fd, reason, operation_id)
            record = {**record, "marker_identity": _marker_identity(*self._marker)}
            _write_remote_record(self.run, fd, record)
            self._check()

    def begin_remote_execution(self) -> None:
        """Durably record one exact remote operation before caller dispatch."""
        self._begin_remote_execution("remote_stop_unconfirmed")

    def confirm_remote_stopped(self) -> None:
        """Persist positive stop proof before removing only this operation's marker."""
        with _run_lock(self.run) as fd:
            if self._closed:
                raise Error("custody_closed")
            binding = read_private(self.run / "binding.json")
            record = _read_remote_record(self.run, binding)
            if (record is None or self._operation_id is None
                    or record["operation_id"] != self._operation_id):
                raise Error("remote_execution_not_started")
            current = self._check(require_protocol=True, directory_fd=fd)
            if record["state"] == "pending":
                if self._marker is None:
                    raise Error("remote_execution_unconfirmed")
                marker = _marker_operation(self.run / MARKER)
                if (marker[:2] != self._marker
                        or marker[2].get("operation_id") != self._operation_id
                        or record.get("marker_identity") != _marker_identity(*marker[:2])):
                    raise Error("uncertainty_marker_changed")
                record = {**record, "state": "confirmed"}
                _write_remote_record(self.run, fd, record)
            elif record["state"] != "confirmed":
                raise Error("remote_operation_record_invalid")
            _remove_confirmed_marker(self.run, fd, record)
            self._marker = None
            self._operation_id = None
            self._lease_identity = tuple(current.get(key) for key in ("id", "generation", "slot"))

    def handoff_uncertain(self, reason_code: str) -> None:
        """Persist pending provenance for an operation whose remote drain is unknown."""
        if not isinstance(reason_code, str) or reason_code not in REASON_CODES:
            raise Error("invalid_uncertainty_reason")
        if self._marker is not None:
            with _run_lock(self.run):
                if _marker_snapshot(self.run / MARKER) != self._marker:
                    raise Error("uncertainty_marker_changed")
            return
        self._begin_remote_execution(reason_code)


def borrow_device(lease_config: LeaseConfig | Mapping[str, Any], serial: str) -> DeviceCustody:
    return DeviceCustody(lease_config, serial)


def handoff_uncertain(reason_code: str) -> None:
    """Record an unconfirmed remote operation for the current owner run."""
    if not isinstance(reason_code, str) or reason_code not in REASON_CODES:
        raise Error("invalid_uncertainty_reason")
    run = Path(os.environ.get("RESOURCE_LEASE_RUN_DIRECTORY", ""))
    if not run.is_absolute():
        raise Error("owned_run_required")
    binding = read_private(run / "binding.json")
    marker = run / MARKER
    if marker.exists() or marker.is_symlink():
        with _run_lock(run) as directory_fd:
            remote = _read_remote_record(run, binding)
            if remote is None:
                raise Error("lease_binding_invalid")
            marker_value = _marker_operation(marker)[2]
            if marker_value.get("operation_id") != remote["operation_id"]:
                raise Error("uncertainty_marker_changed")
            if remote["state"] == "pending":
                return
            _remove_confirmed_marker(run, directory_fd, remote)
    state = read_private(run / "status.json")
    endpoint = (state.get("lease") or {}).get("endpoint")
    serial = endpoint.get("adb_serial") if isinstance(endpoint, dict) else None
    if not isinstance(serial, str) or not serial:
        raise Error("device_serial_required")
    lease_config = binding.get("config")
    if not isinstance(lease_config, dict):
        raise Error("invalid_lease_configuration")
    with borrow_device(lease_config, serial) as custody:
        custody.handoff_uncertain(reason_code)
